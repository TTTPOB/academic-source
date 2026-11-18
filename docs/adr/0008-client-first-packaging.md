# ADR 0008：默认安装是命令行客户端，服务端是可选项

- 状态：Accepted
- 实施情况：已实现（提交 `f6e98d4`），单元测试和 CI 的安装冒烟测试覆盖。生产验收结果见完成报告。

## 背景

服务跑在 NAS 上，使用者在别的机器上通过 MCP 或命令行访问它。两种方式各有问题：

- **MCP 不适合长任务。** `acquire` 最多等 10 秒，之后模型只能反复调用 `job_status`。MCP 工具调用是同步的，agent 运行环境没法把它当成后台任务来管理。
- **命令行能等到任务结束，但装起来很重。** 命令行和服务端在同一个包里，只想当客户端用也要装上 `fastapi`、`uvicorn`、`mcp`、`pymupdf` 等全部服务端依赖。它也缺几样东西：运行中没有进度输出；被终止时服务端任务继续跑；只有 `get` 和 `batch`，搜索和查元数据还得靠 MCP。

## 决策

1. **一个包，两种装法。** 默认安装只依赖 `httpx`，提供客户端命令。运行服务所需的依赖全部放进 `server` extra。
2. **命令名为 `asource`**，`academic-source` 保留为别名，已有部署的启动命令不用改。
3. **客户端命令**：`get`、`batch`、`search`、`metadata`、`status`、`download`、`cancel`。服务地址来自 `--server` 或 `ACADEMIC_SOURCE_SERVER`；密钥来自 `ACADEMIC_SOURCE_API_KEY`，或 `--api-key-file` / `ACADEMIC_SOURCE_API_KEY_FILE` 指向的文件。
4. **需要 `server` extra 的命令**：`serve`、`mcp`、`prune`，以及不指定服务地址时在本进程里执行的 `get`、`batch`、`search`、`metadata`。没装 extra 时给出安装提示。
5. **下载必须是明确要求的。** `get` 和 `batch` 的含义就是把文件取到本机，另有 `--no-download` 可以关掉。`status` 只查询，不下载；取回一个已有任务的文件用 `download`。
6. **进度写到 stderr，结果是 stdout 上的一份 JSON。** 只在状态变化时打印一行。
7. **`get` 或 `batch` 被中断时，先请求服务端取消任务**（见 [ADR 0007](0007-job-cancellation.md)），再退出。
8. **客户端代码只能导入 `httpx` 和标准库。** 客户端逻辑在 `academic_source/client.py`；`interfaces/cli.py` 里凡是用到服务端模块的地方，都在具体命令内部导入。

## 理由与替代方案

- **单独建一个客户端仓库**：安装最干净，但服务端接口每改一次都要跨仓库同步，还要维护两套发布流程。放在一个包里，接口和客户端在同一个提交里改、用同一个版本号。
- **写成 agent 运行环境的插件**：能显示原生的进度条，但要新建包、单独发布、改配置。命令行客户端在后台 shell 里运行，已经能被当作后台任务管理：有任务 id，结束时有通知，能读输出，能终止。
- **`status --output` 顺带下载**：查询命令不应该有写文件的副作用。

## 后果

- **已有部署必须在安装参数里加上 `server`**，否则服务启动不了。NAS 的安装参数从 `cdp,fast,vpnsci` 改为 `server,cdp,fast,vpnsci`。
- 默认安装仍然包含服务端的源码文件，只是没有它们的依赖，占用少量磁盘。
- 以后给 `interfaces/cli.py` 或 `client.py` 加代码时，不能在模块顶层导入服务端的包。有测试和 CI 步骤检查这一点。

## 验证要点

- 屏蔽全部服务端依赖后，客户端命令能运行；`serve` 给出安装提示。
- CI 在一个只装了默认依赖的环境里运行 `asource --help`，并确认 `pydantic` 不存在。
- `--no-download` 和 `status` 不在本机留下文件；`download` 能取回。
- 中断正在等待的客户端后，服务端任务变为 `cancelled`。
