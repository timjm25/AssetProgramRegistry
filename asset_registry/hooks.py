"""on_change hooks: send every registry change to AuditTrailService.

AuditTrailService (timjm25/AuditTrailService, P56, port 5056) takes FORM fields on
``POST /log``. Each registry change becomes one ``AUTO_LOG`` event (a stage move
from a human sign-off becomes ``HUMAN_APPROVED``) against resource
``asset:<AST-id>``, attributed to the real user that PharmaPlatformShell passed in.

The hook never raises. If the service is down the event is spooled to a local
append-only JSONL file so nothing is lost; replay it later with
``asset-registry replay-audit``.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable, Optional

Transport = Callable[[str, dict], int]

#: Same chain as AuditTrailService audittrail/model.py: sha256 over the fields joined by \x1f.
GENESIS_HASH = "0" * 64
_CHAIN_FIELDS = ("source_system", "actor", "actor_type", "action", "resource", "detail", "related_event_id")
_SPOOL_ONLY = ("ts", "prev_hash", "hash")


def chain_hash(prev_hash: str, ts: str, form: dict) -> str:
    """AuditTrailService's hash: sha256("\x1f".join([prev_hash, ts, *fields]))."""
    parts = [prev_hash, ts] + [str(form.get(k) or "") for k in _CHAIN_FIELDS]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def verify_spool(path: str) -> tuple[bool, int]:
    """Check the local spool's hash chain. Returns (intact, number_of_lines)."""
    if not os.path.exists(path):
        return True, 0
    prev, n = None, 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rec = json.loads(line)
            n += 1
            if prev is not None and rec.get("prev_hash") != prev:
                return False, n
            if chain_hash(rec.get("prev_hash", ""), rec.get("ts", ""), rec) != rec.get("hash"):
                return False, n
            prev = rec["hash"]
    return True, n


def _urllib_post(url: str, form: dict) -> int:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 - URL comes from operator config
        return resp.status


def to_audit_form(event: dict, source_system: str = "AssetProgramRegistry") -> dict:
    """Shape a registry change as AuditTrailService form fields."""
    human_gate = event.get("op") == "advance_stage" and bool(event.get("gate_run_id"))
    detail = {k: event.get(k) for k in ("op", "entity", "record_id", "reason", "seq")}
    if event.get("op") == "advance_stage":
        detail.update(from_stage=event.get("from_stage"), to_stage=event.get("to_stage"),
                      gate_run_id=event.get("gate_run_id"))
    elif event.get("op") == "update" and event.get("before") and event.get("after"):
        b, a = event["before"], event["after"]
        detail["changed"] = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    resource = f"asset:{event['asset_id']}" if event.get("asset_id") else f"{event['entity']}:{event['record_id']}"
    actor = event.get("actor") or "unknown"
    return {
        "source_system": source_system,
        "actor": actor,
        "actor_type": "automated_process" if actor in ("system", "unknown", "anonymous") else "human",
        "action": "HUMAN_APPROVED" if human_gate else "AUTO_LOG",
        "resource": resource,
        "detail": json.dumps(detail, sort_keys=True, default=str),
        "related_event_id": "",
    }


class AuditHook:
    """Callable hook: ``Registry(..., on_change=[AuditHook(url, spool)])``."""

    def __init__(self, base_url: Optional[str], spool_path: str, transport: Transport = _urllib_post):
        self.base_url = base_url.rstrip("/") if base_url else None
        self.spool_path = spool_path
        self.transport = transport

    def __call__(self, event: dict) -> None:
        form = to_audit_form(event)
        if self.base_url:
            try:
                status = self.transport(f"{self.base_url}/log", form)
                if 200 <= status < 400:
                    return
            except Exception:  # noqa: BLE001 - spool instead of failing the write
                pass
        self._spool(form)

    def _last_hash(self) -> str:
        last = GENESIS_HASH
        if os.path.exists(self.spool_path):
            with open(self.spool_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = json.loads(line).get("hash", last)
        return last

    def _spool(self, form: dict) -> None:
        """Append to the local spool, hash-chained the same way AuditTrailService chains."""
        os.makedirs(os.path.dirname(os.path.abspath(self.spool_path)), exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        prev = self._last_hash()
        rec = {**form, "ts": ts, "prev_hash": prev, "hash": chain_hash(prev, ts, form)}
        with open(self.spool_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")

    def replay(self) -> tuple[int, int]:
        """Send spooled events in order; keep the ones that still fail. Returns (sent, left)."""
        if not self.base_url or not os.path.exists(self.spool_path):
            return (0, 0)
        with open(self.spool_path, encoding="utf-8") as f:
            pending = [json.loads(line) for line in f if line.strip()]
        sent, left = 0, []
        for form in pending:
            if left:  # keep order: once one fails, keep the rest
                left.append(form)
                continue
            try:
                body = {k: v for k, v in form.items() if k not in _SPOOL_ONLY}
                ok = 200 <= self.transport(f"{self.base_url}/log", body) < 400
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                sent += 1
            else:
                left.append(form)
        # Rewrite what is left as a fresh chain (the sent part now lives in AuditTrailService).
        tmp = self.spool_path + ".tmp"
        prev = GENESIS_HASH
        with open(tmp, "w", encoding="utf-8") as f:
            for x in left:
                body = {k: v for k, v in x.items() if k not in _SPOOL_ONLY}
                rec = {**body, "ts": x["ts"], "prev_hash": prev, "hash": chain_hash(prev, x["ts"], body)}
                prev = rec["hash"]
                f.write(json.dumps(rec, sort_keys=True) + "\n")
        os.replace(tmp, self.spool_path)
        return sent, len(left)
