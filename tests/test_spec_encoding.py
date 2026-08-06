"""스펙 파일은 로케일이 아니라 UTF-8 로 읽힌다.

YAML 은 명세상 UTF-8 이다(YAML 1.2 §5.2). 그러니 스펙 파일의 인코딩은 **지역
설정이 정할 문제가 아니다.** 그런데 `load_spec` 이 `open(path)` 로 열면
파이썬은 로케일 코덱을 쓰고, 한글이 든 description 한 줄에서 죽는다.

실측 2026-08-07 (beadscan_tester, 한국어 Windows 11):

    UnicodeDecodeError: 'cp949' codec can't decode byte 0xed in position 22

한 줄이 스위트의 **1287 중 122건**을 죽였다 — `*_loop_complete` 와
`*_golden_no_regression` 게이트 전부. 리눅스 개발기에서는 로케일이 UTF-8 이라
영원히 초록이었다.

이 시험은 그 조건을 리눅스에서 재현한다: `LC_ALL=C` + `PYTHONCOERCECLOCALE=0`
으로 자식 프로세스의 기본 인코딩을 ANSI_X3.4-1968(ascii) 로 떨어뜨린다. 즉
한국어 Windows 가 없어도 이 결함은 여기서 붉어진다.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

SPEC = """\
name: hangul-spec
target:
  mode: in_process
  callable: nothing:run_pipeline
  backend: memory
requirements:
  - id: REQ-KO
    description: 한글 설명이 들어간 요구사항 — 인코딩이 로케일에 좌우되면 안 된다
    gate:
      - {event: paid, op: "==", count: 1}
"""

_LOADER = textwrap.dedent("""
    import locale, sys
    from ooptdd_loop.domain.spec import load_spec
    spec = load_spec(sys.argv[1])
    # 판정은 **자식 안에서** 한다 — 한글을 ascii stdout 으로 인쇄하려 들면
    # 라이브러리가 아니라 프로브가 죽고, 그러면 무엇이 고장났는지 흐려진다.
    ok = spec.requirements[0].description.startswith("\\ud55c\\uae00")
    print("PREFERRED", locale.getpreferredencoding(False))
    print("DESC_OK", ok)
""")


def _load_under_ascii_locale(tmp_path):
    spec_path = tmp_path / "hangul.yaml"
    spec_path.write_text(SPEC, encoding="utf-8")
    script = tmp_path / "load.py"
    script.write_text(_LOADER, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(script), str(spec_path)],
        capture_output=True, text=True, timeout=60,
        env={"LC_ALL": "C", "LANG": "C",
             "PYTHONCOERCECLOCALE": "0",   # PEP 538 자동 승격을 끈다
             "PYTHONUTF8": "0",            # PEP 540 UTF-8 모드도 끈다
             "PATH": "/usr/bin:/bin",
             "PYTHONPATH": ":".join(sys.path)})


def test_hangul_spec_loads_when_the_locale_is_not_utf8(tmp_path):
    proc = _load_under_ascii_locale(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "DESC_OK True" in proc.stdout, proc.stdout


def test_the_probe_really_runs_under_a_non_utf8_locale(tmp_path):
    """전제 검사 — 자식이 UTF-8 로 떨어지면 위 시험은 아무것도 안 잰다."""
    proc = _load_under_ascii_locale(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    preferred = next(
        (line.split(" ", 1)[1] for line in proc.stdout.splitlines()
         if line.startswith("PREFERRED")), "")
    assert "UTF-8" not in preferred.upper(), (
        f"자식 인코딩이 {preferred!r} 라 비-UTF8 조건이 성립하지 않았다 — "
        "이 파일의 두 시험은 공허하다")
