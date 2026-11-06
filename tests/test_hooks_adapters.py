import json

import pytest

from asset_registry import Registry, RegistryError
from asset_registry import adapters
from asset_registry.hooks import AuditHook, chain_hash, GENESIS_HASH, to_audit_form, verify_spool


def test_audit_form_for_gate_approval(seeded):
    reg, ids = seeded
    events = []
    reg.add_hook(events.append)
    reg.advance_stage(ids["small_molecule"], "candidate", actor="tim", reason="gate", gate_run_id="RUN-9")
    form = to_audit_form(events[-1])
    assert form["action"] == "HUMAN_APPROVED"
    assert form["actor_type"] == "human"
    assert form["resource"] == f"asset:{ids['small_molecule']}"
    detail = json.loads(form["detail"])
    assert detail["from_stage"] == "lead_optimization" and detail["gate_run_id"] == "RUN-9"
    assert set(form) == {"source_system", "actor", "actor_type", "action", "resource", "detail", "related_event_id"}


def test_audit_hook_sends_when_up(tmp_path, clock):
    sent = []
    hook = AuditHook("http://audit.local", str(tmp_path / "spool.jsonl"),
                     transport=lambda url, form: sent.append((url, form)) or 201)
    reg = Registry(clock=clock, on_change=[hook])
    reg.create("asset", {"name": "A", "modality": "mrna"}, actor="system")
    assert sent[0][0] == "http://audit.local/log"
    assert sent[0][1]["actor_type"] == "automated_process"
    assert not (tmp_path / "spool.jsonl").exists()


def test_audit_hook_spools_with_hash_chain_and_replays(tmp_path, clock):
    spool = str(tmp_path / "spool.jsonl")
    up = {"ok": False}
    received = []

    def transport(url, form):
        if not up["ok"]:
            raise ConnectionError("down")
        received.append(form)
        return 201

    hook = AuditHook("http://audit.local", spool, transport=transport)
    reg = Registry(clock=clock, on_change=[hook])
    a = reg.create("asset", {"name": "A", "modality": "mrna"}, actor="tim")
    reg.update("asset", a["asset_id"], {"owner": "x"}, actor="tim")
    lines = [json.loads(l) for l in open(spool)]
    assert len(lines) == 2
    assert lines[0]["prev_hash"] == GENESIS_HASH
    assert lines[1]["prev_hash"] == lines[0]["hash"]
    assert lines[0]["hash"] == chain_hash(GENESIS_HASH, lines[0]["ts"], lines[0])
    assert verify_spool(spool) == (True, 2)

    up["ok"] = True
    assert hook.replay() == (2, 0)
    assert all("hash" not in f and "ts" not in f for f in received)
    assert verify_spool(spool) == (True, 0)


def test_tampered_spool_detected(tmp_path):
    spool = str(tmp_path / "s.jsonl")
    hook = AuditHook(None, spool)
    hook({"op": "create", "entity": "asset", "record_id": "AST-000001", "asset_id": "AST-000001", "actor": "tim"})
    hook({"op": "update", "entity": "asset", "record_id": "AST-000001", "asset_id": "AST-000001", "actor": "tim"})
    lines = open(spool).read().splitlines()
    rec = json.loads(lines[0]); rec["actor"] = "mallory"; lines[0] = json.dumps(rec)
    open(spool, "w").write("\n".join(lines) + "\n")
    assert verify_spool(spool)[0] is False


def test_import_cgtdatamodel_is_idempotent(reg):
    export = json.load(open("examples/cgtdatamodel_export.json"))
    cdmo = reg.create("partner", {"name": "EXAMPLE CDMO", "kind": "cdmo"})
    s1 = adapters.import_cgtdatamodel(reg, export, cdmo_partner_id=cdmo["partner_id"])
    assert s1["assets_created"] == 2 and s1["batches_created"] == 1
    assert any("gene_therapy" in w for w in s1["warnings"])
    s2 = adapters.import_cgtdatamodel(reg, export)
    assert s2["assets_created"] == 0 and s2["assets_matched"] == 2 and s2["batches_matched"] == 1
    aid = reg.resolve_external("CGTDataModel", "PRG-0001", "asset")
    lin = reg.lineage(aid)
    assert lin["asset"]["stage"] == "phase1"
    assert lin["batches"][0]["cgt_batch_id"] == "BAT-0101"
    assert lin["partners"][0]["partner_id"] == cdmo["partner_id"]


def test_exports(seeded):
    reg, ids = seeded
    car = reg.lineage(ids["car_t"])
    sm = reg.lineage(ids["small_molecule"])
    prog = adapters.to_cgt_program(car, "CLI-0001")
    assert prog["modality"] == "car_t" and prog["phase"] == "phase1"
    assert adapters.to_pts_profile(car)["modality"] == "CAR-T"
    with pytest.raises(RegistryError):
        adapters.to_pts_profile(sm)
    with pytest.raises(RegistryError):
        adapters.to_cgt_program(sm, "CLI-0001")
    reg_prof = adapters.to_regulatory_profile(car)
    assert reg_prof["designations_granted"] == ["rmat"]
    assert adapters.to_capacity_request_fields(sm) == {
        "program_id": ids["small_molecule"], "modality": "other", "phase": "preclinical"}
    nodes = adapters.to_thread_nodes(car)
    assert {n["type"] for n in nodes} == {"batch", "submission"}
    assert all(n["asset_id"] == ids["car_t"] for n in nodes)
