# AssetProgramRegistry

One permanent Asset ID from target to deal. This is the spine of the virtual pharma platform: every program gets an `AST-######` id when it is first nominated, and that id travels with it through discovery, IND, the clinic, filing and any licensing deal. The registry holds the lineage record that ties each asset to its target, indications, studies, batches, regulatory filings, deals, partners and IP, and a crosswalk so the CDMO, CRO, valuation and regulatory tools can keep their own ids while everyone agrees which program they mean. It is build 1 of 5 from the virtual pharma gap map (sheet "3 Builds ranked", row 1).

## What it links

| Existing repo | How it links | Where |
|---|---|---|
| CGTDataModel | Data contract. Imports programs and batches from its `GET /entities/<entity>.json` export, and exports an asset as its `POST /programs` body. Its `BAT-0001` batch ids are kept as `batch.cgt_batch_id`. | `asset_registry/adapters.py`, `examples/cgtdatamodel_export.json` |
| CDMOAgentContracts | Data contract. Modality and stage map onto the Common 1.0.0 `modality` enum and the CapacityRequest `phase`, so a capacity request can be filled from the Asset ID. | `schema.py` (`contract_modality`, `STAGE_MAP`), `adapters.to_capacity_request_fields` |
| AuditTrailService | HTTP. Every create, update and stage move is posted to `POST /log` as form fields against resource `asset:AST-000001`. If the service is down, events go to a local hash-chained spool (same sha256 chain as AuditTrailService) and are replayed later. | `asset_registry/hooks.py` |
| CDMOSemanticLayer | Data contract. `/harmonize` turns free text ("Phase II", "RMAT", "CAR-T") into the registry value plus the ontology class id (`clin:Phase2`, `reg:RMAT`, `ct:AutologousCARTCellTherapy`). | `asset_registry/resolve.py` |
| PTSPredictor | Data contract and crosswalk. Exports an asset in PTSPredictor's modality and stage vocabulary; its numeric asset ids resolve to Asset IDs. | `adapters.to_pts_profile` |
| thesis-generator, AssetValuationDataRoom | Data contract. Stage maps to thesis-generator's phase keys (`phase_1`, `ind`, `filing`), which the rNPV engine uses. | `schema.STAGE_MAP` |
| RegulatoryApprovalTimelinePredictor | Data contract. Regulatory profile (designations granted and planned, agencies). | `adapters.to_regulatory_profile` |
| DigitalThreadBuilder | Data contract. Studies, batches and filings as thread nodes tagged with the Asset ID. | `adapters.to_thread_nodes` |
| PharmaPlatformShell | HTTP header. The gateway passes the signed-in user as `X-Platform-User`; that user is written to the change log and audit trail. | `asset_registry/api.py` |
| ProgramDirectorAgent | HTTP. A passed stage gate calls `POST /assets/<id>/advance` with its gate run id, which is the only way a stage changes. | `api.py`, `store.advance_stage` |
| portfolio-agent | Service descriptor in the scaffold `agent.yaml` format, so the orchestrator can discover this capability. | `agent.yaml` |

Nothing imports his private repos, so CI runs on its own. Links are by documented JSON shape (fixtures in `examples/`) and HTTP.

## Quick start

```
pip install -e ".[dev]"
asset-registry seed                 # two fictional EXAMPLE assets
asset-registry lineage AST-000001
asset-registry serve                # http://127.0.0.1:5101
pytest -q
```

Environment: `ASSET_REGISTRY_DB` (default `asset_registry.db`), `AUDIT_TRAIL_URL` (e.g. `http://localhost:5056`), `AUDIT_SPOOL` (default `data/audit_spool.jsonl`), `REQUIRE_PLATFORM_USER=1` behind the gateway, `PORT` (5101).

## Data model

| Entity | Id | Belongs to |
|---|---|---|
| target | `TGT-` | |
| asset | `AST-` | target (optional) |
| indication | `IND-` | asset |
| study | `STY-` | asset, optional indication and partner |
| batch | `BCH-` | asset, optional partner |
| submission (filing or designation) | `SUB-` | asset |
| deal | `DEL-` | asset and partner |
| partner (CRO, CDMO, licensee...) | `PTN-` | |
| ip_ref | `IPR-` | asset |

Rules: ids are issued once and never reused; nothing is deleted (an asset is retired by moving it to `terminated`); every change is in an append-only change log with who, when, before, after and why; `asset.stage` only changes through `advance_stage`, forward and one step at a time unless the asset is in-licensed (`allow_skip`).

Stages: discovery, lead_optimization, candidate, ind_enabling, ind_filed, phase1, phase2, phase3, filed, approved, terminated.

| Registry stage | CGTDataModel | PTSPredictor | thesis-generator | CapacityRequest |
|---|---|---|---|---|
| discovery, lead_optimization, candidate | preclinical | preclinical | preclinical | preclinical |
| ind_enabling | preclinical | ind_enabling | preclinical | preclinical |
| ind_filed | preclinical | ind_enabling | ind | preclinical |
| phase1 / phase2 / phase3 | phase1 / 2 / 3 | phase1 / 2 / 3 | phase_1 / 2 / 3 | phase_1 / 2 / 3 |
| filed | phase3 | filed | filing | phase_3 |
| approved | commercial | none | approval | commercial |

The lineage record (`GET /assets/<id>/lineage`) is the shape every downstream repo consumes; see `examples/lineage_AST-000001.json`.

## API (port 5101)

| Method and path | What it does |
|---|---|
| `GET /healthz`, `GET /schema.json`, `GET /stats` | Health, schema with stage map, counts |
| `GET/POST /entities/<entity>` | List (filter by `asset_id` and any field) or create |
| `GET/PATCH /entities/<entity>/<id>` | Read or patch (stage, ids and owning asset are not patchable) |
| `GET /assets/<id>/lineage` | Target-to-deal record |
| `POST /assets/<id>/advance` | `{"to_stage","reason","gate_run_id"?,"allow_skip"?}` |
| `GET /assets/<id>/exports/<target>` | `cgt_program`, `pts_profile`, `regulatory_profile`, `capacity_request`, `thread_nodes` |
| `GET /resolve?q=` | Which asset a name, code, alias, target or other tool's id refers to |
| `GET /harmonize?term=&field=` | Free text to controlled value and ontology class |
| `GET/POST /external-ids`, `GET /external-ids/resolve` | Crosswalk |
| `GET /history?asset_id=` | Change log |
| `POST /import/cgtdatamodel` | Import a CGTDataModel export (idempotent) |

Errors: 422 with `{"error","errors":[...]}` for invalid data, 404 for unknown ids, 401 for a write without `X-Platform-User` when that is required.

## CLI

`asset-registry init | seed | add <entity> k=v ... | show | list | lineage | resolve | harmonize | advance | link | import-cgt | history | replay-audit | serve`. Run `asset-registry -h` for details.

## What's next

- Move from SQLite to Postgres once more than one person writes at the same time.
- Pull CGTDataModel and PTSPredictor directly over HTTP on a schedule instead of from an export file.
- Publish `AssetCreated` and `StageAdvanced` events onto PharmaEventBus as well as the audit trail.
- Field-level permissions from PharmaPlatformShell roles (for example, only regulatory can edit submissions).
