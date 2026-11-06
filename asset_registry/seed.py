"""Fictional EXAMPLE data: one small-molecule asset and one CAR-T asset."""
from __future__ import annotations

from .store import Registry


def seed(reg: Registry, actor: str = "seed") -> dict:
    """Load two EXAMPLE assets with partners, studies, batches, filings and IP. Returns ids."""
    cro = reg.create("partner", {"name": "EXAMPLE Discovery CRO", "kind": "cro", "country": "US"}, actor=actor)
    cdmo = reg.create("partner", {"name": "EXAMPLE Cell Therapy CDMO", "kind": "cdmo", "country": "US"}, actor=actor)

    klk5 = reg.create("target", {"symbol": "KLK5", "name": "Kallikrein-related peptidase 5",
                                 "uniprot_id": "Q9Y337",
                                 "validation_evidence": "EXAMPLE: loss of LEKTI drives unchecked KLK5 activity"},
                      actor=actor)
    sm = reg.create("asset", {"name": "EXAMPLE-KLK5i", "code_name": "EX-101", "modality": "small_molecule",
                              "stage": "lead_optimization", "target_id": klk5["target_id"],
                              "owner": "EXAMPLE program lead", "aliases": ["oral KLK5 inhibitor"],
                              "description": "EXAMPLE oral KLK5 inhibitor for Netherton syndrome"}, actor=actor)
    a1 = sm["asset_id"]
    ns = reg.create("indication", {"asset_id": a1, "name": "Netherton syndrome", "doid": "DOID:0050474",
                                   "orphan": True, "is_lead": True, "peak_sales_millions": 450}, actor=actor)
    reg.create("study", {"asset_id": a1, "indication_id": ns["indication_id"], "kind": "in_vitro",
                         "title": "EXAMPLE KLK5 potency and selectivity panel", "partner_id": cro["partner_id"],
                         "status": "complete", "start_date": "2026-07-01", "end_date": "2026-09-15"}, actor=actor)
    reg.create("study", {"asset_id": a1, "kind": "in_vivo", "title": "EXAMPLE mouse PK", "phase": "preclinical",
                         "partner_id": cro["partner_id"], "status": "ongoing", "start_date": "2026-10-01"},
               actor=actor)
    reg.create("ip_ref", {"asset_id": a1, "kind": "provisional", "title": "EXAMPLE KLK5 inhibitor series",
                          "priority_date": "2026-10-20", "status": "filed"}, actor=actor)

    cd19 = reg.create("target", {"symbol": "CD19", "name": "B-lymphocyte antigen CD19"}, actor=actor)
    cart = reg.create("asset", {"name": "EXAMPLE-CD19 CAR-T", "code_name": "EX-201",
                                "modality": "autologous_cell_therapy", "stage": "phase1",
                                "target_id": cd19["target_id"]}, actor=actor)
    a2 = cart["asset_id"]
    reg.create("indication", {"asset_id": a2, "name": "Relapsed/refractory DLBCL", "is_lead": True,
                              "peak_sales_millions": 900}, actor=actor)
    reg.create("batch", {"asset_id": a2, "lot_number": "EX-LOT-001", "purpose": "gmp_clinical",
                         "status": "released", "manufactured_at": "2026-08-02",
                         "partner_id": cdmo["partner_id"], "cgt_batch_id": "BAT-0001"}, actor=actor)
    reg.create("submission", {"asset_id": a2, "kind": "designation", "agency": "FDA", "designation": "rmat",
                              "status": "granted", "decision_date": "2026-09-30"}, actor=actor)
    reg.create("submission", {"asset_id": a2, "kind": "bla", "agency": "FDA", "status": "planned",
                              "target_date": "2030-06-30"}, actor=actor)
    reg.link_external("PTSPredictor", "7", a2, actor=actor)
    return {"small_molecule": a1, "car_t": a2, "cro": cro["partner_id"], "cdmo": cdmo["partner_id"]}
