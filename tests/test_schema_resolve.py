import pytest

from asset_registry import harmonize, map_stage, resolve
from asset_registry import schema as S


@pytest.mark.parametrize("stage,target,expected", [
    ("candidate", "cgtdatamodel", "preclinical"),
    ("ind_filed", "ptspredictor", "ind_enabling"),
    ("ind_filed", "thesis_generator", "ind"),
    ("phase2", "thesis_generator", "phase_2"),
    ("filed", "capacity_request", "phase_3"),
    ("approved", "cgtdatamodel", "commercial"),
    ("approved", "ptspredictor", None),
])
def test_stage_map(stage, target, expected):
    assert map_stage(stage, target) == expected


def test_every_stage_mapped():
    for st in S.STAGES:
        for t in S.STAGE_TARGETS:
            map_stage(st, t)  # must not raise


def test_contract_modality():
    assert S.contract_modality("small_molecule") == "other"
    assert S.contract_modality("monoclonal_antibody") == "monoclonal_antibody"


def test_json_schema_export():
    js = S.as_json_schema()
    assert js["schema_version"] == S.SCHEMA_VERSION
    assert set(js["entities"]) == set(S.entity_names())
    assert js["stage_map"]["phase1"]["thesis_generator"] == "phase_1"


def test_resolve_by_id_name_alias_and_external(seeded):
    reg, ids = seeded
    a = ids["small_molecule"]
    assert resolve(reg, a)[0]["matched_on"] == "asset_id"
    assert resolve(reg, "example klk5i")[0]["asset_id"] == a
    assert resolve(reg, "EX-101")[0]["asset_id"] == a
    assert resolve(reg, "oral KLK5 inhibitor")[0]["asset_id"] == a
    assert resolve(reg, "7")[0]["asset_id"] == ids["car_t"]  # PTSPredictor id
    assert resolve(reg, "KLK5")[0]["asset_id"] == a  # target symbol
    assert resolve(reg, "") == []
    assert resolve(reg, "AST-000999") == []


def test_resolve_fuzzy(seeded):
    reg, ids = seeded
    hits = resolve(reg, "EXAMPLE CD19 CART")
    assert hits and hits[0]["asset_id"] == ids["car_t"]


@pytest.mark.parametrize("term,field,value,cls", [
    ("Phase II", None, "phase2", "clin:Phase2"),
    ("RMAT", None, "rmat", "reg:RMAT"),
    ("Orphan Drug Designation", None, "orphan", "reg:OrphanDrugDesignation"),
    ("CAR-T", None, "autologous_cell_therapy", "ct:AutologousCARTCellTherapy"),
    ("IND", "submission_kind", "ind", "reg:IND"),
])
def test_harmonize(term, field, value, cls):
    hit = harmonize(term, field)
    assert hit and hit["value"] == value and hit["ontology_class"] == cls


def test_harmonize_unknown():
    assert harmonize("banana") is None
