#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON:-python}"
TMPROOT="$(mktemp -d)"
VENV="$TMPROOT/venv"
KEEP=0
INCLUDE_EXTERNAL=0
OOPTDD_PATH=""
NO_LOCAL_OOPTDD=0
EXTRAS="dev,mcp,kg,otel,xdist"

cleanup() {
  if (( KEEP )); then
    printf 'kept clean-install venv: %s\n' "$VENV"
  else
    rm -rf "$TMPROOT"
  fi
}
trap cleanup EXIT

usage() {
  cat <<'USAGE'
Usage: scripts/clean_install_smoke.sh [options]

Creates a fresh virtualenv, installs ooptdd-loop editable with extras, then runs
the local verification harness from that installed environment.

Options:
  --ooptdd-path PATH   install a local ooptdd dependency before ooptdd-loop
  --no-local-ooptdd    do not auto-detect a workspace-local ooptdd dependency
  --extras LIST        editable extras to install (default: dev,mcp,kg,otel,xdist)
  --include-external   pass through logserver/OpenObserve checks
  --keep               keep the temporary virtualenv for inspection
  -h, --help           show this help
USAGE
}

while (($#)); do
  case "$1" in
    --ooptdd-path)
      OOPTDD_PATH="${2:?missing path for --ooptdd-path}"
      shift 2
      ;;
    --no-local-ooptdd)
      NO_LOCAL_OOPTDD=1
      shift
      ;;
    --extras)
      EXTRAS="${2:?missing list for --extras}"
      shift 2
      ;;
    --include-external)
      INCLUDE_EXTERNAL=1
      shift
      ;;
    --keep)
      KEEP=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

step() {
  printf '\n==> %s\n' "$1"
}

run() {
  step "$1"
  shift
  "$@"
}

run_json() {
  local label="$1"
  local file="$2"
  shift 2
  step "$label"
  "$@" >"$file"
  "$VENV/bin/python" - "$file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as fh:
    json.load(fh)
PY
}

run "create venv" "$PYTHON_BIN" -m venv "$VENV"
run "upgrade installer" "$VENV/bin/python" -m pip install --upgrade pip

if [[ -z "$OOPTDD_PATH" && "$NO_LOCAL_OOPTDD" -eq 0 ]]; then
  for candidate in "$ROOT/../ooptdd" "$ROOT/../../ooptdd" "<WORKSPACE>/ooptdd"; do
    if [[ -f "$candidate/pyproject.toml" ]]; then
      OOPTDD_PATH="$candidate"
      break
    fi
  done
fi

if [[ -n "$OOPTDD_PATH" ]]; then
  run "install local ooptdd dependency" "$VENV/bin/python" -m pip install -e "$OOPTDD_PATH"
fi

run "install ooptdd-loop editable" "$VENV/bin/python" -m pip install -e ".[${EXTRAS}]"

step "installed package metadata"
"$VENV/bin/python" - <<'PY'
import importlib.metadata as md
import ooptdd
import ooptdd_loop

for dist in ("ooptdd", "ooptdd-loop"):
    print(f"{dist}=={md.version(dist)}")
print("ooptdd module:", ooptdd.__file__)
print("ooptdd_loop module:", ooptdd_loop.__file__)
PY

run_json "console script MCP metadata" "$TMPROOT/mcp-check.json" \
  "$VENV/bin/ooptdd-loop" mcp --check
run_json "console script tools metadata" "$TMPROOT/tools.json" \
  "$VENV/bin/ooptdd-loop" tools --json
run_json "MCP stdio roundtrip" "$TMPROOT/mcp-stdio-smoke.json" \
  "$VENV/bin/python" scripts/mcp_stdio_smoke.py --require --run

VERIFY_ARGS=()
if (( INCLUDE_EXTERNAL )); then
  VERIFY_ARGS+=(--include-external)
fi

step "run verification harness from clean venv"
PATH="$VENV/bin:$PATH" PYTHON="$VENV/bin/python" scripts/verify_ooptdd.sh "${VERIFY_ARGS[@]}"

printf '\nClean install smoke passed.\n'
