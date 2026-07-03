"""Architecture fitness gate — the layered dependency direction, as a fact about imports.

``ooptdd_loop`` is layered the same way the core library is:

    domain   (0)  the requirement spec model (ooptdd_loop.domain.*); imports only domain
    engine   (1)  binding + gate logic (ooptdd_loop.engine.*: longinus, selector_gates);
                  imports domain + the upstream ``ooptdd`` public API, never a loop adapter
    adapter  (2)  the application: runner, rules, oo_rca, log_mcp, kg, tools, cli, plugin, …
    api      (3)  the package __init__ (composition root)

The dependency arrow may only point toward an equal-or-lower layer. An edge that points UP
the stack (domain→engine, engine→runner, …) fails the build. This keeps the engine pure
logic and the domain free of IO — enforced deterministically from the import graph, not by
convention. (Self-contained, stdlib-only: the loop carries no extra test dependency.)
"""
from __future__ import annotations

import ast
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "ooptdd_loop"
ROOT = PKG.parent
_LAYER = {"domain": 0, "engine": 1}
_API = "ooptdd_loop"


def _discover() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(PKG.rglob("*.py")):
        parts = list(p.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = p
    return out


def _imports(path: Path, module: str, modules: set[str]) -> set[str]:
    is_init = path.name == "__init__.py"
    pkg = module if is_init else (module.rsplit(".", 1)[0] if "." in module else "")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    targets: set[str] = set()

    def record(name: str) -> None:
        parts = name.split(".")
        for i in range(len(parts), 0, -1):
            cand = ".".join(parts[:i])
            if cand in modules and cand != module:
                targets.add(cand)
                return

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            anchor = pkg.split(".")
            strip = node.level - 1
            if strip > 0:
                anchor = anchor[: max(0, len(anchor) - strip)]
            base = ".".join(anchor)
            base = f"{base}.{node.module}" if (base and node.module) else (node.module or base)
            if node.module:
                record(base)
            for alias in node.names:
                record(f"{base}.{alias.name}" if base else alias.name)
    return targets


def _layer_of(module: str) -> int:
    if module == _API:
        return 3
    parts = module.split(".")
    if len(parts) >= 2 and parts[0] == "ooptdd_loop" and parts[1] in _LAYER:
        return _LAYER[parts[1]]
    return 2


def test_layer_dependency_direction_is_respected():
    disc = _discover()
    modules = set(disc)
    violations = []
    for module, path in disc.items():
        src_layer = _layer_of(module)
        for dep in _imports(path, module, modules):
            if dep == _API:
                continue  # reading the package root (e.g. __version__) is not an up-edge
            if _layer_of(dep) > src_layer:
                violations.append(f"{module} (L{src_layer}) -> {dep} (L{_layer_of(dep)})")
    assert violations == [], "layering violated (an arrow points up the stack):\n" + "\n".join(
        sorted(violations)
    )


def test_engine_and_domain_do_not_import_loop_adapters():
    # the sharp rule: engine/domain never reach into the application layer (runner, kg,
    # log_mcp, cli, …). They depend down (domain) or out to the ooptdd public API only.
    disc = _discover()
    modules = set(disc)
    leaking = {}
    for module, path in disc.items():
        if _layer_of(module) >= 2:
            continue
        adapters = sorted(d for d in _imports(path, module, modules) if _layer_of(d) == 2)
        if adapters:
            leaking[module] = adapters
    assert leaking == {}, f"engine/domain imports an application adapter: {leaking}"
