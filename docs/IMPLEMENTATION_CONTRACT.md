# 并行实现契约

这是当前实现批次的模块协作约定；架构目标见 ARCHITECTURE_PROPOSAL.md。用户已批准实施。所有代码注释用英文。

## 公共模型

`academic_source.domain` 与 `academic_source.settings` 由主代理负责。其他实现依赖这些类，不另造协议层领域模型。必要的接口调整先通知主代理。

## Store（存储子任务所有）

位置：`academic_source.infrastructure.storage.Store`。

- `Store(settings: Settings)`；`root` 为数据根 Path。
- `put_upload(filename: str, stream: BinaryIO) -> Upload`，流式大小限制，保留扩展名，清理目录分隔符；只支持文献输入格式 txt/md/bib/csv/tsv/tab/xlsx/json，不支持 cookie 上传。
- `upload_path(upload_id: str) -> Path`，未知 ID 抛 KeyError。
- `import_artifact(path: Path, *, kind: str, identifier: str | None, source: str, url: str | None = None, derived_from: str | None = None) -> Artifact`，复制到持久目录，路径不在模型中公开。
- `get_artifact(artifact_id: str) -> Artifact`；`artifact_path(artifact_id: str) -> Path`。
- `save_job(job: Job) -> None`；`get_job(job_id: str) -> Job`。
- `interrupt_jobs() -> None` 将残留 queued/running 置 interrupted，仅 Application 启动时调用。
- `get_cached(key: str) -> AcquisitionResult | None`，验证已登记文件仍存在。
- `put_cached(key: str, result: AcquisitionResult) -> None`。
- `close() -> None`。数据库每操作短连接或明确线程安全；别创建跨线程 SQLite connection 后假装安全。

实现 SQLite + 文件系统，工作目录 `root/work/<job_id>/`。不造通用 repository 框架，不实现分布式锁和多租户。

## Application（获取服务子任务所有）

位置：`academic_source.services.application.Application`。

- `Application(settings: Settings, store: Store | None = None, source: Source | None = None)`。
- 属性 `settings`、`store`。
- `search(query: str, limit: int = 10) -> list[dict[str, Any]]`。
- `resolve(identifier: str) -> dict[str, Any]`（统一规范化/标题解析）。
- `parse_list(*, upload_id: str | None = None, text: str | None = None) -> list[dict[str, Any]]`。
- `submit(request: AcquisitionRequest) -> Job`，持久化后后台执行，同一单篇逻辑适用于批量。
- `job(job_id: str) -> Job`。
- `wait(job_id: str, timeout: float = 0) -> Job`，有界等待，超时返回当前状态。
- `close() -> None`，停止接收并正确清理工作线程/浏览器资源。

source 接口与 engine 由此子任务定义，测试可注入 fake source；给协议子任务同步其简单构造用法。不要经过旧 MCP 或 Web 调业务；可复用旧 parser/search 和来源函数，新的调用不应再走两个完整旧引擎。浏览器同步操作须同线程，模块级 last_error 需串行或替换结果传递。交互策略必须在真实旧回退分支生效，不仅传一个未知 config 键。source_config 来自显式 Settings；默认值可来自旧 DEFAULT_CONFIG，但不偷偷加载 ~/.scansci-pdf 用户配置。

## 统一接口（接口子任务所有）

位置：`academic_source.app.create_app(settings: Settings | None = None, application: Application | None = None) -> FastAPI`；MCP 工厂 `interfaces.mcp.create_mcp(application: Application)`；CLI `interfaces.cli.main`。

一个 ASGI app 同时承载 /mcp 和 /api/v1：search、resolve、uploads、lists/parse、acquisitions、jobs/{id}、artifacts/{id}/content。应用提供的领域模型在协议层序列化，并附产物相对 download_url，禁止暴露服务端绝对路径。普通 HTTP-only 客户端应独立完成上传/解析/获取/取回。

MCP tools：search、resolve、parse_list、acquire、job_status。submit + bounded wait 不重复创建任务。stdio 工厂共用 Application。CLI 支持 serve、mcp（stdio）、get 和 batch，本地/--server 远端模式均可用；get/batch 能导出文件，支持上传本地列表。不要把远端输出路径传给服务器。

服务关闭拥有 Application 生命周期，MCP session_manager 正确启动。SDK 使用当前 uv.lock 1.27；实际验证路径不能 /mcp/mcp。新 UI 简单可用即可，不搬旧 UI 的业务逻辑。

## 所有任务约束

- 独立 worktree，各自只改分配文件；atomic commit，提交风格 feat:/fix:/test:/docs:。
- 不过度防御、不加无意义测试、不编造理论竞态；仅文件 ID 基本边界、真实输入错误、实际线程约束。
- 工具调用尽量批量，批量读取/编辑可以在一轮完成，减少成本。
- 无真实文献网络测试；只跑小而有意义的本地测试。每个子任务只在自身职责写 tests/academic_source/test_<area>.py，不动公共 conftest。
- 质量目标 ruff check/format 和 pyrefly basic（主代理配置）；不要大面积格式化旧源码。
- 不发布、不推送、不修改原作者命名空间的服务。JATS、模型清洗不实现。
