"""Canonical sponsor-side schema: one permanent Asset ID from target to deal.

This extends CGTDataModel's field-level schema (timjm25/CGTDataModel,
``cgtdatamodel/schema.py``: Client -> Program -> Batch -> Deviation) up the value
chain a virtual pharma actually runs:

    Target -> Asset -> Indication -> Study -> Batch -> Submission -> Deal
                 \\-> Partner (CRO / CDMO / licensee) and IP references

The dict layout deliberately mirrors CGTDataModel's ``SCHEMA`` (``key``,
``fields`` with ``type`` / ``required`` / ``values`` / ``ref_entity``) so a tool
that already reads CGTDataModel's ``/schema.json`` can read this one the same way.
Controlled vocabularies reuse CDMOAgentContracts' ``Common`` modality enum and
CDMOSemanticLayer ontology class ids where they exist.
"""
from __future__ import annotations

import re

SCHEMA_VERSION = "1.0.0"

#: Prefix for each entity's canonical identifier (``AST-000001``).
ID_PREFIX = {
    "target": "TGT",
    "asset": "AST",
    "indication": "IND",
    "study": "STY",
    "batch": "BCH",
    "submission": "SUB",
    "deal": "DEL",
    "partner": "PTN",
    "ip_ref": "IPR",
}
ID_PATTERN = re.compile(r"^(TGT|AST|IND|STY|BCH|SUB|DEL|PTN|IPR)-\d{6}$")
ASSET_ID_PATTERN = re.compile(r"^AST-\d{6}$")

# ---------------------------------------------------------------- vocabularies

#: Development stage, in order. ``terminated`` can be reached from any stage.
STAGES = [
    "discovery", "lead_optimization", "candidate", "ind_enabling", "ind_filed",
    "phase1", "phase2", "phase3", "filed", "approved",
]
TERMINAL_STAGES = ["terminated"]
ALL_STAGES = STAGES + TERMINAL_STAGES

#: CDMOAgentContracts Common 1.0.0 ``modality`` enum (commit 8c7bf2c) ...
CONTRACT_MODALITIES = [
    "autologous_cell_therapy", "allogeneic_cell_therapy", "gene_therapy_aav",
    "gene_therapy_lvv", "viral_vector", "plasmid_dna", "mrna",
    "monoclonal_antibody", "adc", "recombinant_protein", "other",
]
#: ... plus the sponsor-side modalities a virtual pharma also runs.
SPONSOR_MODALITIES = ["small_molecule", "oligonucleotide", "peptide"]
MODALITIES = CONTRACT_MODALITIES + SPONSOR_MODALITIES

#: registry stage -> other tools' stage vocabularies (see README "Stage mapping").
STAGE_MAP = {
    #  stage:              (CGTDataModel phase, PTSPredictor stage, thesis-generator key, CapacityRequest phase)
    "discovery":          ("preclinical", "preclinical", "preclinical", "preclinical"),
    "lead_optimization":  ("preclinical", "preclinical", "preclinical", "preclinical"),
    "candidate":          ("preclinical", "preclinical", "preclinical", "preclinical"),
    "ind_enabling":       ("preclinical", "ind_enabling", "preclinical", "preclinical"),
    "ind_filed":          ("preclinical", "ind_enabling", "ind", "preclinical"),
    "phase1":             ("phase1", "phase1", "phase_1", "phase_1"),
    "phase2":             ("phase2", "phase2", "phase_2", "phase_2"),
    "phase3":             ("phase3", "phase3", "phase_3", "phase_3"),
    "filed":              ("phase3", "filed", "filing", "phase_3"),
    "approved":           ("commercial", None, "approval", "commercial"),
    "terminated":         (None, None, None, None),
}
STAGE_TARGETS = ("cgtdatamodel", "ptspredictor", "thesis_generator", "capacity_request")

#: Our modality -> CGTDataModel program.modality (only cell/gene modalities exist there).
CGT_MODALITY = {
    "autologous_cell_therapy": "car_t",
    "allogeneic_cell_therapy": "cell_therapy",
    "gene_therapy_aav": "gene_therapy",
    "gene_therapy_lvv": "gene_therapy",
}
#: Our modality -> PTSPredictor MODALITIES (only three have benchmarks there).
PTS_MODALITY = {
    "autologous_cell_therapy": "CAR-T",
    "gene_therapy_lvv": "LVV",
    "allogeneic_cell_therapy": "Allogeneic",
}


def map_stage(stage: str, target: str):
    """Translate a registry stage into another tool's vocabulary (None = no equivalent)."""
    if stage not in STAGE_MAP:
        raise ValueError(f"unknown stage {stage!r}")
    if target not in STAGE_TARGETS:
        raise ValueError(f"unknown target vocabulary {target!r}; one of {STAGE_TARGETS}")
    return STAGE_MAP[stage][STAGE_TARGETS.index(target)]


def contract_modality(modality: str) -> str:
    """Modality as CDMOAgentContracts Common accepts it (sponsor extras -> 'other')."""
    return modality if modality in CONTRACT_MODALITIES else "other"


# ---------------------------------------------------------------- entities


def _f(name, type_, required=False, description="", **extra):
    d = {"name": name, "type": type_, "required": required, "description": description}
    d.update(extra)
    return d


SCHEMA: dict[str, dict] = {
    "target": {
        "label": "Target",
        "key": "target_id",
        "fields": [
            _f("target_id", "id", True, "Canonical target id, e.g. TGT-000001"),
            _f("symbol", "string", True, "Gene symbol, e.g. KLK5"),
            _f("name", "string", False, "Protein name"),
            _f("uniprot_id", "string", False, "UniProt accession"),
            _f("organism", "string", False, "Default Homo sapiens"),
            _f("validation_evidence", "string", False, "Summary of target validation"),
        ],
    },
    "asset": {
        "label": "Asset",
        "key": "asset_id",
        "fields": [
            _f("asset_id", "id", True, "Permanent Asset ID, e.g. AST-000001. Never reused."),
            _f("name", "string", True, "Asset name"),
            _f("code_name", "string", False, "Internal code, e.g. EX-101"),
            _f("modality", "enum", True, "Modality (CDMOAgentContracts Common + sponsor extras)", values=MODALITIES),
            _f("stage", "enum", True, "Development stage", values=ALL_STAGES),
            _f("status", "enum", True, "Program status", values=["active", "paused", "terminated"]),
            _f("target_id", "ref", False, "Primary target", ref_entity="target"),
            _f("owner", "string", False, "Accountable program lead"),
            _f("aliases", "list", False, "Other names the asset goes by"),
            _f("description", "string", False, "One-paragraph description"),
        ],
    },
    "indication": {
        "label": "Indication",
        "key": "indication_id",
        "fields": [
            _f("indication_id", "id", True),
            _f("asset_id", "ref", True, "Owning asset", ref_entity="asset"),
            _f("name", "string", True, "Disease / indication"),
            _f("doid", "string", False, "Disease Ontology id, e.g. DOID:0050474"),
            _f("orphan", "bool", False, "Meets orphan prevalence threshold"),
            _f("is_lead", "bool", False, "Lead indication for valuation"),
            _f("us_prevalence", "integer", False, "US patients"),
            _f("peak_sales_millions", "number", False, "Peak sales estimate, USD millions"),
        ],
    },
    "study": {
        "label": "Study",
        "key": "study_id",
        "fields": [
            _f("study_id", "id", True),
            _f("asset_id", "ref", True, ref_entity="asset"),
            _f("indication_id", "ref", False, ref_entity="indication"),
            _f("kind", "enum", True, values=["in_vitro", "in_vivo", "tox", "cmc_development", "clinical"]),
            _f("phase", "enum", False, values=["preclinical", "phase1", "phase2", "phase3"]),
            _f("title", "string", True),
            _f("partner_id", "ref", False, "CRO / site running it", ref_entity="partner"),
            _f("status", "enum", True, values=["planned", "ongoing", "complete", "terminated"]),
            _f("start_date", "date", False),
            _f("end_date", "date", False),
            _f("nct_id", "string", False, "ClinicalTrials.gov id"),
        ],
    },
    "batch": {
        "label": "Batch",
        "key": "batch_id",
        "fields": [
            _f("batch_id", "id", True),
            _f("asset_id", "ref", True, ref_entity="asset"),
            _f("lot_number", "string", True),
            _f("purpose", "enum", True, values=["engineering", "tox", "gmp_clinical", "commercial"]),
            # same disposition values as CGTDataModel batch.status
            _f("status", "enum", True, values=["in_process", "released", "quarantined", "rejected"]),
            _f("manufactured_at", "date", False),
            _f("partner_id", "ref", False, "CDMO that made it", ref_entity="partner"),
            _f("cgt_batch_id", "string", False, "CGTDataModel batch_id (e.g. BAT-0001) when made at a CDMO using it"),
        ],
    },
    "submission": {
        "label": "Submission",
        "key": "submission_id",
        "fields": [
            _f("submission_id", "id", True),
            _f("asset_id", "ref", True, ref_entity="asset"),
            _f("kind", "enum", True, values=["pre_ind", "ind", "cta", "bla", "nda", "maa", "designation"]),
            _f("agency", "enum", True, values=["FDA", "EMA", "MHRA", "PMDA", "NMPA", "other"]),
            _f("designation", "enum", False,
               values=["orphan", "rmat", "breakthrough", "fast_track", "priority_review"]),
            _f("status", "enum", True,
               values=["planned", "submitted", "accepted", "granted", "approved", "rejected", "withdrawn"]),
            _f("target_date", "date", False),
            _f("submitted_date", "date", False),
            _f("decision_date", "date", False),
        ],
    },
    "deal": {
        "label": "Deal",
        "key": "deal_id",
        "fields": [
            _f("deal_id", "id", True),
            _f("asset_id", "ref", True, ref_entity="asset"),
            _f("partner_id", "ref", False, "Counterparty", ref_entity="partner"),
            _f("deal_type", "enum", True, values=["license_out", "license_in", "co_development", "option",
                                                   "acquisition", "cdmo_supply", "cro_service"]),
            _f("status", "enum", True, values=["exploring", "term_sheet", "signed", "terminated"]),
            _f("upfront_millions", "number", False),
            _f("milestones_millions", "number", False),
            _f("royalty_pct", "number", False, "0-1"),
            _f("signed_date", "date", False),
            _f("dealsim_name", "string", False, "Deal name in DealStructureSimulator"),
        ],
    },
    "partner": {
        "label": "Partner",
        "key": "partner_id",
        "fields": [
            _f("partner_id", "id", True),
            _f("name", "string", True),
            _f("kind", "enum", True, values=["cro", "cdmo", "licensee", "licensor", "academic", "investor", "other"]),
            _f("country", "string", False),
        ],
    },
    "ip_ref": {
        "label": "IP reference",
        "key": "ip_id",
        "fields": [
            _f("ip_id", "id", True),
            _f("asset_id", "ref", True, ref_entity="asset"),
            _f("kind", "enum", True, values=["invention_disclosure", "provisional", "pct", "national", "granted"]),
            _f("number", "string", False, "Application / patent number"),
            _f("title", "string", True),
            _f("priority_date", "date", False),
            _f("expiry_date", "date", False),
            _f("status", "enum", True, values=["draft", "filed", "published", "granted", "lapsed"]),
        ],
    },
}

#: Value-chain order used for the lineage view.
ENTITY_CHAIN = ["target", "asset", "indication", "study", "batch", "submission", "deal"]
#: Entities that hang off one asset (carry asset_id).
ASSET_SCOPED = ["indication", "study", "batch", "submission", "deal", "ip_ref"]


def entity_names() -> list[str]:
    return list(SCHEMA)


def key_of(entity: str) -> str:
    return SCHEMA[entity]["key"]


def field_names(entity: str) -> list[str]:
    return [f["name"] for f in SCHEMA[entity]["fields"]]


def as_json_schema() -> dict:
    """The schema published at GET /schema.json (CGTDataModel-compatible layout)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "id_pattern": ID_PATTERN.pattern,
        "entity_chain": ENTITY_CHAIN,
        "stages": ALL_STAGES,
        "modalities": MODALITIES,
        "stage_map": {k: dict(zip(STAGE_TARGETS, v)) for k, v in STAGE_MAP.items()},
        "entities": SCHEMA,
    }
