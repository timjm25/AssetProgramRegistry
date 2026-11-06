"""``asset-registry`` command line.

  asset-registry init                          create the database
  asset-registry seed                          load the fictional EXAMPLE assets
  asset-registry add <entity> key=value ...    create a record (lists: aliases=a,b)
  asset-registry show <entity> <id>
  asset-registry list <entity> [--asset AST-000001]
  asset-registry lineage <AST-id>
  asset-registry resolve "<name or code>"
  asset-registry harmonize "<term>" [--field stage]
  asset-registry advance <AST-id> <stage> --reason "..." [--gate-run RUN] [--allow-skip]
  asset-registry link <source_system> <external_id> <canonical_id>
  asset-registry import-cgt export.json [--cdmo PTN-000002]
  asset-registry history [--asset AST-000001]
  asset-registry replay-audit                  resend events spooled while AuditTrailService was down
  asset-registry serve [--port 5101]

The database is ``--db`` or ``ASSET_REGISTRY_DB`` (default ``asset_registry.db``).
The acting user is ``--actor`` or ``PLATFORM_USER`` (default your login name).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from typing import Any, Optional

from . import adapters
from . import schema as S
from .resolve import harmonize, resolve
from .seed import seed
from .store import NotFound, Registry, RegistryError


def _coerce(entity: str, field: str, raw: str) -> Any:
    spec = next((f for f in S.SCHEMA[entity]["fields"] if f["name"] == field), None)
    if spec is None:
        raise RegistryError(f"{entity} has no field {field!r}")
    t = spec["type"]
    if t == "list":
        return [x.strip() for x in raw.split(",") if x.strip()]
    if t == "bool":
        return raw.lower() in ("1", "true", "yes", "y")
    if t == "integer":
        return int(raw)
    if t == "number":
        return float(raw)
    return raw


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, sort_keys=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="asset-registry", description="One permanent Asset ID from target to deal.")
    p.add_argument("--db", default=os.environ.get("ASSET_REGISTRY_DB", "asset_registry.db"))
    p.add_argument("--actor", default=os.environ.get("PLATFORM_USER"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("seed")
    a = sub.add_parser("add"); a.add_argument("entity"); a.add_argument("fields", nargs="*")
    a.add_argument("--reason")
    s = sub.add_parser("show"); s.add_argument("entity"); s.add_argument("id")
    ls = sub.add_parser("list"); ls.add_argument("entity"); ls.add_argument("--asset")
    ln = sub.add_parser("lineage"); ln.add_argument("asset_id")
    r = sub.add_parser("resolve"); r.add_argument("query"); r.add_argument("--limit", type=int, default=5)
    h = sub.add_parser("harmonize"); h.add_argument("term"); h.add_argument("--field")
    adv = sub.add_parser("advance"); adv.add_argument("asset_id"); adv.add_argument("to_stage")
    adv.add_argument("--reason", required=True); adv.add_argument("--gate-run")
    adv.add_argument("--allow-skip", action="store_true")
    lk = sub.add_parser("link"); lk.add_argument("source_system"); lk.add_argument("external_id")
    lk.add_argument("canonical_id")
    ic = sub.add_parser("import-cgt"); ic.add_argument("path"); ic.add_argument("--cdmo")
    hi = sub.add_parser("history"); hi.add_argument("--asset"); hi.add_argument("--limit", type=int, default=50)
    sub.add_parser("replay-audit")
    sv = sub.add_parser("serve"); sv.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5101)))
    sv.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    actor = args.actor or getpass.getuser() or "cli"
    if args.cmd == "replay-audit":
        from .hooks import AuditHook
        url = os.environ.get("AUDIT_TRAIL_URL")
        if not url:
            print("AUDIT_TRAIL_URL is not set", file=sys.stderr)
            return 2
        sent, left = AuditHook(url, os.environ.get("AUDIT_SPOOL", "data/audit_spool.jsonl")).replay()
        _print({"sent": sent, "still_spooled": left})
        return 0 if left == 0 else 1
    if args.cmd == "serve":  # pragma: no cover - runs a server
        os.environ["ASSET_REGISTRY_DB"] = args.db
        from .api import create_app
        create_app().run(host=args.host, port=args.port)
        return 0

    from .api import _default_hooks
    reg = Registry(args.db, on_change=_default_hooks())
    try:
        if args.cmd == "init":
            _print({"db": args.db, "schema_version": S.SCHEMA_VERSION, **reg.stats()})
        elif args.cmd == "seed":
            _print(seed(reg, actor=actor))
        elif args.cmd == "add":
            data = {}
            for kv in args.fields:
                if "=" not in kv:
                    raise RegistryError(f"expected key=value, got {kv!r}")
                k, v = kv.split("=", 1)
                data[k] = _coerce(args.entity, k, v)
            _print(reg.create(args.entity, data, actor=actor, reason=args.reason))
        elif args.cmd == "show":
            _print(reg.get(args.entity, args.id))
        elif args.cmd == "list":
            _print(reg.list(args.entity, asset_id=args.asset))
        elif args.cmd == "lineage":
            _print(reg.lineage(args.asset_id))
        elif args.cmd == "resolve":
            _print(resolve(reg, args.query, limit=args.limit))
        elif args.cmd == "harmonize":
            hit = harmonize(args.term, args.field)
            if not hit:
                print(f"no controlled value for {args.term!r}", file=sys.stderr)
                return 1
            _print(hit)
        elif args.cmd == "advance":
            _print(reg.advance_stage(args.asset_id, args.to_stage, actor=actor, reason=args.reason,
                                     gate_run_id=args.gate_run, allow_skip=args.allow_skip))
        elif args.cmd == "link":
            _print(reg.link_external(args.source_system, args.external_id, args.canonical_id, actor=actor))
        elif args.cmd == "import-cgt":
            with open(args.path, encoding="utf-8") as f:
                export = json.load(f)
            _print(adapters.import_cgtdatamodel(reg, export, cdmo_partner_id=args.cdmo, actor=actor))
        elif args.cmd == "history":
            _print(reg.history(asset_id=args.asset, limit=args.limit))
        return 0
    except (RegistryError, NotFound) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        reg.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
