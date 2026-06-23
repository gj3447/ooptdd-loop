# OMD 브리지 — 요구사항 병렬 개발 조정

[OMD](https://github.com/gj3447/omd)(입체운행물방울 군단장)는 멀티에이전트
병렬 개발 코디네이터다. ooptdd-loop와의 연결점은 **Longinus 바인딩**이다.

## 아이디어

ooptdd-loop의 각 요구사항은 `longinus.source`(emit해야 할 소스 파일)를 가진다.
**그 source가 곧 그 요구사항을 개발할 때 건드리는 write-set**이다.

```yaml
requirements:
  - id: REQ-1
    longinus: { source: shop.py, symbol: authorize_payment, must_emit: payment_authorized }
```

여러 요구사항을 **병렬 에이전트(물방울)** 로 동시에 개발할 때, OMD는 이 write-set들을
**궤도(orbit)** 로 삼아 *서로소(입체)인 요구사항만 동시 운행*시키고(분열=merge conflict=0
사전 보장), 끝나면 CLOUD CONNECT(merge)로 합친다.

- write-set이 **서로소** → 같은 배치에서 병렬 개발 안전
- write-set이 **겹침** → 직렬화 (한쪽이 궤도를 쥐면 다른 쪽은 대기)
- Longinus 바인딩 **없음** → write-set 미상 → 보수적으로 직렬

## 사용

```bash
pip install -e /path/to/omd            # omd_server 필요 (opt-in)

# 어느 요구사항들을 병렬 개발해도 안전한지 배치 계획
python -m ooptdd_loop.omd_bridge requirements.yaml
# → {"parallel_batches": [["REQ-1","REQ-3"], ["REQ-2"]], "max_parallel": 2, ...}
```

```python
from ooptdd_loop.spec import load_spec
from ooptdd_loop.omd_bridge import parallel_batches, declare_to_omd
from omd_server import Coordinator

spec = load_spec("requirements.yaml")
parallel_batches(spec)                 # 병렬 안전 그룹

omd = Coordinator(repo="/path/to/repo")
declare_to_omd(omd, spec)              # 각 요구사항을 OMD task(궤도)로 선언
# 이후 물방울들이 omd.next_task()/claim()/start()/finish()/connect()로
# 서로소 요구사항을 충돌 없이 병렬 개발
```

## API (`ooptdd_loop.omd_bridge`)
- `requirement_writesets(spec) -> {req_id: [source_path...]}`
- `parallel_batches(spec) -> [[req_id...], ...]` — 같은 배치 = 병렬 안전(서로소)
- `declare_to_omd(omd, spec) -> [req_id...]` — OMD Coordinator에 요구사항을 task로 선언
