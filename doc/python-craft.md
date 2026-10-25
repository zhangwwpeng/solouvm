# 我写 Python 的思路

这份文档不是"PEP 8 摘要"，而是**我在写 solouvm 时的决策过程**：先想什么、后写什么、哪些细节会咬人。
每条都指向仓库里的真实代码，你可以边读边对照改。

建议阅读顺序（`solouvm/` 约 2255 行，半天能读完）：

```
errors.py  →  model.py  →  util.py  →  config.py  →  collect.py
→  plan.py  →  makefile.py  →  simulator.py  →  logcheck.py
→  executor.py  →  status.py  →  report.py  →  hooks.py  →  cli.py
```

先读 `errors.py`（只有 42 行）和 `model.py`：这两个文件定下了后面所有代码的形状。

---

## 总纲：我写一个模块的四步

1. **先把契约写成文字**：谁读谁写的文件、字段名、退出码。写不出来说明还没想清楚。
2. **再定数据形状**：dataclass 字段 + 类型 + 默认值。函数签名基本就自动出来了。
3. **每层只做一件事**，并且能被单独调用、单独测试（不依赖全局状态 / 不依赖 cwd）。
4. **最后用假对象把边界钉死**：假仿真器 + 临时目录，跑通全流程。

代码量不是目标。**"改一个需求只动一处"** 才是目标。

---

## 1. 先定契约，再写代码

我在写第一行实现代码之前，先把这四样钉在纸上（现在它们都在 README / design-decisions 里）：

| 契约 | 内容 | 落在哪 |
|------|------|--------|
| 文件 | `plan.json`、`status.json`、`results.json`，都带 `"schema": 1` | `status.py`、`plan.py` |
| 退出码 | `0/1/2/130`，含义唯一 | `errors.py` |
| 单位 | 一个 `case_id = <case>__<seed>`，一个 `outdir` | `model.py` |
| 边界 | 判定在 `_exec`，`end` 只读 `status.json` | `executor.py` / `status.py` |

好处很具体：因为 `status.json` 的形状先定了，`end`、`report`、测试三者可以**并行**开发，互不返工。

**反例**：先写 `end` 去 `grep` 日志，再补 `_exec`，结果两边对"什么算 pass"各有理解——我在评审早期 README 时指出的就是这个坑。

**练法**：下次写脚本前，先用 10 行注释写出"输入文件长什么样、输出文件长什么样、失败怎么表达"。写不出来就别写代码。

---

## 2. 让"不合法状态"无法表示

`errors.py` 里没有 `code: int` 字段让调用方随便填，而是**类属性 + 子类**：

```python
class SolouvmError(Exception):
    exit_code = EXIT_INFRA
    def __init__(self, message: str, *, hint: str = "") -> None: ...

class ConfigError(SolouvmError): ...   # 用户配错
class InfraError(SolouvmError): ...    # 环境坏了
```

调用方拿到的异常自带退出码，`cli.main` 只有一处 `except`：

```python
try:
    return _dispatch(args)
except SolouvmError as exc:
    print(f"{prog}: error: {exc.render()}", file=sys.stderr)
    return exc.exit_code
```

要点：

- **错误信息带 `hint`**（"怎么修"），而不是只报"什么错了"。用户看到的是：

  ```
  solouvm: error: cannot find solouvm.conf.py in /x or any parent directory
    hint: pass -c/--config PATH or create a solouvm.conf.py at the project root
  ```

- **业务失败不走异常**：case 判成 fail 不是异常，是数据（`status.json`）；只有"没法继续"才抛。

**练法**：给任何 CLI 工具定义三个错误类别（用户错 / 环境错 / 结果不通过），分别映射退出码。

---

## 3. 数据建模：可变输入，不可变中间产物

三种 dataclass，用途严格区分：

```python
# (a) 用户写的：全部可选、带默认值，允许方便地构造
@dataclass
class Case:
    name: str
    uvm_test: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)   # 可变默认值必须 factory
    tags: tuple[str, ...] = ()

# (b) 项目默认值，用来补 (a) 的空洞：返回新对象，不改入参
    def with_defaults(self, cfg: ProjectDefaults) -> "Case":
        return Case(...)

# (c) 执行期快照：frozen，可序列化，_exec 只认它
@dataclass(frozen=True)
class CaseSpec:
    case_id: str
    seed: int
    timeout_s: float
    ...
    def to_json(self) -> dict: ...
    @classmethod
    def from_json(cls, payload) -> "CaseSpec": ...
```

三个细节值得抄：

1. **`field(default_factory=dict)`**，永远不要写 `params: dict = {}`。
2. **`with_defaults` 返回新对象**，不做 `self.top = ...`。这样"用户写的 case"和"实际执行的 case"是两个东西，出问题能对比。
3. **`frozen=True` + `to_json`/`from_json` 成对出现**：一旦某个结构要跨进程传递（写进 JSON、被 `make` 调用），就把它变成纯数据，别塞对象、别塞回调。

**练法**：凡是"配置"和"快照"混在一个类里的代码，拆成两个类。

---

## 4. 函数签名就是文档

```python
def judge_file(
    path: Path,
    *,
    checks: Mapping[str, Any],
    rc: int | None,
    signal: str | None = None,
    timed_out: bool = False,
    tail: int = 0,
) -> dict[str, Any]:
```

- **`*` 之后全用 keyword**：`judge_file(log, checks=..., rc=0)` 读起来是句子；三个布尔参数相邻时，位置传参是 bug 工厂。
- **`Mapping` 而不是 `dict`**：声明"我只读，不改你的字典"。
- **`X | None`**（PEP 604）+ 文件头 `from __future__ import annotations`：3.10 可用，且不在运行时求值，避免循环 import。
- **不返回 `bool` + 错误信息元组**：要么返回结构化结果（含 `status`/`reason`），要么抛异常。

再看 `executor.run(plan_path, case_id, *, verbose=False) -> int`：**返回退出码的函数不抛业务异常**。这是"谁负责退出码"的明确约定——`make` 调 `_exec`，`_exec` 永远尽量返回 0，让整批跑完，聚合判定交给 `end`。

**练法**：把 `def f(a, b, flag=False, other=False)` 改成 `def f(a, b, *, flag=False, other=False)`，一周内你会少写一半注释。

---

## 5. 只在边界做一次归一化

所有"用户可能写错"的解析，都集中在 `config.py` / `util.py`，内层假设数据已合法。

`parse_duration` 是模板：

```python
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ms|s|m|h|d)")

def parse_duration(value: str | int | float) -> float:
    if isinstance(value, bool):                 # bool 是 int 的子类，先挡掉
        raise ConfigError(...)
    total, pos = 0.0, 0
    for match in _DURATION_PART.finditer(text):
        if match.start() != pos:                # 中间有垃圾？立刻报错
            raise ConfigError(...)
        total += float(match.group(1)) * _DURATION_FACTOR[match.group(2)]
        pos = match.end()
    if pos != len(text) or total <= 0:          # 没吃完整串？报错
        raise ConfigError(..., hint="use forms like 500ms, 60s, 20m, 1h30m")
    return total
```

四个可复用的技巧：

1. **`finditer` + 位置校验**，而不是"尽量解析"（`"10x"` 必须报错，不能当成 10）。
2. **`isinstance(x, bool)` 先挡**，否则 `True == 1` 会溜进数值逻辑。
3. **报错时附带 `hint` 展示合法形式**——用户不需要读源码。
4. **解析器返回纯值**，不返回"半成品对象"。`parse_seeds("1-3,10") -> [1,2,3,10]` 一行测试就能锁死。

**练法**：任何接收字符串的地方，写出"这个函数对垃圾输入怎么办"，并补 5 个参数化测试。

---

## 6. 子进程：argv、进程组、显式超时

`executor._run_once` 是全仓库最"危险"的代码，所以每个参数都能说出理由：

```python
proc = subprocess.Popen(
    cmd,                       # list[str]，绝不 shell=True
    cwd=str(outdir),
    stdout=sink,               # 自己重定向到 sim.log，不依赖仿真器 -l
    stderr=subprocess.STDOUT,
    env=env,
    start_new_session=True,    # 自成进程组
)
try:
    rc = proc.wait(timeout=spec.timeout_s)
except subprocess.TimeoutExpired:
    timed_out = True
    _kill_group(proc)
    rc = proc.returncode
```

为什么每个参数都重要：

| 做法 | 如果不这么做 |
|------|--------------|
| `argv` 而非 `shell=True` | 路径含空格/`$`/通配符就炸；还会被 shell 注入 |
| `start_new_session=True` + `killpg` | 只杀 `simv`，杀不掉 fork 的子进程 → license 泄漏、机器被占满 |
| TERM → 等 5s → KILL | 直接 KILL 拿不到覆盖率/波形收尾；只发 TERM 又可能杀不掉 |
| 自己重定向 stdout | 不同仿真器 `-l` 语义不同，甚至有人忘了加 |
| `wait(timeout=...)` | 挂死的仿真永远不返回，整条流水线停摆 |

还有一条**数据语义**的坑，我在这份代码里踩过：

```python
signal_name = None if timed_out else _signal_name(rc)
```

我们自己的 SIGTERM 也会让 `rc` 变负数。如果判定器按"rc 是负 → killed"来判，超时就会被误报成"被信号杀死"。所以 `timed_out` 的优先级必须高于 `signal`，并且**在产生数据的地方就消歧**，而不是让下游猜。

**练法**：今天起，所有 `subprocess` 调用都检查三件事：列表 argv、超时、失败时的清理（`killpg` / `finally`）。

---

## 7. 幂等 + 原子写

`util.write_text_atomic` 只有 12 行，但决定了"中断后还能不能重跑"：

```python
fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
try:
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)       # 同目录 rename 才是原子的
except BaseException:
    Path(tmp).unlink(missing_ok=True)
    raise
```

三个要点：

1. **临时文件放在目标同目录**（跨文件系统 `rename` 不原子）；
2. **`os.replace`**，不是 `os.rename`（Windows 上覆盖语义不同）；
3. **`except BaseException`**：包括 `KeyboardInterrupt`，也必须清理临时文件。

配套的"先写占位"策略，在 `executor.run` 里：

```python
status_mod.save(status_file, status_mod.pending(plan, spec))   # 先写 pending
...
status_mod.save(status_file, payload)                          # 跑完再覆盖
```

于是 `end` 看到 `pending` 就知道"跑过但没跑完"，判为 `error`，而不是拿着上一次的 `pass` 骗人。

**练法**：任何"程序要写结果文件"的场景，问一句：写到一半被 Ctrl-C，重跑会读到什么？

---

## 8. 流式处理，别把日志读进内存

`logcheck.Judge` 是"单遍扫描 + O(1) 内存"的形状：

```python
class Judge:
    def feed(self, line: str) -> None: ...      # 每行只更新计数/标记
    def verdict(self, *, rc, signal, timed_out) -> dict: ...   # 最后一次性裁决
```

```python
with path.open("r", encoding="utf-8", errors="replace") as handle:
    for line in handle:
        judge.feed(line)
```

四个细节：

- **`errors="replace"`**：仿真日志里混进二进制/乱码是常态，不能因此崩溃；
- **计数上限**：`self.fired` 最多 20 条，否则 `status.json` 会被一条刷屏的日志撑爆；
- **不在 `feed` 里做判定**：解析与裁决分离，裁决函数可以脱离文件单独测试（见 `tests/test_logcheck.py` 全是内存字符串）；
- **空文件/缺文件也要能判**：`judge_file` 对不存在的日志仍会基于 `rc` 给出结论。

**反例**：`lines = f.readlines()` 然后 `for line in lines`——仿真日志动辄几个 GB。

---

## 9. 判定逻辑写成"规则表"，不要写成嵌套 if

`Judge._decide` 的结构是**有序规则 + 每条一个 return**：

```
signal → killed
timeout → timeout
rc != 0 → fail
fail_regex → fail
UVM_ERROR 超上限 → fail
...
pass
```

为什么这样写：

- **优先级是显式的、可讨论的**（第一条永远赢），而不是藏在一层层 `if/else` 里；
- 每条规则只依赖入参，不依赖对象状态 → 可以逐条写测试；
- 新增规则就是插一行，改优先级就是移动一行。

配套的设计是**"解析器不知道策略"**：`Judge` 不认识 `expect_fail`，`retry` 也不在里面。它们分别是：

```python
# 状态映射放在 _exec 的最后一步
def _apply_expect(raw: str, expect_fail: bool) -> tuple[str, str | None]:
    if not expect_fail:
        return raw, None
    if raw == "pass":
        return "xpass", "XPASS"      # 该失败却通过了，说明测试失效 → 算失败
    if raw == "fail":
        return "xfail", "XFAIL"
    return raw, None
```

**练法**：把你项目里超过 3 层的 `if` 判定，改写成"函数 + 规则列表"，再给每条规则写一行测试。

---

## 10. 命名：一次改名胜过十行注释

这次真实踩的坑，值得单独说：

> 配置文件一开始叫 `solouvm.py`。当用户在项目根执行 `python -m solouvm` 时，`sys.path[0]` 是当前目录，**同名文件遮蔽了包**，`import solouvm` 拿到的是配置字典。表现是：命令无输出、退出码 0、什么都不生。

这个 bug 无法靠"记得"来避免，只能靠**名字不冲突**。改名 `solouvm.conf.py` 之后彻底消失。

同一个道理：

- `case_id`（`smoke__1`）而不是 `id`——它既是目录名也是 make 目标名，必须文件系统安全（见 `util.slug` + `case_id`）；
- `raw_status` vs `status`：前者是执行事实，后者是应用到 `expect_fail` 之后的结论。**名字里体现推导关系**；
- `_exec`（内部命令）带下划线前缀，`gen/build/run/regress/end` 是用户命令——一眼能分清"给谁用"。

**练法**：命名前问三个问题——会不会和已有模块重名？会不会出现在文件名/URL 里？这个名字是"事实"还是"结论"？

---

## 11. 分层：依赖单向，且内层不认识外层

```
errors ← util ← model ← config ← collect ┐
                                  plan ←───┼→ makefile
                                  simulator ┘
logcheck ← executor → status → report → hooks → cli
```

具体表现：

- `executor.py` **不 import `config`**，只读 `plan.json`；因此 `_exec` 可以在任何目录、任何调度器下被调用；
- `logcheck.py` 不知道 `Case`、不知道文件路径（`judge_file` 只接受 `Path` + 参数）；
- `report.py` 只接受 `results` 字典，不认识 `Config`。

判断分层是否破裂，有个土办法：**看 import 图里有没有环，以及最内层有没有在 import `argparse` / `os.environ`**。

**练法**：把"读环境变量/读 argv"限制在 `cli.py` 一个文件里，其余模块都通过参数拿数据。

---

## 12. 测试：三层，各有各的目的

### (a) 表驱动：纯逻辑

```python
@pytest.mark.parametrize(("text", "expected"), [("500ms", 0.5), ("1h30m", 5400.0), ...])
def test_parse_duration(text, expected): ...

@pytest.mark.parametrize("text", ["", "abc", "10", "10x", "m", "-5s"])
def test_parse_duration_rejects_garbage(text): ...
```

**正例和反例成对写**。只写正例的测试只能证明"能跑"，不能证明"该拒绝的会拒绝"。

### (b) 假对象：把外部世界变小

`tests/fake_sim.py` 用 `+MODE=pass|uvm_error|fatal|nosummary|rc1|hang|crash|plain_*` 顶替真仿真器。
于是"超时"测试只需 400ms，"段错误"用 `os.kill(os.getpid(), SIGSEGV)` 就能造出来——**不需要 VCS/Questa，也不依赖 CI 装 EDA 工具**。

### (c) 全流程 + 回归

```python
def test_python_dash_m_works_from_project_root(make_project):
    """Regression test for D9: a config file must never shadow the package."""
    result = subprocess.run([sys.executable, "-m", "solouvm", "-c", "solouvm.conf.py", "gen"],
                            cwd=str(project.root), check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
```

三个习惯：

1. **每个 bug 修完，先写一个会失败的测试**，再修。测试名写 bug 的**语义**（`works_from_project_root`），docstring 指回决策编号（D9）。
2. **fixture 做成工厂函数**（`make_project(**overrides)`），而不是一个巨大的固定 fixture；
3. **捕获层次要对**：`capsys` 抓 Python 层的 `print`，`capfd` 才能抓到子进程/make 的输出。我在这上面浪费过一次调试。

**练法**：给你现在最"手工验证"的流程补一个 `fake_xxx.py`，让它能在 1 秒内跑完。

---

## 13. 我刻意避免的写法

| 反例 | 我改成 | 原因 |
|------|--------|------|
| `params: dict = {}` | `field(default_factory=dict)` | 可变默认值在多个实例间共享 |
| `if "UVM" in line` | `re.compile(r"\bUVM_(INFO|ERROR|FATAL|WARNING)\b")` | `SOLOUVM_DONE` 里含 `UVM`，子串判断会误判 |
| `def f(flag=False)` 位置传参 | `def f(*, flag=False)` | 调用处 `f(True)` 完全不可读 |
| 返回 `(ok, err)` 元组 | 返回 dataclass / dict，或抛异常 | 元组顺序靠记忆 |
| 在 `__init__` 里读 env / 起子进程 | 由调用方传入 | 无法测试、有副作用 |
| `shell=True` + 拼接字符串 | `shlex.split` / list argv；打印时用 `shlex.join` | 空格、引号、注入 |
| 一函数 200 行 | 按"做一件事"拆，私有函数 `_xxx` | 一屏读不完的代码没人敢改 |
| 全局 `CURRENT_CONFIG` | 显式传参（`cfg`） | 并发/重入时是雷 |
| 只写 happy path 测试 | 每个函数至少一个"垃圾输入"测试 | 边界才是 bug 的家 |
| 注释解释"这行在做什么" | docstring 写"为什么这样" | 代码能表达"做什么" |

---

## 14. 我在这个仓库里如何"边写边验证"

实际节奏（你照着做会发现效率比"全写完再跑"高很多）：

1. 写 `util.py` + `test_util.py`，`uv run pytest tests/test_util.py` —— 30 秒拿到反馈；
2. 写 `config.py` + `collect.py`，跑对应测试；
3. 写 `logcheck.py` 时**先写判定表测试**（`tests/test_logcheck.py` 里有 21 个测试），实现只要让测试变绿；
4. 写 `executor.py` 时先用最小 config 手工跑一次 `_exec`，看到 `status.json` 再补 e2e 测试；
5. 全部绿了跑 `ruff check .` 和 `--cov`，90% 以下的分支要么补测试，要么删代码；
6. 每修一个 bug，往提交信息里写"现象 → 根因 → 防回归手段"（见 `git log`）。

命令就三条：

```bash
uv run pytest -q
uv run ruff check .
uv run solouvm -c examples/demo/solouvm.conf.py regress
```

---

## 15. 给你准备的 5 个练习（按难度递增）

每个都有明确的验收标准，改完跑 `uv run pytest` 不能退。

### 练习 1：`gen --dry-run`（30 分钟）

只打印"将写入哪些文件、多少 case"，不落盘。

- 涉及：`cli.py`（argparse 参数 + SUPPRESS 的坑）、`makefile.py`（把"算内容"和"写文件"分离）
- 验收：新增测试断言"目录里没有生成物，但 stdout 有数量"

### 练习 2：`end --rerun-failed`（1 小时）

从 `results.json` 找出 `fail/timeout/error/killed`，再跑一次那一批。

- 涉及：`status.py`（读出坏 case）、`plan.py`（`select` 已有，按 `case_id` 过滤）、`cli.py`
- 关键点：**必须复用同一个 `run_id`**，否则 `end` 会把新结果判成 stale。想清楚这个再动手。

### 练习 3：`--shard i/n`（2 小时）

把 plan 均分成 n 份，只跑第 i 份。

- 涉及：`plan.py` 加一个纯函数 `shard(specs, index, total) -> list[CaseSpec]`
- 关键点：**分片必须基于已生成的 `plan.json`**（种子的随机性只抽一次），且 `shard` 要是纯函数才好测
- 验收：`shard(specs, 0, 3) + shard(specs, 1, 3) + shard(specs, 2, 3) == specs`（顺序无关的并集）

### 练习 4：junit 报告（半天）

`end` 额外写 `results.xml`。

- 涉及：`report.py` 加 `render_junit(results) -> str`，`xml.etree.ElementTree`（标准库，零依赖）
- 关键点：报告层不做判定，只做格式转换；`xfail` 要映射成 `<skipped>`，`xpass` 映射成 `<failure>`

### 练习 5：`gen` 生成 UVM 骨架（2~3 天）

README 承诺但还没实现的那块。

- 需要先决策：模板放哪、已存在文件是覆盖/跳过/报错、用户改过的文件怎么升级
- 建议先只做"**存在即跳过 + 打印 diff 提示**"，把覆盖策略留成 `--force`
- 验收：`examples/demo` 能 `gen` 出 `tb/tb_pkg.sv` 之类的骨架，且二次执行不冲突

每个练习都建议走一遍："先写会失败的测试 → 实现 → 跑 `ruff` → 跑覆盖率 → 写一条决策记录进 `doc/design-decisions.md`"。

---

## 一句话总结

> **先把契约（文件、退出码、边界）钉死，再用 dataclass 定数据形状，每层只做一件事且能在内存里被测试，最后用假对象 + 临时目录把全流程跑通。**

写 Python 的能力，主要长在"边界"上：边界处的校验、边界处的清理、边界处的数据形状。核心算法通常只有 20 行，剩下的两千多行都是为了让那 20 行在出错时依然可解释。
