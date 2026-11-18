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
- `academic_source.app`、`services.application`：组合 HTTP/MCP 生命周期，执行搜索、DOI/arXiv 元数据查询、获取、缓存、任务和导出。
- `services.discovery`、`lists`、`exports`：标识规范化、DOI/arXiv 元数据、列表输入和可选产物转换；继续复用所需的上游解析及抓取知识。
- `sources`：根据来源注册表和请求策略排出来源顺序，逐个尝试。OA 快路径是自有实现；其余来源借用 `scansci_pdf` 的函数，经适配器接入。
- `infrastructure.storage`、`documents`：SQLite 记录、文件落盘和 PDF 可读性检查。
- `interfaces.api`、`mcp`、`cli`、`web`：共享应用服务的协议适配；MCP 提供搜索、DOI/arXiv 元数据、列表解析、获取和任务状态；仅 stdio 另提供 `export_artifacts`，不代理 HTTP。

## 检索与按类型元数据

HTTP/MCP 的 search 通过 Application → services.discovery → scansci_pdf.search，只使用 OpenAlex。Python 入口为 search_papers，与公开服务共用本项目的单一检索实现。默认源内相关性顺序不做本地重排；现有 Python 引用数/发布日期排序（可选 asc/desc）和单边/双边年份范围直接传给 OpenAlex。公开 HTTP/MCP 参数仍为 query、limit。

每次获取 min(100, max(30, limit × 3)) 条候选，不翻页；沿用带 DOI 的结果边界，按明确类型、撤稿状态和少量完整泛标题过滤后应用 limit（1–100）。保留综述、预印本、完整会议论文及未知类型，排除明确非论文对象和 is_retracted=true 的原文。结果投影保留 type、撤稿状态和有效 ISO 发布日期，缺值省略。空结果与来源故障分开：真实无命中为列表，预期网络/HTTP/JSON 故障为 DiscoveryError，HTTP 翻译为 502，MCP 使用 SDK 工具错误机制。

MCP get_metadata(id_type='doi', doi='...') / get_metadata(id_type='arxiv', arxiv_id='...') 和 HTTP POST /api/v1/metadata {id_type, doi} / {id_type, arxiv_id} 通过 Application → services.discovery.get_metadata 显式分流：doi 经 normalize_doi 查 Crossref，arxiv 经局部严格规范化并保留请求版本后查 arXiv 原生 API 的 id_list。两者通过 scansci_pdf.upstream_client 的 get_json/get_text 复用现有网络会话、request_timeout、代理和 User-Agent；Atom 使用 stdlib ElementTree，无独立 transport 或缓存层。

请求的 id_type 必填，只允许对应的 doi 或 arxiv_id；服务层统一拒绝缺失、错配或同时提供两种 ID。所有响应保留 id_type、metadata_source、metadata_status，以及类型专属的规范请求 doi 或 arxiv_id。Crossref 保留现有书目、类型和有向 DOI 关系/更新证据；arXiv 返回精简书目、摘要、日期和实际 version，真实发表 DOI 单列 published_doi。metadata_status 区分匹配记录 found、Crossref 404 / arXiv 合法空 feed 的 not_found、网络/HTTP/JSON/XML/错误 entry/记录不匹配的 unavailable；意外内部错误直接传播。请求身份和 arXiv 版本语义见 README。

get_metadata 不做标题推断，来源只由 id_type 决定。标题搜索使用 OpenAlex；内部获取直接复用现有标题相似度匹配，匹配失败保留原标题，不新增元数据查询依赖。直接 DOI/arXiv 获取无需先查元数据。内部 DOI→idconv→PMCID 与 Europe PMC/PMC 的 PDF 候选转换保持原链路，不提供公开 PMID/PMCID 输入。关键词检索与按需元数据查询互不阻塞，元数据来源不参与候选召回或排序。

## 上游请求控制与错误

scansci_pdf.upstream_client 是 network.fetch 与 JSON/text API 客户端的共享准入点。OpenAlex API 和 Content 共用服务级速率控制，计费观察按实际 api_key 或匿名出口隔离；Crossref 元数据使用 5 RPS / 并发 1，arXiv 元数据使用 3 秒间隔 / 并发 1。stream 响应在消费者 close 后释放并发名额，业务消费者在成功与失败路径均关闭响应。控制保留现有会话、代理、超时与成功 JSON probe 缓存；浏览器渠道不属于此 HTTP 控制点。

scansci_pdf.upstream_errors 集中定义 DiscoveryError、UpstreamError 和 RateLimited。429 根据安全响应证据记录 kind、UTC retry_at 及可用额度信息，不把原始正文或带凭据 URL 放进错误。已知冷却立即拒绝，第一版不自动重试；OpenAlex 日预算不足只拒绝成本超过 remaining 的操作，免费 singleton 可继续，其他服务独立。

搜索将限流交给现有 HTTP 502 / MCP 工具错误边界；标题获取有独立的 RateLimited 分支，保留 rate_limited、retry_later 和 rate_limit。Crossref/arXiv 元数据限流为保留身份与来源的 unavailable，只有限流返回 rate_limit。OA 在尝试记录中保留安全限流信息并继续其他来源；最终失败只透传选中的汇总原因及其恢复信息，不改变来源优先级。Attempt、SourceFailure、AcquisitionResult 的 rate_limit 字段贯通来源、任务持久化和精简对外投影；成功与非限流结果省略空字段。意外内部异常仍传播或记录服务日志。

## 耗时、排队与尝试日志

任务记录三个时间：`created_at`（提交）、`started_at`（工作线程开始执行）、`updated_at`（最近一次写入）。排队时间是 `started_at − created_at`。每篇结果的 `elapsed_seconds` 从轮到这篇开始计，包含标题解析、缓存查找、来源尝试和可选导出，不含排队。

对外投影按任务状态附加进度信息：

| 状态 | 字段 | 含义 |
|---|---|---|
| `queued` | `queue.ahead` | 排在前面的任务数，包括正在执行的那个 |
| `running` | `progress.current` / `total` / `identifier` | 正在处理第几篇、共几篇、这篇的标识符 |

`queue.ahead` 在读取时由内存中的提交顺序算出，不写入任务记录。

每次来源尝试（OA 元数据查询、候选下载、CDP 重试、各出版商和其他渠道）在 `Attempt.seconds` 记录耗时，并由 `academic_source.sources` 日志器写一行 INFO：

```text
attempt identifier=10.1126/science.aad8559 source=EuropePMC status=failed reason=network_error candidates=- seconds=30.412
```

日志行不含 `message`，因为它可能引用下载地址。`serve` 命令把 `academic_source` 日志器设为 INFO 并输出到 stderr。

OA 元数据查询成功时 `Attempt.status` 为 `discovered`，表示服务返回了这篇文献的记录。`Attempt.candidates` 是从该记录得到的候选地址数：0 表示有记录但没有开放版本。

`started_at`、`elapsed_seconds`、`seconds`、`candidates` 在旧任务记录里不存在，读取时为空，对外投影省略空值。

HTTP `POST /api/v1/jobs/{job_id}/cancel` 与 MCP `cancel_job(job_id)` 均返回与任务查询相同的投影。排队任务立即进入 `cancelled`，不再计入 `queue.ahead`；执行中的任务继续处理当前论文（包括来源尝试与导出），结束后进入 `cancelled`，不会再开始下一篇。已完成的结果与产物仍可访问。取消进行中可继续轮询任务状态；已进入终态的任务不会改变。服务重启仍把遗留的 `queued`、`running` 任务标为 `interrupted`。

## 输入与产物

获取请求恰好选择一种输入：标识符列表、已上传文件的 `upload_id` 或内联文本。上传在服务端按块流式写入，保留文件扩展名并受大小限制；列表解析支持文本、BibTeX、表格及 JSON 等当前解析器支持的格式。

请求不接受客户端文件系统路径。客户端与服务端无需共享目录：远程 CLI 先上传本地列表，获取后经 HTTP 下载产物并保存在客户端指定目录。HTTP/HTTP MCP 返回产物 ID、文件名、大小、媒体类型、来源及 HTTP `download_url`；stdio 返回相同元数据但不附无法使用的 HTTP 链接。仅 stdio 的 `export_artifacts(artifact_ids, output_dir)` 从已登记文件复制到 stdio 进程所在机器的目录（容器中须挂载），返回实际文件路径；文件名前缀为产物 ID 以防同名覆盖。不传 base64、不重新下载。实际文件由服务端数据目录管理。

PDF 是获取成功的基础产物；可选 Markdown、补充材料和 BibTeX 结果单独报告，不因可选转换失败而丢弃已取得的 PDF。只登记经过校验且可读取的 PDF，不以文件头或正文长度单独判成功。

## HTTP 认证与一次性下载

HTTP 控制面默认要求静态 Bearer 服务 key，可同时配置多个 key 以支持轮换；显式 `auth_mode=off` 适用于由部署者选择的可信环境。此认证覆盖普通 API、MCP、永久产物下载和页面入口。stdio 属于本地进程入口，不要求 HTTP key。服务 key 只用于入站认证，不进入上游请求、下载 URL 或任务元数据。

HTTP MCP 的 `acquire`、`job_status` 为已登记产物返回绝对 `download_url`、UTC `download_expires_at` 与 `download_single_use=true`。默认基址取自本次 MCP HTTP 请求的 ASGI scheme、host 和应用 root path，agent 无需另传 host；`public_base_url` 可覆盖为实际公网基址。反代须保留正确 Host，并只从受信代理接收 forwarded 信息；应用不自行解释任意 `X-Forwarded-*` 头。普通 HTTP API 保持 `/api/v1/artifacts/{id}/content` 永久受保护路径，远程 CLI 对其携带服务 key；stdio 仅保留产物元数据和本机导出。

`Application.downloads` 持有单进程内存能力表。每个产物至多保留一个尚有效的 32 随机字节 URL-safe token；同一结果的嵌套/顶层产物与连续轮询复用它。token 绑定一个产物及最多六小时的到期时间，消费或到期后，下次 `job_status` 签发新链接。关闭或重启应用清除全部 token；任务、产物、SQLite 与结果缓存均不保存这些链接。签发和消费用一把小锁串行化，防止并发 GET 重复领取；部署维持单服务进程。

独立 `GET /downloads/{token}` 无需 Bearer 或 Cloudflare headers，直接以 attachment 发送登记文件，不复制或删除原产物。首次有效 GET 在开始传输前消费 token；中途断线也已消费，客户端须重新调用 MCP 取链接。不存在、到期和已消费 token 统一返回 404。HEAD/POST 返回 405，Range 返回 400，均不消费；只支持整文件响应，不提供 206、续传或 304。永久产物路径继续支持原有 Range 行为和认证。

独立响应头中间件为 MCP 与下载响应（含拒绝及错误）设置 `Cache-Control: no-store`，下载另设 `Referrer-Policy: no-referrer`。Uvicorn 的常规 access log 将下载路径 token 及 query 隐去，包含 root path 前缀的部署也适用。URL 本身是秘密：转发就转交该文件的一次读取权，预取/安全扫描若发 GET 也会消费。外部代理须对下载 namespace 单独绕过控制面认证和缓存，并自行避免记录完整能力 URL；本地日志过滤不能控制代理日志。

## 来源与获取策略

`LegacySources` 在新的统一来源边界中调度保留的站点实现，包括开放获取服务、出版商 URL/HTML/浏览器规则、arXiv，以及配置后可用的机构访问通道（如 CARSI、WebVPN、EZProxy、机构会话和浏览器批量路径）。这保留有效站点知识，不代表所有来源、浏览器依赖或订阅能力在每个环境均可用。

来源策略由获取请求决定。`fastest`、`oa_first`、`legal_only` 先执行轻量 OA 快路径：逐个查询 Unpaywall、OpenAlex、Europe PMC core 和 PMC 标识转换，发现候选即验证文件，取得可读 PDF 后立即停止，不等待其余元数据。保留 PDF 链接、公开落地页和仓储标识；识别 PMCID 后先尝试 Europe PMC 网站的 `api/getPdf?pmcid=...`。同一次获取内 HTTP 候选去重，回退阶段不重复查询已完成的 OA 服务；失败仍接原有合法、灰色及已配置机构渠道。`scihub_first`、`grey_only`、`scihub_only` 等显式策略保持原顺序。

Elsevier API key 可用时，`10.1016/` 的 DOI 在 `fastest` 和 `legal_only` 的 OA 快路径未成功后，仍优先尝试 Elsevier API，再继续其他出版商来源。API/XML 成功不等于正文授权，最终仍以可读 PDF 校验为准。

### 来源注册表

`sources.registry` 的 `PROVIDERS` 表列出全部来源，每个来源一项：

| 字段 | 含义 |
|---|---|
| 标签 | 来源名，如 `Unpaywall`、`ElsevierBrowser`，出现在尝试记录和日志里 |
| 类别 | `oa`、`publisher`、`grey`、`institutional`，策略按类别排序 |
| 归属 | `owned`：在 `academic_source/sources/` 下的自有实现；`borrowed`：`scansci_pdf` 的函数经适配器接入 |
| 后端 | `http` 或 `browser`。只有[逐函数核对过](HTTP_SOURCE_AUDIT.md)的来源标 `http` |
| 站点键 | 这个来源主要访问的域名或服务名 |
| 启用条件 | 依赖配置的来源在什么条件下参与，例如 OpenAlexContent 需要 API key |

各策略的来源顺序由这张表生成：类别之间的先后取决于策略，类别内部按表中的顺序。出版商类别例外，一个 DOI 对应哪些出版商来源、谁先谁后，仍由 `scansci_pdf.sources.publishers` 的出版商表决定。后端和站点键目前只是记录，还不影响执行；它们是[并行调度设计](PARALLEL_SCHEDULING.md)的输入。

### 来源结果与错误类型

每次来源尝试的结果是 `SourceSuccess`，或 `sources.errors` 里定义的一种错误：`Paywall`、`Blocked`、`AuthRequired`、`RateLimited`、`ConfigNeeded`、`NetworkError`、`InvalidPDF`、`NotFound`、`BrowserError`、`InternalError`、`UnknownLegacyError`。这是一个封闭的集合，`present_error()` 用 `match` 处理每一种，漏掉任何一种时 pyrefly 会报错。

借用的来源函数有三种报告失败的方式：抛异常；返回带 `error_type`、`reason` 或 `action` 的字典；返回 `None`，原因留在 `_publisher_strategies_core` 的模块级变量里。`sources.legacy.call_legacy()` 是唯一把这三种方式转换成错误类型的地方，也是唯一读取那个模块级变量的地方。借用来源返回了不认识的原因时，用 `UnknownLegacyError` 原样保留。

`present_error()` 同时决定三件事：这种错误在尝试记录里的 `reason` 字符串；一篇论文所有来源都失败时，哪个错误代表整体结果（例如 Cloudflare 拦截优先于付费墙，付费墙优先于网络错误）；对外的 `reason`（例如 `paywall` 对外是 `auth_required`）。这些字符串和优先级与引入错误类型之前完全一致。

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
