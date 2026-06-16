"""Requirement spec — the Red artifact, written before the code satisfies it.

A spec file declares the target (how to produce logs) and a list of requirements.
Each requirement is a trace gate (expected events) plus a Longinus binding (which
source symbol is supposed to emit them). The gate is the machine verdict; the
binding is what keeps the verdict honest — it must point at code that exists.

    target:
      mode: in_process            # in_process | command
      callable: shop:run_pipeline # in_process: module:function(backend, cid)
      # command: "pytest -q"      # command: a shell command that ships to the store
      backend: memory             # memory | openobserve | otel | <entrypoint>
      root: .                     # source root for Longinus checks

    requirements:
      - id: REQ-1
        description: payment is authorized exactly once
        gate:
          - {event: payment_authorized, op: "==", count: 1}
        longinus:
          kg_anchor: ref_site:shop:orders:payment
          source: shop.py
          symbol: authorize_payment
          must_emit: payment_authorized
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Longinus:
    kg_anchor: str
    source: str
    symbol: str
    must_emit: str


@dataclass
class Methodology:
    name: str = ""
    enforce: bool = False


@dataclass
class Contract:
    id: str
    kind: str = "message_contract"
    description: str = ""
    role: str = ""
    sender: str = ""
    receiver: str = ""
    message: str = ""
    status: str = "candidate"
    source_req: str = ""
    integration_backstop: str = ""
    is_domain_message: bool = True
    extras: dict = field(default_factory=dict)


@dataclass
class Requirement:
    id: str
    description: str
    gate: list[dict]
    longinus: Longinus | None = None
    kind: str = "guiding"
    covers: list[str] = field(default_factory=list)
    extras: dict = field(default_factory=dict)


@dataclass
class Target:
    mode: str = "in_process"       # in_process | command
    callable: str | None = None    # "module:function"
    command: str | None = None
    backend: str = "memory"
    root: str = "."
    ontology: str | None = None    # path (relative to root) to an event ontology yaml
    backend_options: dict = field(default_factory=dict)
    capture: dict = field(default_factory=dict)


@dataclass
class Spec:
    target: Target
    requirements: list[Requirement]
    name: str = "spec"          # identifies this requirement set in the KG
    methodology: Methodology = field(default_factory=Methodology)
    contracts: list[Contract] = field(default_factory=list)


def _extras(row: dict, known: set[str]) -> dict:
    return {k: v for k, v in row.items() if k not in known}


def _capture_config(raw) -> dict:
    if raw is True:
        return {"logging": True}
    if isinstance(raw, str):
        return {raw: True}
    return dict(raw or {})


def load_spec(path: str) -> Spec:
    import os

    import yaml

    with open(path) as fh:
        data = yaml.safe_load(fh) or {}
    t = data.get("target", {})
    methodology_data = data.get("methodology") or {}
    target = Target(
        mode=t.get("mode", "in_process"),
        callable=t.get("callable"),
        command=t.get("command"),
        backend=t.get("backend", "memory"),
        root=t.get("root", "."),
        ontology=t.get("ontology"),
        backend_options=dict(t.get("backend_options", {})),
        capture=_capture_config(t.get("capture")),
    )
    methodology = Methodology(
        name=methodology_data.get("name", ""),
        enforce=bool(methodology_data.get("enforce", False)),
    )
    contract_known = {
        "id", "kind", "description", "role", "sender", "receiver", "message",
        "status", "source_req", "integration_backstop", "is_domain_message",
    }
    contracts = []
    for c in data.get("contracts", []):
        contracts.append(
            Contract(
                id=c["id"],
                kind=c.get("kind", "message_contract"),
                description=c.get("description", ""),
                role=c.get("role", ""),
                sender=c.get("sender", ""),
                receiver=c.get("receiver", ""),
                message=c.get("message", ""),
                status=c.get("status", "candidate"),
                source_req=c.get("source_req", ""),
                integration_backstop=c.get("integration_backstop", ""),
                is_domain_message=bool(c.get("is_domain_message", True)),
                extras=_extras(c, contract_known),
            )
        )
    reqs = []
    req_known = {
        "id", "description", "gate", "longinus", "kind", "covers",
    }
    for r in data.get("requirements", []):
        lon = r.get("longinus")
        reqs.append(
            Requirement(
                id=r["id"],
                description=r.get("description", ""),
                gate=r.get("gate", []),
                longinus=Longinus(**lon) if lon else None,
                kind=r.get("kind", "guiding"),
                covers=list(r.get("covers", [])),
                extras=_extras(r, req_known),
            )
        )
    if not reqs:
        raise ValueError(f"{path}: no requirements declared")
    name = data.get("name") or os.path.splitext(os.path.basename(path))[0]
    return Spec(
        target=target,
        requirements=reqs,
        name=name,
        methodology=methodology,
        contracts=contracts,
    )
