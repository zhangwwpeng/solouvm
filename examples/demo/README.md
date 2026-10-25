# Demo

一个可以直接跑通的最小工程，不需要任何 EDA 工具（用 `tb/fake_sim.py` 顶替仿真器）。

```bash
# 在仓库根目录
uv sync
uv run solouvm -c examples/demo/solouvm.conf.py regress
```

预期输出：

```
  pass=4  xfail=2  fail=0  timeout=0  error=0  killed=0  xpass=0  skip=0
```

（3 个 case × 2 个 seed，其中 `known_bug` 是 `expect_fail`，所以 4 pass + 2 xfail。）

只看单个 case：

```bash
uv run solouvm -c examples/demo/solouvm.conf.py run --case dut_smoke --seeds 1
```

生成物在 `examples/demo/.solouvm/`（已 `gitignore`）：`plan.json`、`out/<case_id>/status.json`、`results.json`。

换成真仿真器只要改 `solouvm.conf.py` 的 `simulator` / `sim`：

```python
CFG = {
    "simulator": "vcs",
    # 或自定义：
    # "sim": {"build": "vcs -full64 -sverilog -f {filelist} -o {simv}",
    #         "run": "./{simv}"},
}
```
