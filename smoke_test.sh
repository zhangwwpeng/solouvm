#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJ_DIR="${SCRIPT_DIR}/smoke_test"

if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
  PY="${SCRIPT_DIR}/.venv/bin/python"
else
  PY="python3"
fi

log_info() {
  PYTHONPATH="${SCRIPT_DIR}" "${PY}" -c 'import sys; from solouvm import info; info(sys.argv[1])' "$*"
}

log_error() {
  PYTHONPATH="${SCRIPT_DIR}" "${PY}" -c 'import sys; from solouvm import error; error(sys.argv[1])' "$*"
}

QUICK=0
for arg in "$@"; do
  case "$arg" in
    --quick)
      QUICK=1
      ;;
    --fast)
      export SMOKE_DELAY=0
      ;;
    -h|--help)
      cat <<'EOF'
Usage: ./smoke_test.sh [--quick] [--fast] [-h|--help]

Run an end-to-end solouvm showcase against ./smoke_test, exercising the 5 core
pipeline stages (gen -> push -> build -> run -> judge), A-side hooks, B-side
job hooks, live build/sim log streaming, and Rich live footer.

Options:
  --quick      Run only `regress --rebuild` (skip validate/list/cached run/--no-hooks/history)
  --fast       Set SMOKE_DELAY=0 so demo compile/sim/hook pauses are skipped
  -h, --help   Show this help message and exit
EOF
      exit 0
      ;;
    *)
      log_error "Unknown option: $arg"
      exit 2
      ;;
  esac
done

run_solouvm() {
  PYTHONPATH="${SCRIPT_DIR}" "${PY}" -m solouvm -c "${PROJ_DIR}" "$@"
}

if [ "${QUICK}" -eq 1 ]; then
  log_info "smoke_test: regress --rebuild (5 stages + hooks + live logs)"
  run_solouvm regress --rebuild
  exit 0
fi

log_info "smoke_test: solouvm validate"
run_solouvm validate

log_info "smoke_test: solouvm list"
run_solouvm list

log_info "smoke_test: solouvm regress --rebuild (5 stages + A/B hooks + live logs)"
run_solouvm regress --rebuild

log_info "smoke_test: solouvm run --case smoke (cached build + single-job live sim)"
run_solouvm run --case smoke

log_info "smoke_test: solouvm regress --no-hooks --case smoke (skip A-side hooks)"
run_solouvm regress --no-hooks --case smoke

log_info "smoke_test: solouvm history"
run_solouvm history
