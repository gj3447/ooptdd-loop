"""Render a RunResult for humans and for the next agent step."""
from __future__ import annotations

from .runner import RunResult


def render(run: RunResult) -> str:
    lines = [
        f"ooptdd-loop  cid={run.cid}  backend={run.backend}",
        f"requirements: {run.n_done}/{len(run.results)} DONE  "
        f"-> {'COMPLETE ✅' if run.complete else 'INCOMPLETE'}",
        "",
    ]
    for r in run.results:
        gate = "GREEN" if r.gate_ok else ("INCONCLUSIVE" if not r.reachable else "RED")
        bind = "—" if r.binding is None else ("bound" if r.binding.bound else "UNBOUND")
        flag = "✅" if r.done else "❌"
        lines.append(f"{flag} {r.id:10} gate={gate:13} longinus={bind:8} {r.description}")
        if not r.gate_ok:
            for c in r.checks:
                if not c["passed"]:
                    lines.append(
                        f"     gate miss: {c['event']} {c['op']} {c['want']} (got {c['got']})"
                    )
        if r.binding is not None and not r.binding.bound:
            lines.append(f"     longinus: {r.binding.reason}")
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
        if not r.gate_ok and r.rca:
            blocks.append(r.rca)
        if r.binding is not None and not r.binding.bound:
            blocks.append(f"Longinus UNBOUND: {r.binding.reason} "
                          f"(anchor {r.binding.kg_anchor} must point at the real emitter)")
        blocks.append("")
    return "\n".join(blocks)
