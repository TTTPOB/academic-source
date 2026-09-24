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
- `interfaces.api`、`mcp`、`cli`、`web`：共享应用服务的协议适配；MCP 仅提供搜索、解析、列表解析、获取和任务状态等用户动作。

## 输入与产物

获取请求恰好选择一种输入：标识符列表、已上传文件的 `upload_id` 或内联文本。上传在服务端按块流式写入，保留文件扩展名并受大小限制；列表解析支持文本、BibTeX、表格及 JSON 等当前解析器支持的格式。

请求不接受客户端文件系统路径。客户端与服务端无需共享目录：远程 CLI 先上传本地列表，获取后经 HTTP 下载产物并保存在客户端指定目录。HTTP/MCP 返回产物 ID、文件名、大小、媒体类型和来源信息，不公开服务器绝对路径；实际文件由服务端数据目录管理。

PDF 是获取成功的基础产物；可选 Markdown、补充材料和 BibTeX 结果单独报告，不因可选转换失败而丢弃已取得的 PDF。只登记经过校验且可读取的 PDF，不以文件头或正文长度单独判成功。

## 来源与获取策略

`LegacySources` 在新的统一来源边界中调度保留的站点实现，包括开放获取服务、出版商 URL/HTML/浏览器规则、arXiv，以及配置后可用的机构访问通道（如 CARSI、WebVPN、EZProxy、机构会话和浏览器批量路径）。这保留有效站点知识，不代表所有来源、浏览器依赖或订阅能力在每个环境均可用。

来源策略由获取请求决定。Elsevier API key 可用时，`10.1016/` 的 DOI 在 `fastest` 和 `legal_only` 策略中优先尝试 Elsevier API；失败时按普通来源顺序回退。API/XML 成功不等于正文授权，最终仍以可读 PDF 校验为准。

来源尝试保留来源名、状态、原因和诊断信息。单篇结果与批量逐项结果共享成功判定；外部站点错误不会被当成成功，任务级意外错误记录在服务日志并以通用错误状态返回，不把内部异常细节伪装成站点结论。

## 任务、文件与缓存

一个数据目录由一个运行实例使用，不支持多个进程共享同一目录。SQLite 的 `catalog.sqlite` 保存上传、产物、任务、逐项结果和获取缓存；上传、产物和工作文件位于同一数据目录的独立子目录。SQLite 连接按操作创建，不在线程间共享。

任务在进程内执行并持久化状态：`queued`、`running`、`succeeded`、`partial`、`failed`、`interrupted`。当前任务执行器只有一个工作线程，批量任务逐项保存进度；限定的 HTTP 开放获取来源可并发探测，浏览器及会话操作仍在所属任务线程中执行。结果缓存按标识符、策略、配置、来源和可选导出请求区分；命中前检查已登记的产物文件仍存在。PDF 与可选导出缓存分开，未成功生成的可选导出可重试。

应用关闭时停止接收任务、取消尚未开始的任务，并等待运行中的调用结束；不承诺强行终止第三方调用或恢复浏览器执行现场。启动时将上次遗留的未完成任务标为 `interrupted`，不自动续跑；已完成且仍存在的产物可以继续下载。

## 浏览器生命周期与 PDF 流

本地 Patchright、CloakBrowser、Camoufox 后端由应用按配置启动并拥有浏览器/上下文生命周期。`cdp` 后端不同：只在实际执行到浏览器来源时延迟连接配置的 CDP URL，借用 Chrome 已存在的默认持久 context；应用只拥有连接和自己创建的标签页。关闭标签页或任务会断开 CDP 客户端连接，不关闭 Chrome、不关闭其默认 context，也不删除浏览器 profile。浏览器操作须留在创建同步 Playwright 对象的工作线程。

浏览器从已认证文章页发起同源 PDF 流请求，不依赖浏览器 PDF 阅读器或客户端与服务端共享文件路径。默认总时限为 120 秒、大小上限为 100 MiB，可通过 `source_config` 调整；超时、超限或内容校验失败会清理临时文件，不登记半成品。配置的 CDP 端点必须能从应用进程访问。具体外部 Chrome 参考部署、配置及未完成的运行验收状态见 README；原生浏览器探针不等同于应用 HTTP/MCP 生产全链路验收。

应用没有额外的浏览器 MCP 工具、远程桌面或浏览器管理接口。MCP 只暴露获取业务动作，产物仍通过 HTTP 下载。

## 范围与保证

原生 JATS 获取、PDF→JATS 和模型清洗尚未实现。站点可用性、机构登录状态、订阅授权和单篇论文的可获取性均不作保证；权限取决于来源、网络、会话及配置。保留上游 publisher 抓取实现及其知识，不因新应用边界删除其有效规则。项目采用 Apache-2.0，并保留上游版权与归属；未包含的闭源 Cython 源码不是本架构依赖。
