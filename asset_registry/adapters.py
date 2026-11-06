"""Adapters between the registry and the tools that already exist.

Import (their ids -> our Asset ID, recorded in the crosswalk):
  * :func:`import_cgtdatamodel` - CGTDataModel programs and batches. From a sponsor's
    seat, a CGTDataModel *program* is one of our assets made at a CDMO and its
    *batches* are our batches; the CDMO's *client* is us, so it is not imported.

Export (our record -> the shape each tool already takes, so nobody retypes it):
  * :func:`to_cgt_program` - CGTDataModel ``POST /programs`` body
  * :func:`to_pts_profile` - PTSPredictor asset profile (modality + stage)
  * :func:`to_regulatory_profile` - RegulatoryApprovalTimelinePredictor-style profile
  * :func:`to_capacity_request_fields` - fields for a CDMOAgentContracts CapacityRequest
  * :func:`to_thread_nodes` - DigitalThreadBuilder nodes keyed on the Asset ID
"""
from __future__ import annotations

from typing import Optional

from . import schema as S
from .store import Registry, RegistryError

#: CGTDataModel program.modality -> our modality. car_t and the cell therapies are
#: unambiguous enough; plain "gene_therapy" needs the vector, so it defaults to AAV
#: and the import reports it as a warning to check.
CGT_TO_MODALITY = {
    "car_t": "autologous_cell_therapy",
    "cell_therapy": "allogeneic_cell_therapy",
    "gene_edited_cell_therapy": "allogeneic_cell_therapy",
    "gene_therapy": "gene_therapy_aav",
}
#: CGTDataModel program.phase -> our stage (preclinical is ambiguous; take ind_enabling,
#: since a program at a CDMO is past candidate selection).
CGT_PHASE_TO_STAGE = {
    "preclinical": "ind_enabling", "phase1": "phase1", "phase2": "phase2",
    "phase3": "phase3", "commercial": "approved",
}


def _records(export: dict, *keys: str) -> list[dict]:
    for k in keys:
        v = export.get(k)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("records"), list):  # /entities/<e>.json shape
            return v["records"]
    return []


def import_cgtdatamodel(reg: Registry, export: dict, *, source_system: str = "CGTDataModel",
                        cdmo_partner_id: Optional[str] = None, actor: str = "import:cgtdatamodel") -> dict:
    """Import CGTDataModel programs (as assets) and batches. Idempotent via the crosswalk.

    ``export`` holds lists under ``programs`` and ``batches`` (plain lists, or the
    ``{"records": [...]}`` bodies of CGTDataModel's ``GET /entities/<entity>.json``).
    """
    summary = {"assets_created": 0, "assets_matched": 0, "batches_created": 0,
               "batches_matched": 0, "warnings": []}
    for p in _records(export, "programs", "program"):
        pid = p.get("program_id")
        if not pid:
            summary["warnings"].append("program without program_id skipped")
            continue
        existing = reg.resolve_external(source_system, pid, "asset")
        if existing:
            summary["assets_matched"] += 1
            continue
        modality = CGT_TO_MODALITY.get(p.get("modality", ""), "other")
        if p.get("modality") == "gene_therapy":
            summary["warnings"].append(f"{pid}: CGTDataModel says gene_therapy; set AAV vs LVV on the asset")
        stage = CGT_PHASE_TO_STAGE.get(p.get("phase", ""), "ind_enabling")
        asset = reg.create("asset", {"name": p.get("name") or pid, "modality": modality,
                                     "stage": stage, "aliases": [pid]},
                           actor=actor, reason=f"imported from {source_system} program {pid}")
        reg.link_external(source_system, pid, asset["asset_id"], entity="asset", actor=actor)
        summary["assets_created"] += 1
    for b in _records(export, "batches", "batch"):
        bid, pid = b.get("batch_id"), b.get("program_id")
        if not bid or not pid:
            summary["warnings"].append("batch without batch_id/program_id skipped")
            continue
        if reg.resolve_external(source_system, bid, "batch"):
            summary["batches_matched"] += 1
            continue
        asset_id = reg.resolve_external(source_system, pid, "asset")
        if not asset_id:
            summary["warnings"].append(f"{bid}: program {pid} not imported; batch skipped")
            continue
        batch = reg.create("batch", {
            "asset_id": asset_id, "lot_number": b.get("lot_number") or bid,
            "purpose": "gmp_clinical", "status": b.get("status") or "in_process",
            "manufactured_at": b.get("manufactured_at"), "partner_id": cdmo_partner_id,
            "cgt_batch_id": bid}, actor=actor, reason=f"imported from {source_system} batch {bid}")
        reg.link_external(source_system, bid, batch["batch_id"], entity="batch", actor=actor)
        summary["batches_created"] += 1
    return summary


def _lead(lineage: dict) -> Optional[dict]:
    inds = lineage.get("indications") or []
    return next((i for i in inds if i.get("is_lead")), inds[0] if inds else None)


def to_cgt_program(lineage: dict, client_id: str, program_id: Optional[str] = None) -> dict:
    """Body for CGTDataModel ``POST /programs`` (for a CDMO that runs CGTDataModel)."""
    a = lineage["asset"]
    modality = S.CGT_MODALITY.get(a["modality"])
    if modality is None:
        raise RegistryError(f"CGTDataModel has no modality for {a['modality']}; it only covers cell and gene therapy")
    phase = S.map_stage(a["stage"], "cgtdatamodel")
    if phase is None:
        raise RegistryError(f"stage {a['stage']} has no CGTDataModel phase")
    ext = {e["source_system"]: e["external_id"] for e in lineage.get("external_ids", [])
           if e["entity"] == "asset"}
    return {"program_id": program_id or ext.get("CGTDataModel") or a["asset_id"],
            "client_id": client_id, "name": a["name"], "modality": modality, "phase": phase}


def to_pts_profile(lineage: dict) -> dict:
    """PTSPredictor asset profile: its modality and stage vocabularies plus our id."""
    a = lineage["asset"]
    modality = S.PTS_MODALITY.get(a["modality"])
    stage = S.map_stage(a["stage"], "ptspredictor")
    if modality is None:
        raise RegistryError(f"PTSPredictor has benchmarks for CAR-T, LVV and Allogeneic only, not {a['modality']}")
    if stage is None:
        raise RegistryError(f"stage {a['stage']} has no PTSPredictor equivalent")
    lead = _lead(lineage)
    return {"asset_id": a["asset_id"], "asset_name": a["name"], "modality": modality, "stage": stage,
            "indication": lead["name"] if lead else None}


def to_regulatory_profile(lineage: dict) -> dict:
    """Profile for approval-timeline tools: modality, stage, designations held or sought."""
    a = lineage["asset"]
    subs = lineage.get("submissions") or []
    lead = _lead(lineage)
    return {
        "asset_id": a["asset_id"], "asset_name": a["name"], "modality": a["modality"], "stage": a["stage"],
        "indication": lead["name"] if lead else None,
        "orphan": bool(lead and lead.get("orphan")),
        "designations_granted": sorted({s["designation"] for s in subs
                                        if s.get("designation") and s.get("status") == "granted"}),
        "designations_planned": sorted({s["designation"] for s in subs
                                        if s.get("designation") and s.get("status") in ("planned", "submitted")}),
        "agencies": sorted({s["agency"] for s in subs}),
    }


def to_capacity_request_fields(lineage: dict) -> dict:
    """The asset-derived part of a CDMOAgentContracts CapacityRequest 1.0.0."""
    a = lineage["asset"]
    return {"program_id": a["asset_id"], "modality": S.contract_modality(a["modality"]),
            "phase": S.map_stage(a["stage"], "capacity_request")}


def to_thread_nodes(lineage: dict) -> list[dict]:
    """DigitalThreadBuilder-style nodes, one per study, batch and submission, all tagged
    with the Asset ID so the thread stops keying on its own RD-/MFG- ids alone."""
    aid = lineage["asset_id"]
    nodes = []
    for s in lineage.get("studies", []):
        nodes.append({"id": s["study_id"], "asset_id": aid, "type": "study", "stage": "R&D", "title": s["title"]})
    for b in lineage.get("batches", []):
        nodes.append({"id": b["batch_id"], "asset_id": aid, "type": "batch", "stage": "GMP",
                      "title": f"Lot {b['lot_number']} ({b['purpose']})"})
    for s in lineage.get("submissions", []):
        nodes.append({"id": s["submission_id"], "asset_id": aid, "type": "submission", "stage": "Filing",
                      "title": f"{s['agency']} {s['kind']}" + (f" ({s['designation']})" if s.get("designation") else "")})
    return nodes
