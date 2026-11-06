"""Entity resolution and term harmonization.

Two jobs that every tool keying on its own names needs:

* :func:`resolve` answers "which asset is this?" for a free-text name, code, alias,
  target symbol or another tool's id. Exact matches win; otherwise a fuzzy score
  (token overlap + sequence similarity, the same normalisation idea as
  CDMOSemanticLayer's ``semantic/model.py`` ``resolve``) ranks candidates.
* :func:`harmonize` maps a free-text term ("Phase I", "RMAT designation", "CAR-T",
  "lentiviral") to this registry's controlled value **and** the CDMOSemanticLayer
  ontology class id (``clin:Phase1``, ``reg:RMAT``, ``ct:AutologousCARTCellTherapy``),
  so the registry speaks the semantic layer's vocabulary without importing it.
"""
from __future__ import annotations

import difflib
import re
from typing import Optional

from .schema import ID_PATTERN
from .store import Registry

_PUNCT = re.compile(r"[^a-z0-9]+")


def normalize(text: str) -> str:
    """Lowercase, punctuation to spaces, collapse whitespace."""
    return " ".join(_PUNCT.sub(" ", (text or "").lower()).split())


def _tokens(text: str) -> set[str]:
    return set(normalize(text).split())


def _fuzzy(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    seq = difflib.SequenceMatcher(None, na.replace(" ", ""), nb.replace(" ", "")).ratio()
    return round(max(jacc, seq), 3)


def resolve(registry: Registry, query: str, *, limit: int = 5, min_score: float = 0.6) -> list[dict]:
    """Rank assets that ``query`` could refer to. Score 1.0 = exact."""
    q = (query or "").strip()
    if not q:
        return []
    if ID_PATTERN.match(q.upper()) and q.upper().startswith("AST-"):
        try:
            a = registry.get("asset", q.upper())
            return [{"asset_id": a["asset_id"], "name": a["name"], "score": 1.0, "matched_on": "asset_id"}]
        except KeyError:
            return []
    hits: dict[str, dict] = {}

    def add(asset: dict, score: float, how: str) -> None:
        cur = hits.get(asset["asset_id"])
        if cur is None or score > cur["score"]:
            hits[asset["asset_id"]] = {"asset_id": asset["asset_id"], "name": asset["name"],
                                       "score": score, "matched_on": how}

    # another tool's id
    for row in registry.external_ids():
        if row["external_id"].lower() == q.lower():
            aid = row["canonical_id"] if row["entity"] == "asset" else registry._asset_of(row["entity"], row["canonical_id"])
            if aid:
                add(registry.get("asset", aid), 1.0, f"external_id:{row['source_system']}")
    nq = normalize(q)
    targets = {t["target_id"]: t for t in registry.list("target")}
    for a in registry.list("asset"):
        names = [("name", a["name"])] + ([("code_name", a["code_name"])] if a.get("code_name") else []) \
            + [("alias", x) for x in a.get("aliases", [])]
        for how, n in names:
            if normalize(n) == nq:
                add(a, 1.0, how)
            else:
                s = _fuzzy(q, n)
                if s >= min_score:
                    add(a, s, f"{how}~")
        t = targets.get(a.get("target_id") or "")
        if t and normalize(t["symbol"]) in _tokens(q):
            add(a, 0.8, "target_symbol")
    return sorted(hits.values(), key=lambda h: (-h["score"], h["asset_id"]))[:limit]


# field, canonical value, CDMOSemanticLayer class id, synonyms
_TERMS: list[tuple[str, str, Optional[str], list[str]]] = [
    # development stage (asset.stage / study.phase)
    ("stage", "discovery", None, ["discovery", "target discovery", "hit finding", "hit to lead"]),
    ("stage", "lead_optimization", None, ["lead optimization", "lead optimisation", "lead op", "LO"]),
    ("stage", "candidate", None, ["candidate", "development candidate", "DC", "clinical candidate", "candidate selection"]),
    ("stage", "ind_enabling", None, ["IND enabling", "IND-enabling", "GLP tox", "preclinical development"]),
    ("stage", "ind_filed", "reg:IND", ["IND filed", "IND submitted", "IND cleared"]),
    ("stage", "phase1", "clin:Phase1", ["phase 1", "phase I", "ph1", "Ph 1", "first in human", "FIH"]),
    ("stage", "phase2", "clin:Phase2", ["phase 2", "phase II", "ph2", "Ph 2", "proof of concept"]),
    ("stage", "phase3", "clin:Phase3", ["phase 3", "phase III", "ph3", "Ph 3", "pivotal"]),
    ("stage", "filed", None, ["filed", "BLA filed", "NDA filed", "under review", "registration"]),
    ("stage", "approved", None, ["approved", "marketed", "commercial", "launched"]),
    # designations (submission.designation)
    ("designation", "rmat", "reg:RMAT", ["RMAT", "regenerative medicine advanced therapy", "RMAT designation"]),
    ("designation", "breakthrough", "reg:BreakthroughTherapyDesignation", ["breakthrough", "BTD", "breakthrough therapy", "breakthrough therapy designation"]),
    ("designation", "fast_track", "reg:FastTrackDesignation", ["fast track", "FTD", "fast track designation"]),
    ("designation", "orphan", "reg:OrphanDrugDesignation", ["orphan", "ODD", "orphan drug", "orphan designation", "orphan drug designation"]),
    ("designation", "priority_review", "reg:PriorityReview", ["priority review", "PR designation"]),
    # submission kinds
    ("submission_kind", "ind", "reg:IND", ["IND", "investigational new drug"]),
    ("submission_kind", "cta", "reg:IMPD", ["CTA", "clinical trial application", "IMPD"]),
    ("submission_kind", "bla", "reg:BLA", ["BLA", "biologics license application"]),
    ("submission_kind", "nda", "reg:NDA", ["NDA", "new drug application"]),
    ("submission_kind", "maa", "reg:MAA", ["MAA", "marketing authorisation application", "marketing authorization application"]),
    ("submission_kind", "pre_ind", None, ["pre-IND", "pre IND meeting", "type B meeting", "INTERACT"]),
    # agencies
    ("agency", "FDA", "reg:FDA", ["FDA", "CBER", "CDER", "US FDA"]),
    ("agency", "EMA", "reg:EMA", ["EMA", "CHMP", "European Medicines Agency"]),
    ("agency", "MHRA", "reg:MHRA", ["MHRA"]),
    ("agency", "PMDA", "reg:PMDA", ["PMDA", "Japan"]),
    # modalities
    ("modality", "autologous_cell_therapy", "ct:AutologousCARTCellTherapy",
     ["CAR-T", "CAR T", "autologous CAR-T", "autologous cell therapy", "TIL", "TCR-T"]),
    ("modality", "allogeneic_cell_therapy", "ct:AllogeneicTherapy",
     ["allogeneic", "allo CAR-T", "off the shelf cell therapy", "iPSC derived", "NK cell therapy", "MSC"]),
    ("modality", "gene_therapy_aav", "gt:AAV", ["AAV", "AAV gene therapy", "adeno-associated virus"]),
    ("modality", "gene_therapy_lvv", "gt:LentiviralVector", ["LVV", "lentiviral", "lentiviral gene therapy"]),
    ("modality", "monoclonal_antibody", None, ["mAb", "monoclonal antibody", "antibody", "bispecific"]),
    ("modality", "adc", None, ["ADC", "antibody drug conjugate"]),
    ("modality", "mrna", None, ["mRNA"]),
    ("modality", "small_molecule", None, ["small molecule", "SM", "oral small molecule", "NCE"]),
    ("modality", "oligonucleotide", None, ["oligonucleotide", "ASO", "siRNA", "antisense"]),
    ("modality", "peptide", None, ["peptide", "macrocycle"]),
    # partner kinds
    ("partner_kind", "cro", "cdmo:CRO", ["CRO", "contract research organization"]),
    ("partner_kind", "cdmo", "cdmo:CDMO", ["CDMO", "CMO", "contract manufacturer"]),
]
_INDEX = {(field, normalize(s)): (value, cls) for field, value, cls, syns in _TERMS for s in syns + [value]}


def harmonize(term: str, field: Optional[str] = None) -> Optional[dict]:
    """Map a free-text term to ``{field, value, ontology_class}``; None if unknown.

    Pass ``field`` (stage, designation, submission_kind, agency, modality,
    partner_kind) when a term is ambiguous, e.g. "IND" as a stage vs a filing.
    """
    n = normalize(term)
    fields = [field] if field else [f for f, *_ in _TERMS]
    for f in dict.fromkeys(fields):
        hit = _INDEX.get((f, n))
        if hit:
            return {"field": f, "value": hit[0], "ontology_class": hit[1], "term": term}
    return None
