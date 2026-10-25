# solouvm

## 1. 总述

`solouvm` 是一个用于生成 UVM 验证框架和 UVM Runner 的 Python 库。

设计原则：

- **Python 是第一公民**：仅支持 Python 配置，不支持 YAML、JSON 等其他配置格式。
- **零第三方依赖**：仅使用 Python 标准库，无需安装第三方 Python 包。
- **声明式文件收集与 Case 生成**：用户编写的 Python 文件返回 `list`，用于文件收集、测试 Case 生成等操作。
- **只生成，不执行**：框架负责生成 Makefile 和相关文件，不负责实际的仿真执行。
- **环境无关**：项目可以复制到任意目录使用，不要求安装为 Python 包。
- **灵活扩展**：每个阶段提供 `pre-hook` 和 `post-hook`，允许用户根据自身需求扩展流程。

暂定 Python 版本要求：`Python >= 3.10`。

## 2. UVM Runner

### 2.1 总述

UVM Runner 通过统一入口 `main.py` 管理整个流程，并根据命令行参数分发到对应阶段。

整个流程分为五个步骤：

1. `main`：解析命令行参数并检查参数合法性。
2. `com`：收集编译文件，生成 filelist 和编译用 Makefile。
3. `run`：收集测试 Case，生成运行用 Makefile。
4. `user run`：由用户自行提交和执行仿真任务。
5. `end`：解析仿真日志，判断测试结果。

每个由 Runner 管理的阶段均提供 `pre-hook` 和 `post-hook`，供用户自定义扩展逻辑。

其中，**第 4 步由用户自行完成**。考虑到不同公司的计算集群、任务调度系统和仿真环境存在差异，solouvm 不规定统一的任务提交方式，也不负责实际的仿真执行。

### 2.2 Main 阶段

执行流程：

1. 执行 `pre_hook_main.py`，运行用户自定义逻辑。
2. 进入 `main.py`，解析命令行参数。
3. 检查命令及参数是否合法；若不合法，向用户提供错误信息并终止流程。
4. 执行 `post_hook_main.py`，运行用户自定义逻辑。

### 2.3 Com 阶段

执行流程：

1. 执行 `pre_hook_com.py`，运行用户自定义逻辑。
2. 加载一个或多个用户配置的 `filelist.py`，收集编译所需的文件。
3. 根据收集结果生成 `filelist.f`。
4. 执行 `post_hook_com.py`，运行用户自定义逻辑。
5. 生成编译所需的 Makefile 及相关 filelist 文件。

### 2.4 Run 阶段

执行流程：

1. 执行 `pre_hook_run.py`，运行用户自定义逻辑。
2. 加载一个或多个用户配置的 `case.py`，收集待运行的测试 Case。
3. 根据收集结果生成运行用 Makefile。
4. 执行 `post_hook_run.py`，运行用户自定义逻辑。

本阶段仅负责生成运行所需的 Makefile，不负责提交或执行仿真任务。

### 2.5 User Run 阶段

本阶段由用户自行完成，不属于 solouvm Runner 的自动执行流程。

用户根据所在公司的计算集群、任务调度系统及仿真环境，自行选择合适的方式提交和执行仿真任务。

例如，不同环境可能使用不同的集群任务提交命令，也可能直接在本地运行仿真。

solouvm 不规定具体的任务提交方式，仅通过前一阶段生成的 Makefile，为用户提供统一的运行入口。

### 2.6 End 阶段

执行流程：

1. 执行 `pre_hook_end.py`，运行用户自定义逻辑。
2. 解析仿真日志，判断测试 Case 是否通过。
3. 执行 `post_hook_end.py`，运行用户自定义逻辑。

本阶段负责收集和分析仿真结果，不负责启动仿真任务。

## 3. Hook 机制

每个 Runner 阶段提供两个扩展入口：

- `pre-hook`：在阶段核心逻辑执行之前运行。
- `post-hook`：在阶段核心逻辑执行之后运行。

Hook 用于满足不同项目和不同公司的定制化需求，同时尽量保持 Runner 核心逻辑的稳定。

对应文件命名如下：

```text
pre_hook_main.py
post_hook_main.py

pre_hook_com.py
post_hook_com.py

pre_hook_run.py
post_hook_run.py

pre_hook_end.py
post_hook_end.py
```

Hook 的具体输入、返回值、异常处理和执行失败后的行为，由 solouvm 的接口规范统一定义。

## 4. 用户配置接口

用户通过 Python 文件提供配置和扩展逻辑。

例如：

- `filelist.py`：返回编译文件列表。
- `case.py`：返回测试 Case 列表。
- `pre_hook_*.py`：阶段执行前的自定义逻辑。
- `post_hook_*.py`：阶段执行后的自定义逻辑。

配置文件通过返回 `list` 提供数据，由 Runner 负责收集和处理。

框架不要求用户编写额外的 YAML、JSON 等配置文件。

## 5. 设计边界

solouvm 负责：

- UVM 验证框架生成。
- Python 配置加载与处理。
- 编译文件收集。
- 测试 Case 收集。
- filelist 生成。
- Makefile 生成。
- 仿真日志解析与测试结果判断。
- 通过 Hook 提供扩展能力。

solouvm 不负责：

- 实际的仿真执行。
- 特定公司的集群任务提交。
- 特定调度系统的管理。
- 强制规定用户的项目目录结构及工作流。

**核心目标：以 Python 为唯一配置语言，提供轻量、可移植、可扩展的 UVM 框架生成与 Runner 工具，同时将具体仿真环境和任务调度的控制权交给用户。**
