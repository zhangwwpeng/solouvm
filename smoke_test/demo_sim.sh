#!/usr/bin/env bash
DELAY="${SMOKE_DELAY:-0.12}"
for i in $(seq 1 10); do
  echo "UVM_INFO @ $((i*100))ns [AXI_MON] txn #${i} matched"
  if [ "$DELAY" != "0" ]; then
    sleep "$DELAY"
  fi
done
echo "--- UVM Report Summary ---"
echo "** Report counts by severity"
echo "UVM_INFO :    10"
echo "UVM_WARNING :    0"
echo "UVM_ERROR :    0"
echo "UVM_FATAL :    0"
echo "sim time: 50000ns"
