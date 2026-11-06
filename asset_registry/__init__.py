"""AssetProgramRegistry: one permanent Asset ID from target to deal.

The registry is the spine of the virtual pharma platform. Every program gets an
``AST-######`` id that every other tool (CDMO, CRO, valuation, regulatory, deal
tools) carries, and a lineage record that ties the asset to its target,
indications, studies, batches, filings, deals, partners and IP.
"""
from .schema import SCHEMA_VERSION, STAGES, ALL_STAGES, MODALITIES, map_stage, contract_modality
from .store import Registry, RegistryError, NotFound
from .resolve import resolve, harmonize

__version__ = "0.1.0"
__all__ = ["Registry", "RegistryError", "NotFound", "resolve", "harmonize", "map_stage",
           "contract_modality", "SCHEMA_VERSION", "STAGES", "ALL_STAGES", "MODALITIES", "__version__"]
