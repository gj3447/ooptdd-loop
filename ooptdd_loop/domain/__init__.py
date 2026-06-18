"""Domain layer — the requirement spec model (pure data, no IO).

:mod:`ooptdd_loop.domain.spec` holds the dataclasses an agent author writes (Spec, Target,
Requirement, Longinus, Contract, Methodology) and the loader. It imports nothing from the
engine or the adapters — the dependency arrow points toward it, never out of it.
"""
from .spec import (
    Contract,
    Longinus,
    Methodology,
    Requirement,
    Spec,
    Target,
    load_spec,
)

__all__ = [
    "Contract",
    "Longinus",
    "Methodology",
    "Requirement",
    "Spec",
    "Target",
    "load_spec",
]
