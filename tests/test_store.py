import json
import sqlite3

import pytest

from asset_registry import NotFound, Registry, RegistryError
from asset_registry import schema as S


def test_ids_are_issued_in_order_and_match_pattern(reg):
    t = reg.create("target", {"symbol": "KLK5", "name": "Kallikrein 5"})
    a = reg.create("asset", {"name": "X", "modality": "small_molecule", "target_id": t["target_id"]})
    assert t["target_id"] == "TGT-000001"
    assert a["asset_id"] == "AST-000001"
    assert S.ASSET_ID_PATTERN.match(a["asset_id"])
    assert a["stage"] == "discovery" and a["status"] == "active" and a["aliases"] == []


def test_validation_reports_every_problem(reg):
    with pytest.raises(RegistryError) as e:
        reg.create("asset", {"modality": "magic", "target_id": "TGT-999999"})
    msg = " ".join(e.value.errors)
    assert "name" in msg and "modality" in msg and "TGT-999999" in msg


def test_scoped_record_needs_existing_asset(reg):
    with pytest.raises(RegistryError):
        reg.create("indication", {"asset_id": "AST-000123", "name": "X"})


def test_ids_never_reused(reg):
    reg.create("partner", {"partner_id": "PTN-000005", "name": "P", "kind": "cro"})
    with pytest.raises(RegistryError):
        reg.create("partner", {"partner_id": "PTN-000005", "name": "Q", "kind": "cro"})
    nxt = reg.create("partner", {"name": "R", "kind": "cdmo"})
    assert nxt["partner_id"] == "PTN-000001"


def test_update_cannot_touch_stage_or_key(seeded):
    reg, ids = seeded
    with pytest.raises(RegistryError):
        reg.update("asset", ids["small_molecule"], {"stage": "phase3"})
    with pytest.raises(RegistryError):
        reg.update("asset", ids["small_molecule"], {"asset_id": "AST-000099"})
    rec = reg.update("asset", ids["small_molecule"], {"owner": "New lead"}, actor="tim", reason="handover")
    assert rec["owner"] == "New lead"


def test_advance_stage_forward_one_step(seeded):
    reg, ids = seeded
    a = ids["small_molecule"]  # lead_optimization
    with pytest.raises(RegistryError, match="reason"):
        reg.advance_stage(a, "candidate", actor="tim", reason="")
    with pytest.raises(RegistryError, match="skips"):
        reg.advance_stage(a, "ind_enabling", actor="tim", reason="x")
    with pytest.raises(RegistryError, match="forward"):
        reg.advance_stage(a, "discovery", actor="tim", reason="x")
    rec = reg.advance_stage(a, "candidate", actor="tim", reason="KLK5 gate passed", gate_run_id="RUN-1")
    assert rec["stage"] == "candidate"
    rec = reg.advance_stage(a, "phase1", actor="tim", reason="in-licensed", allow_skip=True)
    assert rec["stage"] == "phase1"


def test_terminate_is_final(seeded):
    reg, ids = seeded
    a = ids["car_t"]
    rec = reg.advance_stage(a, "terminated", actor="tim", reason="safety signal")
    assert rec["status"] == "terminated"
    with pytest.raises(RegistryError, match="terminated"):
        reg.advance_stage(a, "phase2", actor="tim", reason="revive")


def test_change_log_is_append_only(seeded, tmp_path):
    reg, ids = seeded
    with pytest.raises(sqlite3.DatabaseError):
        reg._conn.execute("DELETE FROM changes")
    with pytest.raises(sqlite3.DatabaseError):
        reg._conn.execute("UPDATE changes SET actor='x'")
    with pytest.raises(sqlite3.DatabaseError):
        reg._conn.execute("DELETE FROM records WHERE id=?", (ids["car_t"],))


def test_history_and_hooks(clock):
    events = []
    reg = Registry(clock=clock, on_change=[events.append, lambda e: 1 / 0])  # broken hook is tolerated
    a = reg.create("asset", {"name": "A", "modality": "mrna"}, actor="tim")
    reg.advance_stage(a["asset_id"], "lead_optimization", actor="tim", reason="hit confirmed", gate_run_id="G1")
    ops = [e["op"] for e in events]
    assert ops == ["create", "advance_stage"]
    assert events[1]["from_stage"] == "discovery" and events[1]["to_stage"] == "lead_optimization"
    hist = reg.history(asset_id=a["asset_id"])
    assert [h["op"] for h in hist] == ["create", "advance_stage"]
    assert "[gate run G1]" in hist[1]["reason"]


def test_crosswalk(seeded):
    reg, ids = seeded
    a = ids["small_molecule"]
    reg.link_external("ThesisGenerator", "klk5-thesis", a)
    assert reg.resolve_external("ThesisGenerator", "klk5-thesis") == a
    assert reg.link_external("ThesisGenerator", "klk5-thesis", a)["canonical_id"] == a  # idempotent
    with pytest.raises(RegistryError, match="already linked"):
        reg.link_external("ThesisGenerator", "klk5-thesis", ids["car_t"])
    with pytest.raises(NotFound):
        reg.link_external("X", "1", "AST-000999")
    assert reg.resolve_external("Nobody", "1") is None


def test_lineage_shape_matches_fixture(seeded):
    reg, ids = seeded
    lin = reg.lineage(ids["car_t"])
    with open("examples/lineage_AST-000002.json") as f:
        fixture = json.load(f)
    assert set(lin) == set(fixture)
    assert lin["asset"]["modality"] == "autologous_cell_therapy"
    assert lin["target"]["symbol"] == "CD19"
    assert lin["batches"][0]["cgt_batch_id"] == "BAT-0001"
    assert [p["kind"] for p in lin["partners"]] == ["cdmo"]
    assert {"source_system": "PTSPredictor", "external_id": "7", "entity": "asset",
            "canonical_id": ids["car_t"]} in lin["external_ids"]


def test_stats(seeded):
    reg, _ = seeded
    st = reg.stats()
    assert st["records"]["asset"] == 2
    assert st["assets_by_stage"] == {"lead_optimization": 1, "phase1": 1}


def test_persistence(tmp_path, clock):
    db = str(tmp_path / "r.db")
    r1 = Registry(db, clock=clock)
    a = r1.create("asset", {"name": "Persist", "modality": "peptide"})
    r1.close()
    r2 = Registry(db, clock=clock)
    assert r2.get("asset", a["asset_id"])["name"] == "Persist"
    assert r2.create("asset", {"name": "Next", "modality": "peptide"})["asset_id"] == "AST-000002"
