"""ooptdd-loop ↔ OMD 브리지 (입체운행물방울 군단장 연결).

ooptdd-loop의 각 요구사항은 **Longinus 바인딩**(emit해야 할 소스 심볼/파일)을 갖는다.
그 `longinus.source`가 곧 그 요구사항을 개발할 때 건드리는 **write-set**이다.

→ OMD(github.com/gj3447/omd)가 이를 **궤도(orbit)** 로 삼아,
   "어느 요구사항들을 병렬 에이전트(물방울)로 **동시 개발해도 분열(merge conflict)
   없이 안전한가**(=write-set 서로소/입체)"를 판정하고, claim/connect로 조정한다.

바인딩 없는 요구사항은 write-set 미상 → 보수적으로 직렬(아무와도 병렬 안 함).
이 모듈은 opt-in: `omd`(omd_server) 패키지가 설치돼 있어야 한다
(`pip install -e /path/to/omd`).
"""

from __future__ import annotations

from ooptdd_loop.spec import Spec, load_spec
from omd_server import sets_overlap


def requirement_writesets(spec: Spec) -> dict:
    """req.id → write-set(Longinus source 경로 리스트). 바인딩 없으면 []."""
    out = {}
    for r in spec.requirements:
        src = r.longinus.source if (r.longinus and r.longinus.source) else None
        out[r.id] = [src] if src else []
    return out


def _conflict(a, b) -> bool:
    if not a or not b:
        return True  # 바인딩 미상 = 안전하게 충돌 취급(직렬)
    return sets_overlap(a, b)


def parallel_batches(spec: Spec) -> list:
    """서로소(입체) write-set 요구사항을 같은 배치로 묶는다(greedy).
    같은 배치 안의 요구사항들은 write-set이 모두 서로소 → 병렬 개발 안전."""
    ws = requirement_writesets(spec)
    batches: list = []
    for rid in ws:
        for b in batches:
            if all(not _conflict(ws[rid], ws[o]) for o in b):
                b.append(rid)
                break
        else:
            batches.append([rid])
    return batches


def declare_to_omd(omd, spec: Spec) -> list:
    """각 요구사항을 OMD task로 선언(write-set = Longinus source).

    omd = omd_server.Coordinator (인프로세스) 또는 동일 API 클라이언트.
    이후 omd.next_task()/claim()/connect()로 병렬 물방울에 서로소 작업을 배정.
    """
    ws = requirement_writesets(spec)
    for r in spec.requirements:
        omd.declare(r.id, name=r.description, writes=ws[r.id])
    return list(ws)


def _main(argv=None):
    import json
    import sys
    path = (argv if argv is not None else sys.argv[1:])[0]
    spec = load_spec(path)
    batches = parallel_batches(spec)
    print(json.dumps({
        "requirements": len(spec.requirements),
        "parallel_batches": batches,
        "max_parallel": max((len(b) for b in batches), default=0),
        "writesets": requirement_writesets(spec),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
