import json

import pytest

from asset_registry import Registry
from asset_registry.api import create_app
from asset_registry.cli import main
from asset_registry.seed import seed


@pytest.fixture
def client(clock):
    reg = Registry(clock=clock)
    seed(reg)
    app = create_app(reg, require_user=True)
    return app.test_client()


H = {"X-Platform-User": "tim@example.com"}


def test_health_and_schema(client):
    assert client.get("/healthz").json["status"] == "ok"
    assert "asset" in client.get("/schema.json").json["entities"]


def test_writes_need_platform_user(client):
    r = client.post("/entities/target", json={"symbol": "GYS1", "name": "Glycogen synthase 1"})
    assert r.status_code == 401
    r = client.post("/entities/target", json={"symbol": "GYS1", "name": "Glycogen synthase 1"}, headers=H)
    assert r.status_code == 201 and r.json["target_id"].startswith("TGT-")


def test_create_validation_error(client):
    r = client.post("/entities/asset", json={"name": "Bad", "modality": "nope"}, headers=H)
    assert r.status_code == 422 and r.json["errors"]


def test_lineage_and_list(client):
    lin = client.get("/assets/AST-000001/lineage").json
    assert lin["asset"]["name"] == "EXAMPLE-KLK5i"
    lst = client.get("/entities/study?asset_id=AST-000001&status=complete").json
    assert lst["count"] == 1
    assert client.get("/assets/AST-000404/lineage").status_code == 404


def test_advance_records_actor(client):
    r = client.post("/assets/AST-000001/advance", json={"to_stage": "candidate", "reason": "gate",
                                                        "gate_run_id": "RUN-1"}, headers=H)
    assert r.status_code == 200 and r.json["stage"] == "candidate"
    hist = client.get("/history?asset_id=AST-000001").json["changes"]
    assert hist[-1]["actor"] == "tim@example.com" and hist[-1]["op"] == "advance_stage"
    bad = client.post("/assets/AST-000001/advance", json={"to_stage": "phase3", "reason": "x"}, headers=H)
    assert bad.status_code == 422


def test_patch(client):
    r = client.patch("/entities/asset/AST-000002", json={"owner": "CMC lead", "reason": "x"}, headers=H)
    assert r.json["owner"] == "CMC lead"


def test_exports_resolve_harmonize(client):
    assert client.get("/assets/AST-000002/exports/pts_profile").json["modality"] == "CAR-T"
    assert client.get("/assets/AST-000002/exports/nope").status_code == 422
    assert client.get("/resolve?q=EX-201").json["matches"][0]["asset_id"] == "AST-000002"
    assert client.get("/harmonize?term=Phase%20III").json["value"] == "phase3"
    assert client.get("/harmonize?term=banana").status_code == 404


def test_external_ids(client):
    r = client.post("/external-ids", json={"source_system": "DealStructureSimulator",
                                           "external_id": "klk5-deal", "canonical_id": "AST-000001"}, headers=H)
    assert r.status_code == 201
    assert client.get("/external-ids/resolve?source_system=DealStructureSimulator&external_id=klk5-deal"
                      ).json["canonical_id"] == "AST-000001"
    assert client.get("/external-ids/resolve?source_system=X&external_id=1").status_code == 404


def test_import_endpoint(client):
    export = json.load(open("examples/cgtdatamodel_export.json"))
    r = client.post("/import/cgtdatamodel", json=export, headers=H)
    assert r.json["assets_created"] == 2


def test_cli_round_trip(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AUDIT_TRAIL_URL", raising=False)
    monkeypatch.delenv("AUDIT_SPOOL", raising=False)
    db = str(tmp_path / "cli.db")
    assert main(["--db", db, "--actor", "tim", "seed"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "--actor", "tim", "add", "asset", "name=EXAMPLE-GYS1i",
                 "modality=small_molecule", "aliases=GYS1 backup,Lafora"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["asset_id"] == "AST-000003" and out["aliases"] == ["GYS1 backup", "Lafora"]
    assert main(["--db", db, "--actor", "tim", "advance", "AST-000003", "lead_optimization",
                 "--reason", "hits confirmed"]) == 0
    capsys.readouterr()
    assert main(["--db", db, "resolve", "Lafora"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["asset_id"] == "AST-000003"
    assert main(["--db", db, "advance", "AST-000003", "phase3", "--reason", "x"]) == 1
    assert main(["--db", db, "harmonize", "banana"]) == 1


def test_cli_replay_needs_url(monkeypatch):
    monkeypatch.delenv("AUDIT_TRAIL_URL", raising=False)
    assert main(["replay-audit"]) == 2
