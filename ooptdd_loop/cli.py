"""``ooptdd-loop`` — run the requirements loop once.

    ooptdd-loop run <spec.yaml> [--cid CID] [--kg-write] [--json] [--passes N]

Exit 0 = all requirements DONE (gate GREEN + Longinus bound); 1 = incomplete.
On incomplete it prints the log-grounded next-step context for the agent.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
from pathlib import Path
import sys

from .mcp_config import CLIENTS, check_configs, generated_configs
from .report import next_step_context, render
from .runner import run_until_complete
from .kg_seed import seed_cypher, seed_payload, write_seed
from .rules import rules_as_dicts
from .domain.spec import load_spec
from .tools import (
    t_golden_diff,
    t_golden_save,
    list_tools,
    t_harness_profile,
    t_logserver_errors,
    t_logserver_health,
    t_logserver_query,
    t_logserver_trace,
    t_validate_spec,
)


def _neo4j_store():
    from .kg import Neo4jKgStore  # env-gated; raises if NEO4J_* / driver absent
    return Neo4jKgStore()


def _cmd_run(args) -> int:
    spec = load_spec(args.spec)
    store = _neo4j_store() if args.kg else None
    run = run_until_complete(spec, cid=args.cid, max_passes=args.passes,
                             kg_write=args.kg_write, kg_store=store,
                             fix_cmd=args.fix, patience=args.patience,
                             backoff_s=args.backoff)
    if args.json:
        payload = {
            "cid": run.cid,
            "backend": run.backend,
            "complete": run.complete,
            "done": run.n_done,
            "total": len(run.results),
            "loop_reason": run.loop_reason,
            "transcript": [dataclasses.asdict(p) for p in run.transcript],
            "requirements": [
                {
                    "id": r.id,
                    "gate_ok": r.gate_ok,
                    "reachable": r.reachable,
                    "bound": r.bound,
                    "done": r.done,
                    "checks": r.checks,
                    "binding": dataclasses.asdict(r.binding) if r.binding else None,
                }
                for r in run.results
            ],
            "methodology_checks": [
                dataclasses.asdict(c) for c in run.methodology_checks
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(render(run))
        ctx = next_step_context(run)
        if ctx:
            print("\n" + ctx, file=sys.stderr)
    return 0 if run.complete else 1


def _cmd_rules(args) -> int:
    if args.cypher:
        print(seed_cypher())
        if args.params:
            print(json.dumps(seed_payload(), ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(rules_as_dicts(), ensure_ascii=False, indent=2))
    return 0


def _cmd_seed_kg(args) -> int:
    if args.dry_run:
        print(seed_cypher())
        print(json.dumps(seed_payload(), ensure_ascii=False, indent=2))
        return 0
    ok = write_seed(uri=args.uri, user=args.user, password=args.password)
    if not ok:
        print(
            "KG seed was not written. Set NEO4J_URI/NEO4J_PASSWORD or use --dry-run "
            "and run the Cypher through the workspace KG tool.",
            file=sys.stderr,
        )
        return 2
    print("OOPTDD methodology rules seeded into KG")
    return 0


def _cmd_tools(args) -> int:
    tools = list_tools()
    if args.json:
        print(json.dumps(tools, ensure_ascii=False, indent=2))
    else:
        for t in tools:
            print(f"{t['name']}: {t['description']}")
    return 0


def _cmd_validate_spec(args) -> int:
    payload = t_validate_spec(args.spec)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        status = "PASS" if payload["ok"] else "FAIL"
        enabled = "enabled" if payload["ooptdd_enabled"] else "disabled"
        print(f"{payload['spec']}: {status} (OOPTDD {enabled})")
        for c in payload["checks"]:
            if not c["passed"]:
                print(f"  {c['rule_id']}: {c['message']}")
    return 0 if payload["ok"] else 1


def _cmd_harness_profile(args) -> int:
    payload = t_harness_profile()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for layer, info in payload["layers"].items():
            print(f"{layer}: {info['purpose']}")
            print("  surfaces: " + ", ".join(info["surfaces"]))
    return 0


def _cmd_mcp(args) -> int:
    if args.check:
        from .log_mcp import display_mcp_url

        payload = {
            "server": "ooptdd-loop",
            "entrypoint": "ooptdd-loop-mcp",
            "module": "ooptdd_loop.mcp_server:main",
            "transport": "stdio",
            "logserver_upstream_mcp": display_mcp_url(),
            "tools": [t["name"] for t in list_tools()],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    from .mcp_server import main as mcp_main

    return mcp_main([])


def _selected_clients(client: str) -> tuple[str, ...]:
    return CLIENTS if client == "all" else (client,)


def _cmd_mcp_config(args) -> int:
    clients = _selected_clients(args.client)
    generated = generated_configs(
        clients=clients,
        server_name=args.server_name,
        command=args.command,
        module=args.module,
        cwd=args.cwd,
        pythonpath=args.pythonpath,
        oo_mcp_url=args.oo_mcp_url,
        include_cwd_for_claude=args.claude_cwd,
    )
    if args.check:
        paths = {"codex": args.codex_config, "claude": args.claude_config}
        payload = check_configs(
            clients=clients,
            generated=generated,
            paths=paths,
            require_cwd=args.require_cwd,
        )
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for name, report in payload["clients"].items():
                status = "PASS" if report["ok"] else "FAIL"
                print(f"{name}: {status} ({report['config_path']})")
                if report.get("error"):
                    print(f"  error: {report['error']}")
                for check in report.get("checks", []):
                    marker = "ok" if check["ok"] else "mismatch"
                    print(f"  {marker}: {check['field']}")
        return 0 if payload["ok"] else 1

    payload = {"clients": generated}
    text: str
    if args.json:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        blocks = []
        for client in clients:
            path = generated[client]["config_path"]
            header = (
                f"# Codex config fragment for {path}"
                if client == "codex"
                else f"# Claude settings.json fragment for {path}"
            )
            blocks.append(header + "\n" + generated[client]["snippet"].rstrip())
        text = "\n\n".join(blocks) + "\n"

    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def _cmd_coverage(args) -> int:
    cov = _neo4j_store().coverage(args.spec_name)
    print(json.dumps(cov, ensure_ascii=False, indent=2))
    return 0 if cov.get("total", 0) and cov.get("complete") else 1


def _cmd_drift(args) -> int:
    d = _neo4j_store().drift(args.spec_name)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    if d:
        print(f"DRIFT — {len(d)} Longinus binding(s) changed vs baseline", file=sys.stderr)
        return 1
    return 0


def _print_logserver_payload(payload: dict) -> int:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("reachable") else 2


def _cmd_logserver_health(args) -> int:
    return _print_logserver_payload(t_logserver_health(args.stale_minutes))


def _cmd_logserver_trace(args) -> int:
    return _print_logserver_payload(t_logserver_trace(args.cid, args.minutes_back))


def _cmd_logserver_query(args) -> int:
    return _print_logserver_payload(t_logserver_query(args.sql, args.minutes_back, args.size))


def _cmd_logserver_errors(args) -> int:
    return _print_logserver_payload(t_logserver_errors(args.minutes_back, args.stream))


def _cmd_golden_save(args) -> int:
    out = args.out
    if out is None:
        from .golden import default_golden_path

        out = default_golden_path(load_spec(args.spec))
    payload = t_golden_save(
        args.spec,
        out=out,
        cid=args.cid,
        run=args.run,
        allow_incomplete=args.allow_incomplete,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _cmd_golden_diff(args) -> int:
    payload = t_golden_diff(args.spec, baseline=args.baseline, cid=args.cid, run=args.run)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["status"] == "PASSED":
        return 0
    fail_on = {"REGRESSION", "TOOLS_CHANGED", "OUTPUT_CHANGED"} if args.strict else set(args.fail_on)
    return 1 if payload["status"] in fail_on else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ooptdd-loop")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the requirements loop once")
    r.add_argument("spec")
    r.add_argument("--cid")
    r.add_argument("--kg-write", action="store_true", help="write Longinus ReferenceSites to KG")
    r.add_argument("--kg", action="store_true", help="persist the run (verdicts+sites) to Neo4j")
    r.add_argument("--json", action="store_true")
    r.add_argument("--passes", type=int, default=1,
                   help="max fixpoint iterations (run→fix→re-run)")
    r.add_argument("--fix", default=None,
                   help="shell command run between RED passes to edit the code "
                        "(receives the RCA on stdin and via $OOPTDD_RCA); e.g. an agent")
    r.add_argument("--patience", type=int, default=2,
                   help="stop after this many consecutive no-progress passes (stall)")
    r.add_argument("--backoff", type=float, default=0.0,
                   help="seconds to sleep between passes (for async-ingest stores)")
    r.set_defaults(func=_cmd_run)
    rules = sub.add_parser("rules", help="print canonical OOPTDD methodology rules")
    rules.add_argument("--cypher", action="store_true", help="print KG seed Cypher")
    rules.add_argument("--params", action="store_true", help="also print Cypher params")
    rules.set_defaults(func=_cmd_rules)
    seed = sub.add_parser("seed-kg", help="MERGE OOPTDD rules into Neo4j")
    seed.add_argument("--dry-run", action="store_true")
    seed.add_argument("--uri")
    seed.add_argument("--user")
    seed.add_argument("--password")
    seed.set_defaults(func=_cmd_seed_kg)

    tools = sub.add_parser("tools", help="list the agent/MCP tool registry")
    tools.add_argument("--json", action="store_true")
    tools.set_defaults(func=_cmd_tools)

    val = sub.add_parser("validate-spec", help="validate OOPTDD methodology rules only")
    val.add_argument("spec")
    val.add_argument("--json", action="store_true")
    val.set_defaults(func=_cmd_validate_spec)

    hp = sub.add_parser("harness-profile", help="show L_IDE/L_RT/L_MC integration map")
    hp.add_argument("--json", action="store_true")
    hp.set_defaults(func=_cmd_harness_profile)

    mcp = sub.add_parser("mcp", help="run or inspect the MCP stdio server")
    mcp.add_argument("--check", action="store_true", help="print MCP server metadata and exit")
    mcp.set_defaults(func=_cmd_mcp)

    mc = sub.add_parser("mcp-config", help="generate or check Claude/Codex MCP config")
    mc.add_argument("--client", choices=["all", *CLIENTS], default="all")
    mc.add_argument("--check", action="store_true", help="check an existing client config")
    mc.add_argument("--json", action="store_true", help="print machine-readable JSON")
    mc.add_argument("--out", help="write generated output to this file")
    mc.add_argument("--server-name", default="ooptdd-loop")
    mc.add_argument("--command", default="python")
    mc.add_argument("--module", default="ooptdd_loop.mcp_server")
    mc.add_argument("--cwd", help="working directory for generated config; default finds repo root")
    mc.add_argument("--pythonpath", help="PYTHONPATH env value; default is the generated cwd")
    mc.add_argument("--oo-mcp-url", help="OO_MCP_URL env value; default resolves workspace oo-mcp")
    mc.add_argument("--claude-cwd", action="store_true", help="also include cwd in Claude JSON")
    mc.add_argument("--require-cwd", action="store_true", help="check cwd for every client")
    mc.add_argument("--codex-config", help="Codex config path; default ~/.codex/config.toml")
    mc.add_argument("--claude-config", help="Claude settings path; default ~/.claude/settings.json")
    mc.set_defaults(func=_cmd_mcp_config)

    c = sub.add_parser("coverage", help="query KG: which requirements are DONE (latest run)")
    c.add_argument("spec_name")
    c.set_defaults(func=_cmd_coverage)

    d = sub.add_parser("drift", help="query KG: Longinus bindings whose source sha256 changed")
    d.add_argument("spec_name")
    d.set_defaults(func=_cmd_drift)

    lh = sub.add_parser("logserver-health", help="query upstream log-server MCP health")
    lh.add_argument("--stale-minutes", type=float, default=15.0)
    lh.set_defaults(func=_cmd_logserver_health)

    lt = sub.add_parser("logserver-trace", help="query upstream log-server MCP trace_cycle")
    lt.add_argument("cid")
    lt.add_argument("--minutes-back", type=float, default=60.0)
    lt.set_defaults(func=_cmd_logserver_trace)

    lq = sub.add_parser("logserver-query", help="query upstream log-server MCP SQL")
    lq.add_argument("sql")
    lq.add_argument("--minutes-back", type=float, default=60.0)
    lq.add_argument("--size", type=int, default=100)
    lq.set_defaults(func=_cmd_logserver_query)

    le = sub.add_parser("logserver-errors", help="query upstream log-server MCP recent errors")
    le.add_argument("--minutes-back", type=float, default=60.0)
    le.add_argument("--stream")
    le.set_defaults(func=_cmd_logserver_errors)

    golden = sub.add_parser("golden", help="save or diff deterministic golden traces")
    golden_sub = golden.add_subparsers(dest="golden_cmd", required=True)
    gs = golden_sub.add_parser("save", help="save a golden baseline for a spec/cid")
    gs.add_argument("spec")
    gs.add_argument("--cid")
    gs.add_argument("--out", help="baseline path; defaults to .ooptdd/golden/<spec>.json")
    gs.add_argument("--run", action="store_true", help="run the spec before saving")
    gs.add_argument("--allow-incomplete", action="store_true",
                    help="allow saving a baseline from an incomplete run")
    gs.set_defaults(func=_cmd_golden_save)

    gd = golden_sub.add_parser("diff", help="compare a current run against a golden baseline")
    gd.add_argument("spec")
    gd.add_argument("baseline")
    gd.add_argument("--cid")
    gd.add_argument("--run", action="store_true", help="run the spec before diffing")
    gd.add_argument("--fail-on", nargs="+", default=["REGRESSION"],
                    choices=["REGRESSION", "TOOLS_CHANGED", "OUTPUT_CHANGED"])
    gd.add_argument("--strict", action="store_true", help="fail on any non-PASSED status")
    gd.set_defaults(func=_cmd_golden_diff)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
