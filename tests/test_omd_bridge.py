"""omd_bridge: 요구사항 Longinus 바인딩 → OMD write-set → 병렬 안전 배치."""

import pytest

pytest.importorskip("omd_server")  # omd 미설치 시 skip

from ooptdd_loop.spec import load_spec
from ooptdd_loop.omd_bridge import (
    requirement_writesets, parallel_batches, declare_to_omd,
)

YAML = """
name: t
target:
  mode: command
  command: "true"
  backend: memory
  root: .
requirements:
  - id: A
    description: a
    gate: [{event: e, op: ">=", count: 1}]
    longinus: {kg_anchor: k, source: src/a.py, symbol: f, must_emit: e}
  - id: B
    description: b
    gate: [{event: e, op: ">=", count: 1}]
    longinus: {kg_anchor: k, source: src/b.py, symbol: g, must_emit: e}
  - id: C
    description: c
    gate: [{event: e, op: ">=", count: 1}]
    longinus: {kg_anchor: k, source: src/a.py, symbol: h, must_emit: e}
  - id: D
    description: unbound
    gate: [{event: e, op: ">=", count: 1}]
"""


def _spec(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(YAML)
    return load_spec(str(p))


def test_writesets(tmp_path):
    assert requirement_writesets(_spec(tmp_path)) == {
        "A": ["src/a.py"], "B": ["src/b.py"], "C": ["src/a.py"], "D": [],
    }


def test_parallel_batches(tmp_path):
    batches = parallel_batches(_spec(tmp_path))
    batch_of = {r: i for i, b in enumerate(batches) for r in b}
    assert batch_of["A"] == batch_of["B"]            # 서로소 → 병렬 안전
    assert batch_of["C"] != batch_of["A"]            # src/a.py 겹침 → 분리
    assert [b for b in batches if "D" in b] == [["D"]]  # 미바인딩 → 단독(직렬)


def test_declare_to_omd_coordinates(tmp_path):
    from omd_server import Coordinator
    omd = Coordinator()
    ids = declare_to_omd(omd, _spec(tmp_path))
    assert set(ids) == {"A", "B", "C", "D"}
    omd.claim("ag1", ["src/a.py"], task_id="A")                      # A 점유
    assert omd.claim("ag2", ["src/b.py"], task_id="B")["state"] == "HELD"     # 서로소
    assert omd.claim("ag3", ["src/a.py"], task_id="C")["state"] == "PENDING"  # 겹침→대기
