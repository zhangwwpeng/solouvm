# solouvm Command & Role Reference

## Global Flags (accepted before or after subcommands)

- `-c, --config PATH`: config file or project root (searches parent dirs when omitted)
- `-v / -vv`: stream logs (`-v`), trace external commands to `.solouvm/solouvm.log` (`-vv`)
- `-q, --quiet`: suppress info, print only errors and final summary
- `--json`: emit schema-1 JSON envelope (`schema`, `command`, `ok`, `data`, `truncated`, `next`)
- `--limit N`: row/line limit (default `50`, `0` disables truncation); for `triage packet`, sets character budget
- `--role main|triage`: enforce role permissions (`ROLE_DENIED` -> exit `2`)
- `--provider NAME`: override configured provider
- `--no-hooks`: skip A-side hooks (reserved)
- `--version`: print version and exit

## Role Matrix

### Allowed under `--role triage`
- Read-only: `config`, `files`, `validate`, `schema`, `list`, `uvm diff`, `lint-style`, `status`, `log`, `explain`, `wave`, `history`, `notes`, `progress`, `triage packet`, `triage status`, `help`, `version`
- Controlled writes: `bug add`, `bug update` (only if `bug.author == current_actor` and not linked to a reviewed triage task), `note add`, `todo add`, `triage write`

### Denied under `--role triage` (`ROLE_DENIED`, exit 2)
`init`, `new`, `uvm sync`, `gen`, `build`, `run`, `regress`, `rerun`, `check`, `stop`, `judge`, `report`, `todo done`, `triage plan`, `triage review`, `cov merge`, `cov report`, `clean`.

## Key Subcommands by Task

- **Setup & Inspect**:
  - `init [--simulator fake|vcs|xrun|questa|xezim|verilator] [--bare] [--agents claude]`
  - `config [KEY]`, `files`, `validate`, `schema [job.facts|plan|results|tasks|json_envelope]`, `doctor`, `help [TOPIC]`
- **UVM Scaffolding**:
  - `new agent|env|test|seq|vseq NAME [--env E] [--agent A] [--agents A,B]`
  - `uvm sync [--env E] [--agent A] [--dry-run]`, `uvm diff [--env E] [--agent A]`, `lint-style`
- **Build & Run**:
  - `list`, `gen`, `build [--build MODE...] [--rebuild]`, `run` (requires `--case`/`--tag`/`--stage`), `regress`, `rerun [--failed] [--status S...] [--run ID] [--note REF]`
  - Selection flags: `--case PAT`, `--tag T`, `--exclude-tag T`, `--stage S`, `--seed N`, `--seeds SPEC`, `--build MODE`, `--shard I/N`, `--allow-empty`, `--order name|duration`
  - Exec flags: `-j N`, `--force`, `--detach`, `--wave`, `--retry N`, `--max-fail N`, `--budget DUR`, `--dry-run`, `--rebuild`
  - `check [--slang-only] [--no-faketop] [--where local|remote] [--format text|gcc|json] [--incremental]`
- **Monitor & Post-Run**:
  - `status [--run ID] [--watch]`, `stop [--run ID]`
  - `log [JOB_ID] [--run ID] [--try N] [--tail N | --lines A-B | --around N --context C] [--grep PAT] [-f] [--build MODE]`
  - `explain JOB_ID [--run ID] [--try N]`, `wave JOB_ID --signals S [--from T1] [--to T2] [--changes|--all]`
  - `judge [--run ID] [--allow-missing] [--partial]`, `report [--run ID] [--format text|json|junit|html] [--compare ID] [-o PATH]`
  - `history [CASE]`, `cov merge [--run ID] [-o DIR]`, `cov report [--run ID] [--merged-dir DIR]`, `clean [--run ID | --all | --locks] [--local-only]`
- **Notes & Triage**:
  - `notes [--type bug|todo|log|note] [--status ...] [--owner O] [--since YYYY-MM-DD] [--until YYYY-MM-DD] [--overdue] [--next-id CASE]`
  - `bug add CASE --title T [--seed N] [--run ID] [--job JID] [--owner O] [--link L] [--body TEXT|-] [--author A] [--id N]`
  - `bug update CASE#ID [--status open|fixed|verified|wontfix|invalid|duplicate] [--owner O] [--link L] [--append-body TEXT|-] [--author A]`
  - `note add CASE --text TEXT|- [--kind log|note] [--date YYYY-MM-DD] [--author A]`
  - `todo add CASE --title T [--owner O] [--due YYYY-MM-DD] [--author A]`, `todo done CASE#ID`
  - `doc [-o DIR] [--lang zh|en] [--with-results|--no-results]`, `progress`

## JSON Error Codes (`error.code`)

- Exit `2`: `CFG_NOT_FOUND`, `CFG_UNKNOWN_KEY`, `CFG_INVALID`, `CFG_LOAD_FAILED`, `FILES_MISSING`, `CASE_INVALID`, `CASE_PLANNED`, `RECORD_INVALID`, `RECORD_CONFLICT`, `RECORD_WRITE_FAILED`, `SELECT_EMPTY`, `LIMIT_EXCEEDED`, `ROLE_DENIED`, `BUILD_FAILED`, `PROVIDER_FAILED`, `GIT_DIRTY`, `RUN_ACTIVE`, `RUN_NOT_FOUND`, `JOB_NOT_FOUND`, `TASK_NOT_FOUND`, `TRIAGE_INVALID`, `UVMGEN_EXISTS`, `CHECK_UNAVAILABLE`, `HOOK_FAILED`.
- Exit `1`: `TESTS_FAILED`, `CHECK_FAILED`, `STYLE_VIOLATION`, `TRIAGE_PENDING`.
- Exit `130`: `INTERRUPTED`.
