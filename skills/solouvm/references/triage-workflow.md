# Triage Subagent & Main Review Workflow

## 1. Main Agent: Plan Tasks

```bash
solouvm triage plan --run <run_id> --json
```
Options:
- `--pass-policy none|changed|all` (default from `CFG["triage"]["pass_policy"]`, `"changed"`)
- `--group-by signature|case|job` (default `"signature"`)

Each task object in `data.tasks` contains `id` (e.g. `20261118-223739-b9c2/1`), `kind` (`fail` or `pass`), `cases`, `jobs`, `reason`, `signature`.

## 2. Triage Subagent (`--role triage`): Inspect & Write

Always pass `--role triage` and `--json` for structured reads:

```bash
# 1) Read full 7-section context packet (sections: Task Instructions, System Doc,
#    Case Doc, Current Run Result, Source Code Snippets, Historical Context, Repro & Wave)
solouvm --role triage triage packet <task_id> --run <run_id>

# 2) Optional deep-dive reads if packet truncated logs or you need waveform/history
solouvm --role triage log <job_id> --run <run_id> --around <line> --context 40 --json
solouvm --role triage explain <job_id> --run <run_id> --json
solouvm --role triage wave <job_id> --run <run_id> --signals "tb_top.dut.*" --from 0ns --to 1000ns --json
solouvm --role triage notes --case <case> --json

# 3) Check whether an open/fixed Bug with the same failure signature already exists:
#    - If not -> create a new Bug (note primary case shown in Section 1/3 for multi-case tasks):
solouvm --role triage bug add <case> --run <run_id> --job <job_id> --title "<concise bug title>" --body - --json
#    - If same-signature Bug exists -> append a finding note instead:
solouvm --role triage note add <case> --text - --json

# 4) Write triage verdict + 5-section debug document
solouvm --role triage triage write <task_id> --run <run_id> \
  --verdict <verdict> \
  --confidence high|medium|low \
  --summary "<=200 chars non-empty summary>" \
  --evidence "<evidence>" \
  --bug <case>#<id> \
  --doc - <<'EOF'
## 现象
<what failed or looked suspicious>

## 排查过程
<logs/code/wave steps checked>

## 证据
<exact line/signal/file observations>

## 结论
<root cause verdict justification>

## 建议
<concrete next action for main agent or RTL owner>
EOF
```

### Strict `triage write` Validation Contracts
- Allowed verdicts for `kind=fail`: `design_bug`, `tb_bug`, `spec_unclear`, `env_issue`, `unknown`, `false_pass` (`real_pass` forbidden).
- Allowed verdicts for `kind=pass`: `real_pass`, `false_pass`, `tb_bug`, `spec_unclear`, `unknown`.
- `--summary`: non-empty, `<= 200` chars.
- `--evidence`: at least one value matching `log:<line>`, `<file>:<line>`, or `wave:<time> <signal>`.
- `--doc`: path to `.md` file or `"-"` (stdin); MUST contain all 5 non-empty headings: `## 现象`, `## 排查过程`, `## 证据`, `## 结论`, `## 建议`.

## 3. Main Agent: Review & Apply

```bash
solouvm triage status --run <run_id> --json   # exit 1 if any task unreviewed, 0 when all reviewed
solouvm triage review <task_id> --run <run_id> --accept --json
solouvm triage review <task_id> --run <run_id> --reject "<reason>" --json
```
- `--accept` writes `Triage.review` into each task case and re-judges `results.json` (applying `false_pass` verdict overrides automatically; failed jobs can never become `pass`).
- `--reject "<reason>"` writes rejection, marks bugs created & linked only to this rejected task as `invalid`, and allows triage to re-run `triage write` cleanly.
