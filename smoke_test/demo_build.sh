#!/usr/bin/env bash
DELAY="${SMOKE_DELAY:-0.12}"
for i in $(seq 1 12); do
  echo "[VLOG] compiling module_${i}.sv (step ${i}/12)..."
  if [ "$DELAY" != "0" ]; then
    sleep "$DELAY"
  fi
done
touch simv
echo "[ELAB] top tb_top elaborated OK"
