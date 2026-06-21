"""Render a RunResult for humans and for the next agent step."""
from __future__ import annotations

from .runner import RunResult


def _check_miss(c: dict) -> str:
    """One-line description of a failed gate check, for every check shape
    (event / where / must_order / selector)."""
    if "selector_order" in c:
        seq = ">".join(c["selector_order"])
        if c.get("missing"):
            return f"selector order [{seq}] — missing selectors {c['missing']}"
        if c.get("gaps_exceeded"):
            return f"selector order [{seq}] — gaps exceeded {c['gaps_exceeded']}"
        return f"selector order [{seq}] — selectors present but out of order"
    if "select" in c:
        target = c.get("label") or str(c["select"])
        if c.get("after") and not (c.get("causal") or {}).get("passed"):
            causal = c.get("causal") or {}
            return (
                f"selector {target} after {causal.get('predecessor')} — "
                f"predecessor_found={causal.get('predecessor_found')} "
                f"target_found={causal.get('target_found')}"
            )
        return f"selector {target} {c.get('op')} {c.get('want')} (got {c.get('got')})"
    if "conforms" in c:
        viols = c.get("violations") or []
        if not viols:
            return f"conforms {c['conforms']} — no ontology loaded"
        first = viols[0]
        probs = ", ".join(first.get("problems", []))
        ev = first.get("event", "?")
        more = f" (+{len(viols) - 1} more)" if len(viols) > 1 else ""
        return f"conforms {c['conforms']} — {ev}: {probs}{more}"
    if "absent" in c:
        # the negative wing: forbidden events (e.g. ERROR-level logs) that DID occur.
        # Surface the offenders so the agent gets the actual error logs, not just a count.
        labels = ",".join(c["absent"])
        offenders = c.get("offending") or []
        if offenders:
            shown = "; ".join(
                str(o.get("error") or o.get("message") or o.get("event") or o)[:160]
                for o in offenders[:3]
            )
            more = f" (+{c.get('violations', len(offenders)) - len(offenders[:3])} more)" \
                if c.get("violations", 0) > 3 else ""
            return f"forbidden [{labels}] — {c.get('violations')} occurred: {shown}{more}"
        return f"forbidden [{labels}] — {c.get('violations')} occurred"
    if "must_order" in c:
        seq = ">".join(c["must_order"])
        if c.get("missing"):
            return f"order [{seq}] — missing events {c['missing']}"
        return f"order [{seq}] — events present but out of order"
    target = c.get("event") or (
        "where:" + ",".join(f"{k}={v}" for k, v in (c.get("where") or {}).items())
    ) or "(any)"
    return f"{target} {c.get('op')} {c.get('want')} (got {c.get('got')})"


def render(run: RunResult) -> str:
    lines = [
        f"ooptdd-loop  cid={run.cid}  backend={run.backend}",
        f"requirements: {run.n_done}/{len(run.results)} DONE  "
        f"-> {'COMPLETE ✅' if run.complete else 'INCOMPLETE'}",
        "",
    ]
    if run.methodology_checks:
        passed = sum(1 for c in run.methodology_checks if c.passed)
        total = len(run.methodology_checks)
        lines.append(f"methodology: {passed}/{total} OOPTDD rules pass")
        for c in run.methodology_checks:
            if not c.passed:
                lines.append(f"     rule miss: {c.rule_id} — {c.message}")
        lines.append("")
    for r in run.results:
        gate = "GREEN" if r.gate_ok else ("INCONCLUSIVE" if not r.reachable else "RED")
        bind = "—" if r.binding is None else ("bound" if r.binding.bound else "UNBOUND")
        flag = "✅" if r.done else "❌"
        lines.append(f"{flag} {r.id:10} gate={gate:13} longinus={bind:8} {r.description}")
        if not r.gate_ok:
            for c in r.checks:
                if not c["passed"]:
                    lines.append("     gate miss: " + _check_miss(c))
        if r.binding is not None and not r.binding.bound:
            lines.append(f"     longinus: {r.binding.reason}")
    charge = getattr(run, "charge", None)
    if charge is not None and charge.enabled:
        lines.append("")
        lines.append(charge.summary())
    return "\n".join(lines)


def next_step_context(run: RunResult) -> str:
    """The block to hand the dev agent when something is RED — log-grounded,
    not a guess. Empty string when complete."""
    if run.complete:
        return ""
    blocks = ["NEXT STEP (agent): the following requirements are not DONE. "
              "Fix the code so the store actually receives the expected events; "
              "do not edit the spec to pass. Re-run the loop after each change.\n"]
    for r in run.results:
        if r.done:
            continue
        blocks.append(f"### {r.id} — {r.description}")
        if not r.gate_ok:
            for c in r.checks:
                if not c["passed"]:
                    blocks.append("gate miss: " + _check_miss(c))   # precise reason
            if r.rca:
                blocks.append(r.rca)                                 # aggregation context
        if r.binding is not None and not r.binding.bound:
            blocks.append(f"Longinus UNBOUND: {r.binding.reason} "
                          f"(anchor {r.binding.kg_anchor} must point at the real emitter)")
        blocks.append("")
    failed_rules = [c for c in run.methodology_checks if not c.passed]
    if failed_rules:
        blocks.append("### OOPTDD methodology rules")
        for c in failed_rules:
            blocks.append(f"{c.rule_id}: {c.message}")
        blocks.append("")
    return "\n".join(blocks)
