# academic-source 架构

本文描述当前实现边界。安装、配置及外部 Chrome 的部署操作见 [README](../README.md)；CI 负责持续检查，不在此维护逐次测试或部署记录。

## 应用边界

这是一个 Python 单体服务：HTTP API、Web 页面和 Streamable HTTP MCP 共用一个 `Application`、一个存储目录；stdio MCP 与本地 CLI 也使用相同应用服务。协议入口负责验证、序列化和文件传输，不自行编排来源。

```text
HTTP API / Web / HTTP MCP ─┐
stdio MCP / CLI ────────────┴─> Application ─> Source boundary ─> scansci_pdf 来源实现
                                   │                 │
                                   └─ Store           └─ HTTP / 本地浏览器 / CDP / 机构会话
                                      SQLite + 文件
```

主要模块：

- `academic_source.domain`：请求、任务、逐篇结果、获取尝试、产物及来源记录。
- `academic_source.settings`：运行设置和显式 `source_config`；默认数据目录为 `~/.academic-source`，配置文件为其中的 `settings.json`。
- `academic_source.app`、`services.application`：组合 HTTP/MCP 生命周期，执行搜索、解析、获取、缓存、任务和导出。
- `services.discovery`、`lists`、`exports`：标识解析、列表输入和可选产物转换；继续复用所需的上游解析及抓取知识。
- `sources`：统一选源和结果边界，直接调用保留下来的来源函数；不把旧下载编排器作为新的总入口。
- `infrastructure.storage`、`documents`：SQLite 记录、文件落盘和 PDF 可读性检查。
- `interfaces.api`、`mcp`、`cli`、`web`：共享应用服务的协议适配；MCP 提供搜索、解析、列表解析、获取和任务状态；仅 stdio 另提供 `export_artifacts`，不代理 HTTP。

## 输入与产物

获取请求恰好选择一种输入：标识符列表、已上传文件的 `upload_id` 或内联文本。上传在服务端按块流式写入，保留文件扩展名并受大小限制；列表解析支持文本、BibTeX、表格及 JSON 等当前解析器支持的格式。

请求不接受客户端文件系统路径。客户端与服务端无需共享目录：远程 CLI 先上传本地列表，获取后经 HTTP 下载产物并保存在客户端指定目录。HTTP/HTTP MCP 返回产物 ID、文件名、大小、媒体类型、来源及 HTTP `download_url`；stdio 返回相同元数据但不附无法使用的 HTTP 链接。仅 stdio 的 `export_artifacts(artifact_ids, output_dir)` 从已登记文件复制到 stdio 进程所在机器的目录（容器中须挂载），返回实际文件路径；文件名前缀为产物 ID 以防同名覆盖。不传 base64、不重新下载。实际文件由服务端数据目录管理。

PDF 是获取成功的基础产物；可选 Markdown、补充材料和 BibTeX 结果单独报告，不因可选转换失败而丢弃已取得的 PDF。只登记经过校验且可读取的 PDF，不以文件头或正文长度单独判成功。

## 来源与获取策略

`LegacySources` 在新的统一来源边界中调度保留的站点实现，包括开放获取服务、出版商 URL/HTML/浏览器规则、arXiv，以及配置后可用的机构访问通道（如 CARSI、WebVPN、EZProxy、机构会话和浏览器批量路径）。这保留有效站点知识，不代表所有来源、浏览器依赖或订阅能力在每个环境均可用。

来源策略由获取请求决定。Elsevier API key 可用时，`10.1016/` 的 DOI 在 `fastest` 和 `legal_only` 策略中优先尝试 Elsevier API；失败时按普通来源顺序回退。API/XML 成功不等于正文授权，最终仍以可读 PDF 校验为准。

来源尝试保留来源名、状态、原因和诊断信息。单篇结果与批量逐项结果共享成功判定；外部站点错误不会被当成成功，任务级意外错误记录在服务日志并以通用错误状态返回，不把内部异常细节伪装成站点结论。

## 任务、文件与缓存

一个数据目录由一个运行实例使用，由 Application 的跨进程文件锁确保同一时间只有一个实例；锁在中断历史任务前取得，关闭时释放。SQLite 的 `catalog.sqlite` 保存上传、产物、任务、逐项结果和获取缓存；上传、产物和工作文件位于同一数据目录的独立子目录。SQLite 连接按操作创建，不在线程间共享。

任务在进程内执行并持久化状态：`queued`、`running`、`succeeded`、`partial`、`failed`、`interrupted`。当前任务执行器只有一个工作线程，批量任务逐项保存进度；来源尝试按顺序执行，浏览器及会话操作仍在所属任务线程中执行。PDF 缓存按标识符、策略、来源与访问相关配置区分；精确排除输出目录、会话缓存目录、已禁用的来源并发参数及阅读器等待配置。可选导出逐项按 PDF 产物 ID、种类与相关配置缓存，成功项跨请求组合复用，失败项单独重试；返回缓存前检查登记文件仍存在。补充材料返回空列表不能证实永久没有材料，因此不缓存空结果。任务的顶层 artifacts 从逐篇结果派生，不在任务 JSON 重复保存；历史含此字段的 JSON 仍可读取。

来源接口返回明确的成功/失败模型，准备与清理由任务工作线程执行，应用层不直接管理浏览器模块。应用正常关闭时停止接收任务并排空已接受的任务队列；不承诺强行终止第三方调用或恢复浏览器执行现场。启动时将上次遗留的未完成任务标为 `interrupted`，不自动续跑；已完成且仍存在的产物可以继续下载。停止服务后可用 `prune` 预览或 `prune --apply` 清理 30 天以上无任务引用的上传、失效缓存、无任务/有效缓存引用产物及空工作目录；保留任务记录与有效缓存引用，运行实例占用目录时拒绝清理。

CLI 独占进程信号处理：首个 SIGINT 或 SIGTERM 请求优雅排空，第二个 SIGINT 直接以 130 退出，不等待阻塞的工作线程；SIGTERM 不累加 SIGINT 次数，关闭回调只触发一次。HTTP 入口禁用 Uvicorn 的信号接管与退出重放，避免一次 Ctrl+C 被重复计算。独立 stdio 复用 MCP SDK 传输；首信号完成应用排空、资源释放并刷新输出后以 0 结束进程，避免 SDK 在客户端保持 stdin 打开时留下不可取消的读取线程。独立 Application 不安装全局信号处理器。

## 浏览器生命周期与 PDF 流

本地 Patchright、CloakBrowser、Camoufox 后端由应用按配置启动并拥有浏览器/上下文生命周期。`cdp` 启动时在工作线程安排一次非致命预检（`browser_cdp_timeout` 默认 5 秒），只连接、检查默认 context 并断开，不开页、不清理；预检失败不阻断 HTTP 来源，也不缓存为永久状态。正式浏览器获取按需连接并借用 Chrome 已存在的默认持久 context。`cache_dir/cdp_owned_targets.json` 登记确切创建的 target，下一次正式连接恢复、只关闭登记页；历史未知归属页不清理，`SIGKILL` 落在建页与登记之间也无法保证回收。断开 CDP 客户端连接不关闭 Chrome、默认 context 或 profile；同步 Playwright 对象只在所属工作线程操作。普通 CDP 路径不适用 `browser_restart_every`，不建议设为非零；CDP forward 保留。

Science HTTP 快路径使用 `cache_dir/science_http_state.json` 中成对的 UA/cookie 快照；不迁移旧全局 UA 缓存，下一次浏览器成功后重获。快照不保证站点接受。`science_http_proxy` 非空显式值优先；未设置或为 `null` 时，本地浏览器沿 `browser_static_proxy` → `network_proxy`，CDP 沿 `SCANSCI_PDF_PROXY` → `network_proxy`；空串表示直连，不假设外部 Chrome 出口。Science 阅读器普通加载宽限 `science_reader_grace` 默认 5 秒，遇到 challenge 的等待上限 `science_reader_timeout` 默认 60 秒。HTTP 快路径成功不需要 CDP 工作连接，但不排除应用启动时的预检。

浏览器从已认证文章页发起同源 PDF 流请求，不依赖浏览器 PDF 阅读器或客户端与服务端共享文件路径。默认总时限为 120 秒、大小上限为 100 MiB，可通过 `source_config` 调整；超时、超限或内容校验失败会清理临时文件，不登记半成品。配置的 CDP 端点必须能从应用进程访问。具体外部 Chrome 参考部署和配置见 [专题文档](SOURCES_AND_CDP.md)；原生浏览器探针不等同于应用 HTTP/MCP 生产全链路验收。

应用没有额外的浏览器 MCP 工具、远程桌面或浏览器管理接口。HTTP MCP 产物通过 HTTP 下载；独立 stdio 通过本机 `export_artifacts` 复制产物。

## 范围与保证

原生 JATS 获取、PDF→JATS 和模型清洗尚未实现。站点可用性、机构登录状态、订阅授权和单篇论文的可获取性均不作保证；权限取决于来源、网络、会话及配置。保留上游 publisher 抓取实现及其知识，不因新应用边界删除其有效规则。项目采用 Apache-2.0，并保留上游版权与归属；未包含的闭源 Cython 源码不是本架构依赖。
