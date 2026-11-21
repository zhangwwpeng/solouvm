# solouvm 代码审查报告

审查对象：`/Users/wpzhang/workspace/solouvm`（0.1.0，约 29k 行 Python，venv 内 802 个测试通过）。
审查方式：通读 `solouvm/` 全部非 vendor 模块与关键文档（`doc/verdict.md`、`doc/formats.md`、`doc/requirements.md`、`doc/cli.md`），
对每个可疑点做最小复现脚本验证；标注 **已验证** 的条目均有可重复的实测输出，标注 **代码事实** 的条目为静态确认但未跑端到端复现。

严重级：P0 = 会把失败判成通过，或静默破坏已有数据；P1 = 常见配置下功能错误；P2 = 特定输入/环境下出错或缺少防护；P3 = 边缘缺陷。

## 1. 结论摘要

| # | 级别 | 缺陷 | 位置 | 状态 |
|---|------|------|------|------|
| 1 | P0 | UVM 摘要残缺时丢弃实际错误计数，失败被判定为 pass | `judge/scan.py:170` | 已验证 |
| 2 | P0 | 复用 `SOLOUVM_RUN_ID` 静默覆盖 run 目录，留下互相矛盾的 plan/results | `pipeline.py:90`、`pipeline.py:191` | 已验证 |
| 3 | P1 | 编译键不含当前 provider 的 `setup`，跨环境复用旧产物 | `pipeline.py:164`、`buildkey.py:242` | 已验证 |
| 4 | P1 | `--shard` 与 `random:N` 种子组合不是稳定分片 | `plan.py:312`、`plan.py:368` | 已验证 |
| 5 | P1 | 项目路径含空格时生成的 Makefile 全部失败 | `gen/makefile.py:383` | 已验证 |
| 6 | P1 | VCD 存储器/数组元素被折叠成一个信号，波形查询结果错误 | `wave.py:240` | 已验证 |
| 7 | P2 | `plan.json` 的 `job.dir` 未校验，可指向轮次目录之外 | `plan.py:599` | 代码事实 |
| 8 | P2 | `plan.json` 的 `timeout_s`/`hang_s` 接受 NaN/Inf/负数 | `plan.py:534` | 代码事实 |
| 9 | P2 | SSH 传输没有连接超时与操作超时，控制机可能永久阻塞 | `providers/transport_ssh.py:104` 等 | 代码事实 |
| 10 | P2 | `results.json` 只校验顶层 schema，报告渲染直接做数值转换 | `report/json.py:217`、`report/junit.py:22` | 代码事实 |
| 11 | P3 | SSH `read_many()` 帧格式在文件名含换行时解析错位 | `providers/transport_ssh.py:281` | 代码事实 |
| 12 | P3 | 编译锁的 stale 回收只在同一主机生效 | `gen/scripts.py:363` | 代码事实 |
| 13 | P3 | `compare_results()` 用 `status` 而非 `judged_status`，与分诊语义存在歧义 | `report/json.py:251` | 代码事实 |

## 2. 缺陷详情

### 2.1 [P0] 残缺的 UVM 摘要使失败被判定为通过

位置：`solouvm/judge/scan.py:170-172`

```python
has_summary = bool(summary_counts)
source = summary_counts if has_summary else line_counts
uvm_counts = {sev: source.get(sev, 0) for sev in SEVERITIES}
```

`scan()` 只要在 `** Report counts by severity` 之后解析到 **任意一条** 计数行，就整体改用摘要计数，
而 `summary_counts` 里缺失的严重度一律取 0。日志被截断、摘要被改写或摘要只写了一部分时，
日志正文里的 `UVM_ERROR`/`UVM_FATAL` 计数被直接丢弃；`_uvm_limit()`（`judge/rules.py:265`）
看到 0 不报错，规则一路落到 `_rule_14` 的 `pass clean`。

复现（自定义日志：一条 ERROR + 残缺摘要，仅含 `UVM_INFO`）：

```text
UVM_ERROR @ 10ns: uvm_test_top [SCB] mismatch
** Report counts by severity
UVM_INFO : 1
```

```text
SUMMARY partial True {'INFO': 1, 'WARNING': 0, 'ERROR': 0, 'FATAL': 0} pass clean
SUMMARY complete ... {'INFO': 1, 'WARNING': 0, 'ERROR': 1, 'FATAL': 0} fail UVM_ERROR=1 > 0
```

同一份日志，摘要补全后判 `fail`，摘要残缺时判 `pass`。这与 README “日志被截断不会被误判为 pass” 的承诺直接冲突。

影响：`max_uvm_error` / `max_uvm_fatal` / `max_uvm_warning` 全部失效；`scan.has_summary`
还被 `_rule_11`（“UVM log without Report Summary”）用作存在性判据，残缺摘要同样能绕过该规则。

建议：仅当 `SEVERITIES` 各项都在摘要中出现时才采用摘要计数；否则与 `line_counts` 取 `max()`，
或在缺少某项时回退到正文计数。另建议对摘要行数与 `UVM_INFO` 行数做一致性检查并告警。

### 2.2 [P0] 复用 `SOLOUVM_RUN_ID` 会静默覆盖已有轮次

位置：`solouvm/pipeline.py:90-109`（`make_run_id` 只校验字符集，不检查目录是否已存在）、
`solouvm/pipeline.py:191-203`（写入 `plan.json` 后立刻 `update_latest_link`）

`gen`/`regress`/`rerun` 在 `SOLOUVM_RUN_ID` 存在时无条件返回该 id，随后覆盖
`.solouvm/runs/<id>/plan.json`、`run.mk`、`env.json`，但旧轮次的 `results.json`、
`triage/tasks.json`、`try*/` 目录原地保留。结果是同一轮次里 plan 与 results 指向不同 job 集合。

复现：

```bash
export SOLOUVM_RUN_ID=fixed-id
solouvm regress                       # smoke__2099073578 pass，写出 results.json
solouvm gen --seed 77                 # 覆盖 plan.json
```

```text
RUN_ID 0 original results [('smoke__2099073578', 'pass')] new plan ['smoke__77']
retained results ['smoke__2099073578']
```

影响：`judge`/`report`/`status`/`rerun` 读到不匹配的 plan/results；CI 里把 `SOLOUVM_RUN_ID`
固定成构建号重跑时，旧结论会被当作本轮结果读取（可能保留一个虚假的 pass）。
`doc/guide/05-running-and-remote.md:38` 明确承诺“各轮产物互不覆盖”，实现与文档相反。

建议：当目标目录已存在且含 `plan.json`/`results.json` 时拒绝启动（除非新增
`--reuse-run` 显式声明覆盖），或在覆盖前把旧目录改名归档。

### 2.3 [P1] 编译键忽略当前 provider 的 `setup`

位置：`solouvm/pipeline.py:164-172` 调用 `compute_build_keys(...)` 时没有传 `provider=`；
`solouvm/buildkey.py:242` 因此在任何情况下都退回 `config.provider`

```python
active_provider = provider if provider is not None else config.provider
provider_cfg = config.providers.get(active_provider)
provider_setup = provider_cfg.setup if provider_cfg is not None else ""
```

`architecture.md §4.2` 与 `tests/test_buildkey.py:125` 都把 provider `setup` 列为键的组成部分，
但管线从不把实际生效的 provider 传进来（`cli.py:376` 已经把 `provider=` 交给 `pipeline.gen`）。
同时 `gen/makefile.py:375` 生成 recipe 时用的是 `plan.provider`，即“编译命令按 A provider、缓存键按 B provider”。

复现：两个 provider（`local` 无 setup、`alt` 有 `setup`），同一项目同一模式：

```text
CACHE [('local', {'default': '06500e3dcc2d'}, False), ('alt', {'default': '06500e3dcc2d'}, True)]
```

键完全相同，而生成的 `run.mk` 中只有 `alt` 带 `--setup`。先以 `local` 编译、再以 `alt` 运行时，
`.ok` 命中，直接用未加载工具环境的旧 simv 跑，编译期差异被掩盖。

建议：`gen()` 把 `eff_provider` 传给 `compute_build_keys(provider=eff_provider.name)`，
并补一条“不同 setup 必须给出不同键”的回归测试。

### 2.4 [P1] `--shard` 与 `random:N` 组合不是稳定分片

位置：`solouvm/plan.py:312`（每次 `build_plan` 新建 `random.Random()`）、
`solouvm/plan.py:331`（先展开种子）、`solouvm/plan.py:368-370`（后切片）

```python
jobs = [j for idx, j in enumerate(jobs) if idx % total == index - 1]
```

种子在切片前就已随机抽取，每个分片进程各抽各的，切片得到的集合既不重合于整体，也不可重组。

复现（`seeds: "random:4"`）：

```text
shard 1/2 -> [76354963, 1514360304]    shard 2/2 -> [1297587658, 1931561869]
unsharded -> [706821935, 955539927, 1576348300, 1805264948]
```

影响：`doc/requirements.md:119` 的 T-16 要求“稳定地分成 n 份，只生成第 i 份”，
用随机种子分发到多台执行机时，每个分片跑的是互不相同的种子集合，整体覆盖与单机一轮不一致，
也违反 G-06 的“只生成指定子集”可复现要求。

建议：随机种子改为由 `(run_id, case, index)` 派生的确定性序列（如 `hashlib` 或
`random.Random(f"{run_id}:{case}")`），或先对 case 列表切片、再在各分片内展开种子。

### 2.5 [P1] 项目路径含空格时生成的 Makefile 全部失败

位置：`solouvm/gen/makefile.py:381-383`

```make
ROOT    := $(CURDIR)
RUN_JOB := bash $(ROOT)/.solouvm/gen/bin/run_job.sh
```

`RUN_JOB` 未加引号也未做 `make_escape`；其余 token 都经过转义，只有这一处漏掉。
项目放在含空格的路径（macOS 的 `~/Documents/My Project` 很常见）时，make 把路径按空格拆成两个参数。

复现（`init --simulator fake` 后 `regress`，目录名 `with space`）：

```text
bash /private/var/.../solouvm-review-c3l6g4kz/with space/.solouvm/gen/bin/run_job.sh --kind build ...
bash: /private/var/.../solouvm-review-c3l6g4kz/with: No such file or directory
make: *** [.solouvm/build/default/f1f3d4bbda2f/.ok] Error 127
# 退出码 2，全部 job 判 error
```

同一流程放在无空格目录下 `Summary: 1/1 passed (100.0%)`。

影响：报错只显示 make 的 `Error 127`，与 README “只用相对路径、项目可搬迁”的定位相悖；
用户很难从输出定位到空格问题。

建议：改为 `RUN_JOB := bash "$(ROOT)/.solouvm/gen/bin/run_job.sh"`，并对
`$(B_<mode>)/.ok`、`--dir` 等所有引用 `$(ROOT)` 的位置统一加引号。

### 2.6 [P1] VCD 存储器/数组元素被折叠成一个信号

位置：`solouvm/wave.py:232-250`

```python
ref = tokens[3]
full_name = ".".join([*scope_stack, ref]) if scope_stack else ref
```

`$var wire 8 ! mem [0] $end` 的 `[0]` 在 `tokens[4]`，未参与命名；
多个元素共用 `mem` 这个名字，`id_to_names` 各自登记，但 `commit_tick()` 只按名字快照，
后写入的元素覆盖先写入的。

复现（VCD 中两个 8 位元素 `mem[0]`、`mem[1]` 分别为 `0x1`、`0x2`，查询 `tb.mem*`）：

```text
['tb.mem'] [{'time_ns': 0.0, 'time': '0ns', 'values': {'tb.mem': '0x2'}}]
```

查询 `tb.mem[[]0]` 则返回空结果（`_STRIP_RANGE_RE` 把 `[0]` 当位选剥掉）。

影响：`solouvm wave` 对存储器、多维数组、`$var ... name [0]` 这类常见声明给出**错误数值**且不报错；
用它做“波形里到底是不是这个值”的判断会得出相反结论。这类信号在 UVM 环境中大量存在。

建议：无位选时用 `tokens[4:]` 拼成 `name[0]` 形式；有位选时保留为位选并映射到同一 id；
多个 id 共用一个名字时，快照按 `(name, index)` 存储，或改为按 id 输出。

### 2.7 [P2] `plan.json` 的 `job.dir` 未做边界校验

位置：`solouvm/plan.py:599`（`dir=_require_str(raw, "dir", path)`）

`read_plan()` 校验了类型、schema、时间戳，但 `dir` 原样接受。该值随后被直接拼进：
`pipeline.py:498`（读 `sim.log`）、`:645`（写 `.wave`）、`:1257`（`read_many` glob）、
`:1263`（`root / job.dir`）、`judge/__init__.py:415`、`cov.py:271`、`explain.py:600`。

影响：被篡改或损坏的 `plan.json` 可让 `status`/`log`/`wave`/`judge` 读取轮次目录之外的文件，
远端 provider 下同样的路径会发到执行机。属于“本地产物可信”假设下的加固缺失，但成本很低。

建议：加载时要求 `dir` 为 `PurePosixPath`、非绝对、不含 `..`，且等于
`.solouvm/runs/<run_id>/<job_id>`，否则报 `CFG_INVALID`。

### 2.8 [P2] `plan.json` 的超时字段接受 NaN / Inf / 负数

位置：`solouvm/plan.py:534-547`（`_job_number`）

```python
return float(value)
```

没有 `math.isfinite` 与范围检查。`solouvm/units.py:25` 对配置里的时长做了
`if not math.isfinite(seconds) or seconds < 0: raise ValueError`，反序列化这条路径没有复用它。

影响：`NaN` 参与比较恒为假，超时判断可能永不触发；`Infinity` 使超时失效；负数导致立即超时。
`json.loads` 默认接受 `NaN`/`Infinity` 字面量，手工改过的 `plan.json` 不会被拦住。

建议：`_job_number` 增加 `math.isfinite(value)` 与 `value >= 0` 校验，`timeout_s` 要求 `> 0`，
并复用 `units.parse_duration` 的错误文案。

### 2.9 [P2] SSH 传输没有连接超时与操作超时

位置：`solouvm/providers/transport_ssh.py:63`（`_ssh_base` 不注入任何超时选项）、
`:104`、`:122`、`:148`、`:256`、`:295`、`:342`、`:361`、`:383`、`:400`（全部 `subprocess.run` 无 `timeout=`）

`-o ConnectTimeout` / `ServerAliveInterval` / `BatchMode` 都未设置，Python 侧也没有超时。

影响：主机不可达、网络半开、认证卡住或远端命令不退出时，整个 CLI 永久挂起；
`run_job.sh` 的 `--timeout`/`--hang` 只管子进程，保护不到传输层。
`_launch_make()` 的轮询（`pipeline.py:503-528`）会卡在 `read_many()` 里，
`--budget`、`max_fail` 都无法生效。

建议：`_ssh_base()` 默认追加 `-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=15
-o ServerAliveCountMax=3`（允许 `ssh_opts` 覆盖），并为 `subprocess.run` 提供可选超时参数。

### 2.10 [P2] `results.json` 只校验顶层 schema，渲染层直接做数值转换

位置：`solouvm/report/json.py:217-248`（只检查 dict 与 `schema`）、
`solouvm/report/junit.py:22`（`float(summary_dict.get("duration_s", 0.0) or 0.0)`）、
`solouvm/report/html.py:60-63`（`int(...)`/`float(...)` 直接转换）、`report/json.py:49-61`
（`job_duration` 直接取 `item["attempts"]`）

影响：被截断、被手工编辑或由旧版本写出的 `results.json` 能通过校验，
然后在 `report`/`junit`/`html` 阶段以 `TypeError`/`KeyError` 崩溃，报错信息与根因无关。

建议：`read_results_json` 校验 `jobs` 为 list[dict]、`summary` 为 dict、关键字段类型正确；
或提供 `validate_results_payload(payload)` 供渲染前调用，失败时给
“results.json 已损坏，重跑 `solouvm judge`”的 hint。

### 2.11 [P3] `read_many()` 帧格式对含换行的文件名不安全

位置：`solouvm/providers/transport_ssh.py:281-329`

远端 `printf '%d\n%s\n' "$sz" "$f"`，本地按“一行长度 + 一行文件名 + size 字节内容”解析。
匹配到的文件名含换行时，文件名被拆成两行，后续字节流整体错位。
`_glob_quote()` 只拒绝了模式里的换行，没有约束匹配结果。触发面限于 `job.facts` 等由 solouvm
自己命名的文件，实际风险低。

建议：改用 NUL 分隔（`printf '%d\0%s\0'`）或先 `find -print0` 再按长度读。

### 2.12 [P3] 编译锁的 stale 回收只在本机生效

位置：`solouvm/gen/scripts.py:363-364`

```bash
if [ -n "$owner_host" ] && [ "$owner_host" = "$host" ] \
    && [ -n "$owner_pid" ] && [[ "$owner_pid" =~ ^[0-9]+$ ]] \
    && ! kill -0 "$owner_pid" 2>/dev/null; then
```

跨主机共享目录时，持有者主机崩溃留下的锁永远不会被回收，后续编译会一直等到锁超时。
设计上避免对他机 PID 发信号是合理的，但缺少 TTL/租约式兜底。

建议：owner 文件加写入时间戳，超过 `lock_ttl`（如 2× 编译超时）即视为 stale 并在日志里说明。

### 2.13 [P3] `compare_results()` 使用 `status` 而非 `judged_status`

位置：`solouvm/report/json.py:251-295`

`result_to_dict()` 同时序列化 `status`（含分诊后的最终状态）与 `judged_status`（机械判定），
比较逻辑只看 `status`。`doc/formats.md:226` 说明两者语义不同。
按最终状态比较“新失败/新修复”本身说得通，但 `triage` 接受 `false_pass` 后，
同一份日志在两次比较里会呈现为“已修复”，与分诊语义混淆。

建议：在 `doc/cli.md` 的 `report --compare` 一节写明比较的是最终状态；
或增加 `--compare-mode status|judged`。

## 3. 已核查、未发现缺陷的部分

- `solouvm/records/write.py`：`_rewrite()` 的读-改-写事务完整——写前比对摘要（`RECORD_CONFLICT`）、
  `_write_atomic()` 保留权限位、写后 `check()` 失败回滚并删除新建文件；`_offset()` 正确把
  UTF-8 字节列偏移换算成字符下标。未发现会破坏用户源码的路径。
- `solouvm/units.py`：`parse_duration`/`parse_sim_time_ns` 拒绝 `bool`、非有限值、负数，
  并把 `OverflowError` 归一成 `ValueError`；问题只在绕过它的 `plan.py` 反序列化（见 2.8）。
- `solouvm/gen/scripts.py` 的 `run_job.sh`：facts 先写 `state=running` 再执行、`rc=124/137`
  的归类、超时/挂死看门狗对进程组 TERM→KILL、锁内 `.ok` 复查，逻辑自洽。
- `solouvm/judge/rules.py` 的规则顺序与 `verdict.md §3` 一致；`_rule_0a`~`_rule_14` 覆盖编译缺失、
  facts 缺失/过期/schema 不符、环境失败、信号、超时、挂死、退出码、`fail_regex`、UVM 计数、expect。
- `solouvm/config/load.py`：分层合并、插件注册、未知键建议、`solouvm.py` 遮蔽检测均按文档实现。
- 三个独立子审查（核心流程、执行层、判定/报告）各自复核过一遍，本报告只保留经代码或复现确认的条目。

## 4. 修复优先级建议

1. 先修 2.1 与 2.2：前者直接产出假 pass，后者会毁掉可追溯性，二者都在默认工作流上。
2. 再修 2.3、2.4、2.5：分别影响远程/多 provider 场景的缓存正确性、分片分发和含空格路径，
   都属于“配置对了但结果错”的类型。
3. 2.6 单独修：波形查询是分诊阶段的主要证据来源，返回错误数值比返回失败更危险。
4. 2.7 ~ 2.10 作为加固批次，配合 `read_plan`/`read_results_json` 的校验测试一起做。
5. 2.11 ~ 2.13 可并入日常维护。
