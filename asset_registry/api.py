"""HTTP API (Flask, default port 5101).

Who did it: the acting user comes from the ``X-Platform-User`` header that
PharmaPlatformShell's gateway sets after sign-in. Set ``REQUIRE_PLATFORM_USER=1``
when the registry sits behind the gateway so anonymous writes are refused.

Endpoints
  GET  /healthz
  GET  /schema.json
  GET  /stats
  GET  /entities/<entity>?asset_id=&<field>=        list
  POST /entities/<entity>                           create  (body: record, optional "reason")
  GET  /entities/<entity>/<id>                      get
  PATCH /entities/<entity>/<id>                     update  (body: changes, optional "reason")
  GET  /assets/<asset_id>/lineage                   the record downstream tools consume
  POST /assets/<asset_id>/advance                   {"to_stage","reason","gate_run_id"?,"allow_skip"?}
  GET  /assets/<asset_id>/exports/<target>          cgt_program | pts_profile | regulatory_profile |
                                                    capacity_request | thread_nodes
  GET  /resolve?q=&limit=                           which asset does this name mean
  GET  /harmonize?term=&field=                      free text -> controlled value + ontology class
  GET  /external-ids?canonical_id=|asset_id=        crosswalk
  POST /external-ids                                {"source_system","external_id","canonical_id","entity"?}
  GET  /external-ids/resolve?source_system=&external_id=&entity=
  GET  /history?asset_id=|record_id=&limit=
  POST /import/cgtdatamodel                         CGTDataModel export {"programs":[...],"batches":[...]}
"""
from __future__ import annotations

import os
from typing import Any, Optional

from flask import Flask, jsonify, request

from . import adapters
from . import schema as S
from .resolve import harmonize, resolve
from .store import NotFound, Registry, RegistryError

_EXPORTS = {
    "cgt_program": lambda lin, args: adapters.to_cgt_program(
        lin, args.get("client_id", "CLI-0001"), args.get("program_id")),
    "pts_profile": lambda lin, args: adapters.to_pts_profile(lin),
    "regulatory_profile": lambda lin, args: adapters.to_regulatory_profile(lin),
    "capacity_request": lambda lin, args: adapters.to_capacity_request_fields(lin),
    "thread_nodes": lambda lin, args: adapters.to_thread_nodes(lin),
}


def create_app(registry: Optional[Registry] = None, *, require_user: Optional[bool] = None) -> Flask:
    """Build the app. Pass a Registry (tests) or let it open ``ASSET_REGISTRY_DB``."""
    app = Flask(__name__)
    reg = registry or Registry(os.environ.get("ASSET_REGISTRY_DB", "asset_registry.db"),
                               on_change=_default_hooks())
    if require_user is None:
        require_user = os.environ.get("REQUIRE_PLATFORM_USER", "0") == "1"
    app.config["REGISTRY"] = reg

    def actor() -> str:
        user = (request.headers.get("X-Platform-User") or "").strip()
        if not user:
            if require_user and request.method not in ("GET", "HEAD"):
                raise PermissionError("X-Platform-User header required (sign in through PharmaPlatformShell)")
            return "anonymous"
        return user

    def body() -> dict:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise RegistryError("request body must be a JSON object")
        return data

    @app.errorhandler(RegistryError)
    def _bad(e: RegistryError):
        return jsonify({"error": str(e), "errors": e.errors}), 422

    @app.errorhandler(NotFound)
    def _nf(e: NotFound):
        return jsonify({"error": str(e)}), 404

    @app.errorhandler(PermissionError)
    def _forbidden(e: PermissionError):
        return jsonify({"error": str(e)}), 401

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "service": "asset-registry", "schema_version": S.SCHEMA_VERSION})

    @app.get("/schema.json")
    def schema_json():
        return jsonify(S.as_json_schema())

    @app.get("/stats")
    def stats():
        return jsonify(reg.stats())

    @app.get("/entities/<entity>")
    def list_entities(entity: str):
        reg._check_entity(entity)
        filters: dict[str, Any] = {k: v for k, v in request.args.items()
                                   if k != "asset_id" and k in S.field_names(entity)}
        recs = reg.list(entity, asset_id=request.args.get("asset_id"), **filters)
        return jsonify({"entity": entity, "count": len(recs), "records": recs})

    @app.post("/entities/<entity>")
    def create_entity(entity: str):
        data = body()
        reason = data.pop("reason", None)
        return jsonify(reg.create(entity, data, actor=actor(), reason=reason)), 201

    @app.get("/entities/<entity>/<rid>")
    def get_entity(entity: str, rid: str):
        return jsonify(reg.get(entity, rid))

    @app.patch("/entities/<entity>/<rid>")
    def patch_entity(entity: str, rid: str):
        data = body()
        reason = data.pop("reason", None)
        return jsonify(reg.update(entity, rid, data, actor=actor(), reason=reason))

    @app.get("/assets/<asset_id>/lineage")
    def lineage(asset_id: str):
        return jsonify(reg.lineage(asset_id))

    @app.post("/assets/<asset_id>/advance")
    def advance(asset_id: str):
        data = body()
        if not data.get("to_stage"):
            raise RegistryError("to_stage is required")
        rec = reg.advance_stage(asset_id, data["to_stage"], actor=actor(), reason=data.get("reason", ""),
                                gate_run_id=data.get("gate_run_id"), allow_skip=bool(data.get("allow_skip")))
        return jsonify(rec)

    @app.get("/assets/<asset_id>/exports/<target>")
    def export(asset_id: str, target: str):
        fn = _EXPORTS.get(target)
        if not fn:
            raise RegistryError(f"unknown export {target!r}; one of {', '.join(_EXPORTS)}")
        return jsonify(fn(reg.lineage(asset_id), request.args))

    @app.get("/resolve")
    def resolve_q():
        limit = int(request.args.get("limit", 5))
        return jsonify({"query": request.args.get("q", ""),
                        "matches": resolve(reg, request.args.get("q", ""), limit=limit)})

    @app.get("/harmonize")
    def harmonize_q():
        hit = harmonize(request.args.get("term", ""), request.args.get("field"))
        if not hit:
            return jsonify({"error": "no controlled value for that term", "term": request.args.get("term")}), 404
        return jsonify(hit)

    @app.get("/external-ids")
    def ext_list():
        return jsonify({"external_ids": reg.external_ids(canonical_id=request.args.get("canonical_id"),
                                                         asset_id=request.args.get("asset_id"))})

    @app.post("/external-ids")
    def ext_link():
        d = body()
        row = reg.link_external(d.get("source_system", ""), d.get("external_id", ""), d.get("canonical_id", ""),
                                entity=d.get("entity"), actor=actor())
        return jsonify(row), 201

    @app.get("/external-ids/resolve")
    def ext_resolve():
        cid = reg.resolve_external(request.args.get("source_system", ""), request.args.get("external_id", ""),
                                   request.args.get("entity"))
        if not cid:
            return jsonify({"error": "not linked"}), 404
        return jsonify({"canonical_id": cid})

    @app.get("/history")
    def history():
        return jsonify({"changes": reg.history(asset_id=request.args.get("asset_id"),
                                               record_id=request.args.get("record_id"),
                                               limit=int(request.args.get("limit", 500)))})

    @app.post("/import/cgtdatamodel")
    def import_cgt():
        d = body()
        return jsonify(adapters.import_cgtdatamodel(reg, d, cdmo_partner_id=d.get("cdmo_partner_id"),
                                                    actor=actor())), 200

    return app


def _default_hooks() -> list:
    """AuditTrailService hook when AUDIT_TRAIL_URL is set; always spools locally if it is down."""
    from .hooks import AuditHook
    url = os.environ.get("AUDIT_TRAIL_URL")
    spool = os.environ.get("AUDIT_SPOOL", "data/audit_spool.jsonl")
    return [AuditHook(url, spool)] if url or os.environ.get("AUDIT_SPOOL") else []


def main() -> None:  # pragma: no cover - thin wrapper
    port = int(os.environ.get("PORT", 5101))
    create_app().run(host=os.environ.get("HOST", "127.0.0.1"), port=port)


if __name__ == "__main__":  # pragma: no cover
    main()
