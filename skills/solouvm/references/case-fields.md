# Configuration, Case, Expect & Record Reference

## Config Hierarchy & Keys (`solouvm.conf.py` / `solouvm.local.py`)

Precedence: `CLI > env vars > solouvm.local.py (LOCAL) > solouvm.conf.py (CFG) > DEFAULTS`.

- Required `CFG` keys: `simulator`, `top`.
- Top-level keys (`SPEC` strictly rejects unknown keys with `CFG_UNKNOWN_KEY`):
  `project`, `simulator`, `top`, `uvm_test`, `incdirs`, `defines`, `builds`, `regress`, `run`, `checks`, `job`, `sim`, `providers`, `provider`, `check`, `case_dirs`, `system_doc`, `triage`, `doc`, `history`, `limits`, `plugins`, `uvm`.
- Per-build keys under `CFG["builds"][mode]`:
  `defines`, `incdirs`, `compile_opts`, `run_opts`, `coverage` (`bool`), `timeout` (default `"2h"`). *(Use `compile_opts` / `run_opts`, never `opts`.)*
- Provider keys under `CFG["providers"][name]`:
  - `transport`: `kind` (`local|ssh`), `host`, `workdir`, `ssh_opts`
  - `launcher`: `kind` (`make|lsf`), `queue`, `resources`, `extra`
  - `sync`: `kind` (`none|rsync|git`), `exclude`, `delete`, `remote`, `branch`
  - `setup`: shell setup string sourced on B before tool commands

## `filelist.py` (`FILES`)

Keys: `rtl`, `tb`, `top`. Supports glob patterns (`**`), `+incdir+...`, `+define+...`, `-y`, `-v`, `+libext+`, `-f` (cwd-relative), `-F` (`.f`-relative), and `${VAR}` env refs.

## `Case` & `Suite` Fields (`solouvm.model`)

- Identity & build/run:
  - `name: str` (`[A-Za-z0-9_.-]+`, no `__`)
  - `uvm_test: str | None = None`
  - `build: str = "default"`
  - `params: dict[str, str|int|float|bool] = {}`
  - `opts: list[str] = []`
  - `env: dict[str, str] = {}`
  - `seeds: str | list[int] | None = None`
  - `timeout: str | None = None`, `hang: str | None = None`, `retry: int | None = None`
- Classification & Verdict:
  - `tags: tuple[str, ...] = ()`
  - `owner: str | None = None`
  - `stage: str = "wip"` (`planned | wip | ready | signoff | deprecated`; `planned` is skipped by default and raises `CASE_PLANNED` if selected by `--case`)
  - `checks: dict[str, object] = {}` (overrides `Checks`; `waivers` list is **appended** to global waivers)
  - `expect_fail: bool | str = False` (regex matches first/relevant failure line -> `xfail`, clean run -> `xpass`)
- Intent & Mechanical Checks:
  - `intent: str = ""`, `method: str = ""`, `spec_refs: list[str] = []`, `doc: str = ""`
  - `expect: list[Expect] = []` (requires `ready`/`signoff` intent+method+checks hygiene)
  - `notes: list[Bug | Todo | Log | Note | Triage] = []` (recommended last field in `Case(...)` for clean AST edits)
- `Expect(...)`: requires >= 1 of `scoreboard` (requires `min` or `max`), `regex` (`count_min`/`count_max`), `sim_time_min` (e.g. `"1us"`), `uvm_info_min` (requires `id`). Optionally `reason`.
- `Waiver(...)`: requires `reason` and (`id` or `regex`); optional `severity=("ERROR","WARNING")`, `max: int | None`, `until: "YYYY-MM-DD"`.

## Record Rules (`notes=[...]`)

- `Bug` statuses & required date fields: `open` (`found`), `fixed` (`found, fixed`), `verified` (`found, fixed, verified`), `wontfix` (`found`), `invalid` (`found`), `duplicate` (`found, link`).
- Notice `Bug` and `Todo` share monotonically allocated numeric IDs within a `Case` (`CASE#1`, `CASE#2`, ...). Always capture `data.ref` returned by `bug add --json` / `todo add --json`.
