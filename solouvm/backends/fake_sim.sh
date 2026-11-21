#!/usr/bin/env bash
# solouvm fake simulator (S-06, Q-01).
# Select behaviour with +SOLOUVM_FAKE_MODE=<mode>; default is "pass".
# All output goes to stdout (run_job.sh redirects it into sim.log).
set -u

mode=pass
scb_args=()
for arg in "$@"; do
    case "$arg" in
        +SOLOUVM_FAKE_MODE=*) mode="${arg#+SOLOUVM_FAKE_MODE=}" ;;
        +SOLOUVM_FAKE_SCB=*) scb_args+=("${arg#+SOLOUVM_FAKE_SCB=}") ;;
    esac
done

banner() {
    echo 'FAKE-SIM 1.0: simulation started'
    echo 'UVM_INFO @ 0ns: reporter [RNTST] Running test fake_test...'
    echo 'UVM_INFO verif/tests/fake_test.sv(42) @ 100ns: uvm_test_top [FAKE] stimulus running'
}

scb_report() {
    local item scb_id scb_n
    for item in ${scb_args[@]+"${scb_args[@]}"}; do
        scb_id="${item%%:*}"
        scb_n="${item#*:}"
        if [ -n "$scb_id" ] && [ -n "$scb_n" ]; then
            printf 'UVM_INFO @ 50000ns: uvm_test_top.env.scb [%s] compared=%s passed=%s failed=0\n' \
                "$scb_id" "$scb_n" "$scb_n"
        fi
    done
}

test_done() {
    echo 'UVM_INFO /tools/uvm/src/base/uvm_objection.svh(1271) @ 50000ns: reporter [TEST_DONE] '"'"'run'"'"' phase is ready to proceed to the '"'"'extract'"'"' phase'
}

# $1=UVM_ERROR 数，$2=UVM_FATAL 数
summary() {
    echo ''
    echo '--- UVM Report Summary ---'
    echo ''
    echo '** Report counts by severity'
    printf 'UVM_INFO :    %s\n' 4
    printf 'UVM_WARNING :    %s\n' 0
    printf 'UVM_ERROR :    %s\n' "$1"
    printf 'UVM_FATAL :    %s\n' "$2"
}

finish() {
    echo 'sim time: 50000ns'
}

case "$mode" in
    pass)
        banner; scb_report; test_done; summary 0 0; finish
        exit 0 ;;
    uvm_error)
        banner
        echo 'UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: uvm_test_top.env.scb [AXI_SCB] mismatch: expect 0x00 got 0x1f'
        scb_report; test_done; summary 1 0; finish
        exit 0 ;;
    uvm_fatal)
        banner
        echo 'UVM_FATAL verif/envs/axi/axi_drv.sv(88) @ 2345ns: uvm_test_top.env.agt.drv [AXI_DRV] bus hung'
        scb_report; test_done; summary 0 1; finish
        exit 0 ;;
    no_summary)
        banner; scb_report; test_done
        exit 0 ;;
    rc3)
        banner; scb_report; test_done; summary 0 0; finish
        exit 3 ;;
    hang)
        banner
        while true; do
            echo 'UVM_INFO @ 0ns: reporter [FAKE] still running'
            sleep 1
        done ;;
    segv)
        banner
        kill -SEGV $$
        exit 1 ;;
    license)
        echo 'Failed to obtain license for fake_sim'
        exit 1 ;;
    nonuvm_pass)
        echo 'SOLOUVM_DONE status=pass'
        exit 0 ;;
    version)
        echo 'FAKE-SIM 1.0'
        exit 0 ;;
    *)
        echo "FAKE-ERROR: unknown mode $mode"
        exit 2 ;;
esac
