"""SQLite-backed registry with validation, permanent IDs, a crosswalk and a change log.

Design rules (borrowed from AuditTrailService and CGTDataModel):

* **IDs are permanent.** Canonical ids (``AST-000001``) are issued once and never
  reused or renamed. Nothing is deleted: an asset is retired by moving it to the
  ``terminated`` stage.
* **Every change is logged.** Each create / update / stage move writes a row to
  ``changes`` (who, when, before, after, reason) and is handed to the ``on_change``
  hooks, e.g. the AuditTrailService hook in :mod:`asset_registry.hooks`.
* **Stage moves are explicit.** ``asset.stage`` can only change through
  :meth:`Registry.advance_stage`, which enforces forward order, so a stage gate
  (ProgramDirectorAgent) is the one place a program moves.
* **Other tools keep their own ids.** :meth:`Registry.link_external` records that
  e.g. CGTDataModel's ``PRG-0001`` or PTSPredictor's asset 7 *is* ``AST-000001``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Optional

from . import schema as S

log = logging.getLogger(__name__)

ChangeHook = Callable[[dict], None]


class RegistryError(ValueError):
    """Invalid record or illegal operation. ``.errors`` lists every problem found."""

    def __init__(self, message: str, errors: Optional[list[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


class NotFound(KeyError):
    def __str__(self) -> str:
        return self.args[0]


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


_DDL = """
CREATE TABLE IF NOT EXISTS records (
    entity      TEXT NOT NULL,
    id          TEXT PRIMARY KEY,
    asset_id    TEXT,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_records_entity ON records(entity);
CREATE INDEX IF NOT EXISTS ix_records_asset ON records(asset_id);
CREATE TABLE IF NOT EXISTS counters (
    entity TEXT PRIMARY KEY,
    last   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS external_ids (
    source_system TEXT NOT NULL,
    entity        TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    canonical_id  TEXT NOT NULL,
    linked_by     TEXT NOT NULL,
    linked_at     TEXT NOT NULL,
    PRIMARY KEY (source_system, entity, external_id)
);
CREATE INDEX IF NOT EXISTS ix_ext_canonical ON external_ids(canonical_id);
CREATE TABLE IF NOT EXISTS changes (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    op         TEXT NOT NULL,
    entity     TEXT NOT NULL,
    record_id  TEXT NOT NULL,
    asset_id   TEXT,
    before     TEXT,
    after      TEXT,
    reason     TEXT
);
CREATE INDEX IF NOT EXISTS ix_changes_asset ON changes(asset_id);
CREATE TRIGGER IF NOT EXISTS changes_no_update BEFORE UPDATE ON changes
BEGIN SELECT RAISE(ABORT, 'changes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS changes_no_delete BEFORE DELETE ON changes
BEGIN SELECT RAISE(ABORT, 'changes is append-only'); END;
CREATE TRIGGER IF NOT EXISTS records_no_delete BEFORE DELETE ON records
BEGIN SELECT RAISE(ABORT, 'records are never deleted; retire an asset with stage terminated'); END;
"""

_DEFAULTS = {
    "asset": {"stage": "discovery", "status": "active", "aliases": []},
    "target": {"organism": "Homo sapiens"},
    "indication": {"orphan": False, "is_lead": False},
}


class Registry:
    """The asset and program registry. Thread-safe; one SQLite connection."""

    def __init__(self, db_path: str = ":memory:", *, clock: Callable[[], str] = _utcnow,
                 on_change: Optional[Iterable[ChangeHook]] = None):
        self.db_path = db_path
        self._clock = clock
        self._hooks: list[ChangeHook] = list(on_change or [])
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_DDL)
        self._conn.commit()

    # ------------------------------------------------------------ plumbing
    def close(self) -> None:
        self._conn.close()

    def add_hook(self, hook: ChangeHook) -> None:
        self._hooks.append(hook)

    def _emit(self, event: dict) -> None:
        for hook in self._hooks:
            try:
                hook(event)
            except Exception:  # noqa: BLE001 - a broken hook must never undo a committed change
                log.exception("on_change hook failed for %s %s", event.get("op"), event.get("record_id"))

    def _next_id(self, entity: str) -> str:
        row = self._conn.execute("SELECT last FROM counters WHERE entity = ?", (entity,)).fetchone()
        last = row["last"] if row else 0
        # skip past any id that was supplied explicitly
        while True:
            last += 1
            cid = f"{S.ID_PREFIX[entity]}-{last:06d}"
            if not self._conn.execute("SELECT 1 FROM records WHERE id = ?", (cid,)).fetchone():
                break
        self._conn.execute(
            "INSERT INTO counters(entity, last) VALUES(?, ?) "
            "ON CONFLICT(entity) DO UPDATE SET last = excluded.last", (entity, last))
        return cid

    def _log_change(self, op: str, entity: str, record_id: str, asset_id: Optional[str],
                    before: Optional[dict], after: Optional[dict], actor: str, reason: Optional[str]) -> dict:
        ts = self._clock()
        cur = self._conn.execute(
            "INSERT INTO changes(ts, actor, op, entity, record_id, asset_id, before, after, reason) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (ts, actor, op, entity, record_id, asset_id,
             json.dumps(before, sort_keys=True) if before is not None else None,
             json.dumps(after, sort_keys=True) if after is not None else None, reason))
        return {"seq": cur.lastrowid, "ts": ts, "actor": actor, "op": op, "entity": entity,
                "record_id": record_id, "asset_id": asset_id, "before": before, "after": after,
                "reason": reason}

    @staticmethod
    def _check_entity(entity: str) -> None:
        if entity not in S.SCHEMA:
            raise RegistryError(f"unknown entity {entity!r}; one of {', '.join(S.SCHEMA)}")

    # ------------------------------------------------------------ validation
    def _exists(self, entity: str, rid: str) -> bool:
        return bool(self._conn.execute(
            "SELECT 1 FROM records WHERE id = ? AND entity = ?", (rid, entity)).fetchone())

    def validate(self, entity: str, record: dict) -> list[str]:
        """Return every problem with ``record`` (empty list = valid)."""
        self._check_entity(entity)
        errors: list[str] = []
        known = {f["name"]: f for f in S.SCHEMA[entity]["fields"]}
        for extra in sorted(set(record) - set(known)):
            if not extra.startswith("x_"):
                errors.append(f"{extra}: unknown field for {entity} (custom fields must start with x_)")
        for name, f in known.items():
            value = record.get(name)
            if value is None or value == "":
                if f["required"]:
                    errors.append(f"{name}: required")
                continue
            t = f["type"]
            if t == "id":
                if not isinstance(value, str) or not S.ID_PATTERN.match(value) \
                        or not value.startswith(S.ID_PREFIX[entity] + "-"):
                    errors.append(f"{name}: must look like {S.ID_PREFIX[entity]}-000001")
            elif t == "string":
                if not isinstance(value, str):
                    errors.append(f"{name}: must be text")
            elif t == "enum":
                if value not in f["values"]:
                    errors.append(f"{name}: {value!r} not one of {', '.join(f['values'])}")
            elif t == "ref":
                if not isinstance(value, str) or not self._exists(f["ref_entity"], value):
                    errors.append(f"{name}: {f['ref_entity']} {value!r} does not exist")
            elif t == "date":
                try:
                    date.fromisoformat(value)
                except (TypeError, ValueError):
                    errors.append(f"{name}: must be an ISO date YYYY-MM-DD")
            elif t == "number":
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    errors.append(f"{name}: must be a number")
                elif value < 0:
                    errors.append(f"{name}: must be >= 0")
            elif t == "integer":
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    errors.append(f"{name}: must be a whole number >= 0")
            elif t == "bool":
                if not isinstance(value, bool):
                    errors.append(f"{name}: must be true or false")
            elif t == "list":
                if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                    errors.append(f"{name}: must be a list of text values")
        # cross-field rules
        for a, b in (("start_date", "end_date"), ("priority_date", "expiry_date")):
            if record.get(a) and record.get(b):
                try:
                    if date.fromisoformat(record[b]) < date.fromisoformat(record[a]):
                        errors.append(f"{b}: must not be before {a}")
                except ValueError:
                    pass
        if entity == "deal" and record.get("royalty_pct") not in (None, ""):
            if isinstance(record["royalty_pct"], (int, float)) and record["royalty_pct"] > 1:
                errors.append("royalty_pct: give a fraction between 0 and 1 (e.g. 0.08)")
        if entity == "submission" and record.get("kind") == "designation" and not record.get("designation"):
            errors.append("designation: required when kind is designation")
        if entity == "asset" and record.get("stage") == "terminated" and record.get("status") not in (None, "terminated"):
            errors.append("status: must be terminated when stage is terminated")
        if entity == "indication" and record.get("is_lead") and record.get("asset_id"):
            row = self._conn.execute(
                "SELECT id, data FROM records WHERE entity='indication' AND asset_id=?",
                (record["asset_id"],)).fetchall()
            for r in row:
                if r["id"] != record.get("indication_id") and json.loads(r["data"]).get("is_lead"):
                    errors.append(f"is_lead: asset already has lead indication {r['id']}")
        return errors

    # ------------------------------------------------------------ writes
    def create(self, entity: str, data: dict, *, actor: str = "system", reason: Optional[str] = None) -> dict:
        """Create a record. The canonical id is issued here unless one is supplied."""
        self._check_entity(entity)
        key = S.key_of(entity)
        record = {**_DEFAULTS.get(entity, {}), **{k: v for k, v in data.items() if v is not None}}
        with self._lock:
            if record.get(key):
                if self._conn.execute("SELECT 1 FROM records WHERE id = ?", (record[key],)).fetchone():
                    raise RegistryError(f"{record[key]} already exists; ids are never reused")
            else:
                record[key] = self._next_id(entity)
            errors = self.validate(entity, record)
            if errors:
                self._conn.rollback()
                raise RegistryError(f"invalid {entity}: " + "; ".join(errors), errors)
            now = self._clock()
            asset_id = record[key] if entity == "asset" else record.get("asset_id")
            self._conn.execute(
                "INSERT INTO records(entity, id, asset_id, data, created_at, updated_at) VALUES(?,?,?,?,?,?)",
                (entity, record[key], asset_id, json.dumps(record, sort_keys=True), now, now))
            event = self._log_change("create", entity, record[key], asset_id, None, record, actor, reason)
            self._conn.commit()
        self._emit(event)
        return self.get(entity, record[key])

    def update(self, entity: str, rid: str, changes: dict, *, actor: str = "system",
               reason: Optional[str] = None) -> dict:
        """Patch fields of a record. Keys, owning asset and stage are not patchable."""
        self._check_entity(entity)
        key = S.key_of(entity)
        if key in changes and changes[key] != rid:
            raise RegistryError(f"{key} is permanent and cannot be changed")
        if entity in S.ASSET_SCOPED and "asset_id" in changes:
            raise RegistryError("asset_id cannot be changed; create a new record under the other asset")
        if entity == "asset" and "stage" in changes:
            raise RegistryError("stage changes go through advance_stage (a stage gate), not update")
        with self._lock:
            before = self._raw(entity, rid)
            after = {**before, **changes}
            after = {k: v for k, v in after.items() if v is not None}
            errors = self.validate(entity, after)
            if errors:
                raise RegistryError(f"invalid {entity}: " + "; ".join(errors), errors)
            if after == before:
                return self.get(entity, rid)
            self._conn.execute("UPDATE records SET data = ?, updated_at = ? WHERE id = ?",
                               (json.dumps(after, sort_keys=True), self._clock(), rid))
            asset_id = rid if entity == "asset" else after.get("asset_id")
            event = self._log_change("update", entity, rid, asset_id, before, after, actor, reason)
            self._conn.commit()
        self._emit(event)
        return self.get(entity, rid)

    def advance_stage(self, asset_id: str, to_stage: str, *, actor: str, reason: str,
                      gate_run_id: Optional[str] = None, allow_skip: bool = False) -> dict:
        """Move an asset to a later stage (or terminate it). Called by the stage gate.

        Forward only, one stage at a time unless ``allow_skip`` (e.g. a licensed-in
        asset that arrives at phase 2). ``reason`` is mandatory: a stage move is a
        decision someone has to be able to explain later.
        """
        if not reason or not reason.strip():
            raise RegistryError("reason is required for a stage move")
        if to_stage not in S.ALL_STAGES:
            raise RegistryError(f"unknown stage {to_stage!r}")
        with self._lock:
            before = self._raw("asset", asset_id)
            current = before["stage"]
            if current == "terminated":
                raise RegistryError(f"{asset_id} is terminated; stage can no longer change")
            if to_stage != "terminated":
                cur_i, new_i = S.STAGES.index(current), S.STAGES.index(to_stage)
                if new_i <= cur_i:
                    raise RegistryError(f"{asset_id} is at {current}; stages only move forward")
                if new_i - cur_i > 1 and not allow_skip:
                    raise RegistryError(
                        f"{current} -> {to_stage} skips {', '.join(S.STAGES[cur_i + 1:new_i])}; "
                        "pass allow_skip for an in-licensed asset")
            after = {**before, "stage": to_stage}
            if to_stage == "terminated":
                after["status"] = "terminated"
            self._conn.execute("UPDATE records SET data = ?, updated_at = ? WHERE id = ?",
                               (json.dumps(after, sort_keys=True), self._clock(), asset_id))
            full_reason = reason if not gate_run_id else f"{reason} [gate run {gate_run_id}]"
            event = self._log_change("advance_stage", "asset", asset_id, asset_id,
                                     before, after, actor, full_reason)
            event["from_stage"], event["to_stage"], event["gate_run_id"] = current, to_stage, gate_run_id
            self._conn.commit()
        self._emit(event)
        return self.get("asset", asset_id)

    # ------------------------------------------------------------ crosswalk
    def link_external(self, source_system: str, external_id: str, canonical_id: str, *,
                      entity: Optional[str] = None, actor: str = "system") -> dict:
        """Record that ``source_system``'s ``external_id`` is our ``canonical_id``."""
        source_system, external_id = (source_system or "").strip(), str(external_id or "").strip()
        if not source_system or not external_id:
            raise RegistryError("source_system and external_id are required")
        row = self._conn.execute("SELECT entity FROM records WHERE id = ?", (canonical_id,)).fetchone()
        if not row:
            raise NotFound(f"{canonical_id} does not exist")
        entity = entity or row["entity"]
        if entity != row["entity"]:
            raise RegistryError(f"{canonical_id} is a {row['entity']}, not a {entity}")
        with self._lock:
            existing = self._conn.execute(
                "SELECT canonical_id FROM external_ids WHERE source_system=? AND entity=? AND external_id=?",
                (source_system, entity, external_id)).fetchone()
            if existing:
                if existing["canonical_id"] != canonical_id:
                    raise RegistryError(
                        f"{source_system} {entity} {external_id} is already linked to {existing['canonical_id']}")
                return self._ext_row(source_system, entity, external_id)
            self._conn.execute(
                "INSERT INTO external_ids VALUES(?,?,?,?,?,?)",
                (source_system, entity, external_id, canonical_id, actor, self._clock()))
            asset_id = self._asset_of(entity, canonical_id)
            event = self._log_change("link_external", entity, canonical_id, asset_id, None,
                                     {"source_system": source_system, "external_id": external_id},
                                     actor, None)
            self._conn.commit()
        self._emit(event)
        return self._ext_row(source_system, entity, external_id)

    def _ext_row(self, source_system: str, entity: str, external_id: str) -> dict:
        r = self._conn.execute(
            "SELECT * FROM external_ids WHERE source_system=? AND entity=? AND external_id=?",
            (source_system, entity, external_id)).fetchone()
        return dict(r)

    def resolve_external(self, source_system: str, external_id: str,
                         entity: Optional[str] = None) -> Optional[str]:
        """Canonical id for another tool's id, or None if never linked."""
        q = "SELECT canonical_id FROM external_ids WHERE source_system=? AND external_id=?"
        params: list[Any] = [source_system, str(external_id)]
        if entity:
            q += " AND entity=?"
            params.append(entity)
        rows = self._conn.execute(q, params).fetchall()
        if len(rows) > 1:
            raise RegistryError(f"{source_system} id {external_id} maps to several entities; pass entity")
        return rows[0]["canonical_id"] if rows else None

    def external_ids(self, canonical_id: Optional[str] = None, asset_id: Optional[str] = None) -> list[dict]:
        if canonical_id:
            rows = self._conn.execute("SELECT * FROM external_ids WHERE canonical_id=? ORDER BY source_system",
                                      (canonical_id,)).fetchall()
        elif asset_id:
            ids = [asset_id] + [r["id"] for r in self._conn.execute(
                "SELECT id FROM records WHERE asset_id=?", (asset_id,)).fetchall()]
            marks = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT * FROM external_ids WHERE canonical_id IN ({marks}) ORDER BY source_system, external_id",
                ids).fetchall()
        else:
            rows = self._conn.execute("SELECT * FROM external_ids ORDER BY source_system").fetchall()
        return [{k: r[k] for k in ("source_system", "external_id", "entity", "canonical_id")} for r in rows]

    # ------------------------------------------------------------ reads
    def _raw(self, entity: str, rid: str) -> dict:
        row = self._conn.execute("SELECT data FROM records WHERE id = ? AND entity = ?", (rid, entity)).fetchone()
        if not row:
            raise NotFound(f"{entity} {rid} does not exist")
        return json.loads(row["data"])

    def _asset_of(self, entity: str, rid: str) -> Optional[str]:
        if entity == "asset":
            return rid
        row = self._conn.execute("SELECT asset_id FROM records WHERE id = ?", (rid,)).fetchone()
        return row["asset_id"] if row else None

    def get(self, entity: str, rid: str) -> dict:
        self._check_entity(entity)
        row = self._conn.execute("SELECT data, created_at, updated_at FROM records WHERE id=? AND entity=?",
                                 (rid, entity)).fetchone()
        if not row:
            raise NotFound(f"{entity} {rid} does not exist")
        rec = json.loads(row["data"])
        if entity == "asset":
            rec["created_at"], rec["updated_at"] = row["created_at"], row["updated_at"]
        return rec

    def list(self, entity: str, *, asset_id: Optional[str] = None, **filters: Any) -> list[dict]:
        self._check_entity(entity)
        if asset_id:
            rows = self._conn.execute("SELECT id FROM records WHERE entity=? AND asset_id=? ORDER BY id",
                                      (entity, asset_id)).fetchall()
        else:
            rows = self._conn.execute("SELECT id FROM records WHERE entity=? ORDER BY id", (entity,)).fetchall()
        out = [self.get(entity, r["id"]) for r in rows]
        for k, v in filters.items():
            if v is not None:
                out = [r for r in out if r.get(k) == v]
        return out

    def lineage(self, asset_id: str) -> dict:
        """Everything about one asset, target to deal: the record downstream tools consume."""
        asset = self.get("asset", asset_id)
        target = self.get("target", asset["target_id"]) if asset.get("target_id") else None
        scoped = {e: self.list(e, asset_id=asset_id) for e in S.ASSET_SCOPED}
        partner_ids = sorted({r["partner_id"] for e in ("study", "batch", "deal")
                              for r in scoped[e] if r.get("partner_id")})
        return {
            "asset_id": asset_id,
            "schema_version": S.SCHEMA_VERSION,
            "asset": asset,
            "target": target,
            "indications": scoped["indication"],
            "studies": scoped["study"],
            "batches": scoped["batch"],
            "submissions": scoped["submission"],
            "deals": scoped["deal"],
            "partners": [self.get("partner", p) for p in partner_ids],
            "ip_refs": scoped["ip_ref"],
            "external_ids": self.external_ids(asset_id=asset_id),
        }

    def history(self, *, asset_id: Optional[str] = None, record_id: Optional[str] = None,
                limit: int = 500) -> list[dict]:
        q, params = "SELECT * FROM changes", []
        if asset_id:
            q, params = q + " WHERE asset_id = ?", [asset_id]
        elif record_id:
            q, params = q + " WHERE record_id = ?", [record_id]
        rows = self._conn.execute(q + " ORDER BY seq LIMIT ?", params + [limit]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("before", "after"):
                d[k] = json.loads(d[k]) if d[k] else None
            out.append(d)
        return out

    def stats(self) -> dict:
        counts = {e: 0 for e in S.SCHEMA}
        for r in self._conn.execute("SELECT entity, COUNT(*) n FROM records GROUP BY entity"):
            counts[r["entity"]] = r["n"]
        by_stage: dict[str, int] = {}
        for a in self.list("asset"):
            by_stage[a["stage"]] = by_stage.get(a["stage"], 0) + 1
        return {
            "records": counts,
            "assets_by_stage": by_stage,
            "external_ids": self._conn.execute("SELECT COUNT(*) FROM external_ids").fetchone()[0],
            "changes": self._conn.execute("SELECT COUNT(*) FROM changes").fetchone()[0],
        }
