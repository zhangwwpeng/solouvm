---
name: solouvm
description: Drive SystemVerilog/UVM regression runs, UVM scaffolding, fast compile checks, and main/triage AI failure analysis with solouvm. Always use `--json` for structured reads and `--role triage` when acting as a triage subagent.
---

# solouvm Agent Skill

`solouvm` separates the controller (**A**, Python CLI) from the execution machine (**B**, generated `run.mk` + bash). Always drive verification through `solouvm` commands instead of hand-parsing logs or editing `.solouvm/` artifacts manually.

## Core Rules

1. **Always pass `--json`** for machine-readable state (`schema == 1`, keys: `schema`, `command`, `ok`, `data`, `truncated`, `next`). Note that streaming runs (`regress --json`, `run --json`, `rerun --json`) emit single-line JSONL `"event"` lines (`progress`, `job_done`) before the final formatted envelope object — parse all JSON objects sequentially and consume the last one.
2. **Respect role boundaries**:
   - Main agent (default `--role main`): creates/edits configs & cases, runs `validate`/`check`/`build`/`regress`/`rerun`, runs `triage plan` and `triage review --accept|--reject`, fixes RTL/TB, closes todos (`todo done`).
   - Triage subagent (**MUST pass `--role triage`** on every CLI call): read-only inspection + `bug add` / `bug update` (own unreviewed bugs only) / `note add` / `todo add` / `triage write`. NEVER edit RTL, testbench, or `case.py` directly when triaging; NEVER downgrade a failed job to pass.
3. **Exit codes**:
   - `0`: clean pass / valid / all triage tasks reviewed.
   - `1`: completed run/check/style/triage has non-pass jobs, style issues, check errors, or pending unreviewed tasks.
   - `2`: configuration/case/build/provider/role error (`error.code` in JSON) or `SELECT_EMPTY`.
   - `130`: interrupted (`SIGINT`); check `partial: true` in `results.json`.
4. **UVM editing rule**: When scaffolding (`new`, `uvm sync`), only write custom SV code inside `// solouvm:user begin <name>` ... `// solouvm:user end <name>` markers. Keep custom file entries inside `// solouvm:files begin/end` in `<pkg>_files.svh`. Run `solouvm lint-style` after editing SV files.

## Standard Main-Agent Loop

1. Inspect & check before long runs:
   - `solouvm validate --json`
   - `solouvm list --json`
   - `solouvm check --format json` (when `slang` or target simulator is available)
   - `solouvm lint-style --json`
2. Run & judge:
   - `solouvm regress --json` (or `solouvm run --case <pat> --json`)
   - To re-evaluate after updating `checks` / `waivers` without re-simulating: `solouvm judge --json`
3. Triage & review when non-pass or risky-pass jobs exist:
   - `solouvm triage plan --json`
   - Dispatch subagents (`--role triage`) with each `task_id`; read [references/triage-workflow.md](references/triage-workflow.md).
   - `solouvm triage status --json`
   - `solouvm triage review <task_id> --accept` (or `--reject "<reason>"`)

## References

- CLI commands, flags, role permissions & error codes: [references/commands.md](references/commands.md)
- `CFG`, `Case`, `Suite`, `Expect`, `Waiver`, `Notes` schemas: [references/case-fields.md](references/case-fields.md)
- Step-by-step subagent triage protocol & `--doc` rules: [references/triage-workflow.md](references/triage-workflow.md)
