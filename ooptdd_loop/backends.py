"""Process-level composition root for backend instances.

ooptdd a53e844 made backend state explicitly owned: ``get_backend()`` builds a
fresh registry per call, and a fresh ``MemoryStore`` with it. The loop's whole
premise is the opposite of per-call isolation — a ship anywhere in this process
and a judgment anywhere else must see the same evidence. Under the old library
default that sharing was an accident of a module-global store; under the new
contract it has to be owned by somebody, and that somebody is this module.

Every backend acquisition inside ooptdd_loop (and in its test suite) goes
through :func:`resolve`. For the ``memory`` backend it injects the single
process-owned :class:`~ooptdd.backends.MemoryStore`; every other backend is
passed through untouched, because their sharing is the external store itself.

Do not call ``ooptdd.backends.get_backend`` directly from loop code: two raw
calls silently stop sharing evidence, which is exactly the bug class the
2026-08-10 skew (33 red tests) was made of.
"""

from __future__ import annotations

from ooptdd.backends import MemoryStore, memory_reset
from ooptdd.backends import get_backend as _get_backend

_PROCESS_MEMORY_STORE = MemoryStore()


def resolve(name: str, **options):
    """Return a backend that shares this process's evidence.

    ``memory`` gets the process-owned store unless the caller explicitly
    supplies one (that explicit seam is how tests build isolated worlds).
    """
    if name == "memory":
        options.setdefault("store", _PROCESS_MEMORY_STORE)
    return _get_backend(name, **options)


def process_memory_store() -> MemoryStore:
    """The one store :func:`resolve` injects — for explicit-ownership call sites."""
    return _PROCESS_MEMORY_STORE


def reset_process_memory() -> None:
    """Clear the process store. The test-fixture replacement for the old
    argument-less ``memory_reset()``, which upstream turned into a no-op."""
    memory_reset(_PROCESS_MEMORY_STORE)
