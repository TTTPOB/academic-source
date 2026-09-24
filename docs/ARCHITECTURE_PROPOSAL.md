# academic-source 架构提案

状态：架构方向已获批准并进入实现。本文保留目标设计；当前完成度、限制与验证记录见 IMPLEMENTATION_STATUS.md，不能将所有目标理解为已经完成。

## 1. 目标与判断

目标是获得干净、可持续维护的学术资源获取服务，而不是对 scansci-pdf 做最小补丁。保留现有出版商规则、PDF URL 发现、浏览器下载、机构访问及相关数据；重新组织业务编排、文件管理和对外接口。

不根据代码是否由 AI 生成判断质量。当前可确认的问题是入口业务不一致、存在多套编排器、文件系统路径泄漏到远端协议，以及业务配置和运行环境配置混在一起。

初稿阶段只做静态调查，后续已获授权实施。原生 JATS 与模型转换仍不实施；用户明确这是独立新项目，不要求兼容旧命令、工具名和响应，核心是保留来源抓取能力。

## 2. 已核对的现状

- `src/scansci_pdf/main.py:22-78`：stdio MCP、HTTP MCP、Web 是分开的启动分支。
- `src/scansci_pdf/server.py:35-108`：下载工具还承担错误提示、附件和 Markdown 导出，返回服务端路径。
- `src/scansci_pdf/web.py:97-177`：Web 自行解析标题、调用下载器并返回 PDF，搜索与正式 MCP 走不同函数。
- `src/scansci_pdf/server.py:1517-1545` 与 `paperlist.py:21-50`：批量文件输入是服务端文件路径，不是客户端上传。
- `src/scansci_pdf/sources/__init__.py:862-1048`：下载入口同时处理配置、缓存、目录扫描、元数据、选源、竞速、机构会话、命名和索引。
- `src/scansci_pdf/fetcher.py:55-114`：另一套抓取流程把全文长度、PDF、摘要等质量状态混入成功判定；不同调用者对“成功”的含义不一致。
- `src/scansci_pdf/config.py:36-126`：选源、代理、浏览器、密钥、目录、桌面 UI 等设置混合。
- `src/scansci_pdf/cache.py:20-45`：缓存存储结果 JSON，而结果携带文件路径；文件记录与请求结果未分离。
- `uv.lock:759-777` 锁定 MCP 1.27.0，但 `pyproject.toml` 允许更宽版本；ASGI 挂载和会话生命周期尚未实际验证。

## 3. 总体选择：一个模块化 Python 应用

保持 Python 与 uv，不换语言、不拆微服务、不引入消息中间件。一个服务进程、一套应用服务、一个持久化目录。

```text
HTTP API ─┐
HTTP MCP ─┼── Application services ── Acquisition engine ── Source adapters
stdio MCP ┤           │                      │                    │
CLI ──────┘           │                      └── HTTP / Browser / Sessions
                      └── SQLite catalog + local artifact files
```

- 入口层负责协议转换，不决定选源策略，不自行做 Markdown 转换或补 DOI。
- 应用层负责搜索、解析、获取、导入列表、导出，以及任务状态。
- 获取引擎负责候选源选择、执行顺序、预算、成功判定及尝试记录。
- 来源适配器负责站点知识，基础设施负责网络、浏览器会话、文件和数据库。
- 依赖关系指向内层：领域类型不导入 FastAPI/MCP；应用层不依赖具体路由；来源实现不调用入口层。
- 用显式构造函数和少量 Protocol 即可，不建立通用 DI 容器、插件市场或工作流 DSL。

## 4. 建议目录

这是目标组织，不是现在就把旧文件批量移动。

```text
src/academic_source/
  app.py                 # Composition root and resource lifecycle
  settings.py            # Typed configuration
  domain/
    papers.py            # Identifiers and metadata
    artifacts.py         # File references and provenance
    acquisition.py       # Request, result, attempt, policy
  services/
    discovery.py         # Search and identifier resolution
    acquisition.py       # Single-paper acquisition
    lists.py             # Uploaded/inline paper-list parsing
    batches.py           # Batch scheduling and aggregation
    jobs.py              # Small persistent job lifecycle
    exports.py           # Existing Markdown/citation export
  acquisition/
    planner.py           # Source selection and fallback order
    executor.py           # Bounded execution and attempt recording
  sources/
    registry.py          # Explicit source registration
    oa/
    publishers/
    institutional/
    legacy/              # Temporary adapters to existing implementations
  infrastructure/
    http.py
    browser.py
    sessions.py
    storage.py
    database.py
  interfaces/
    cli.py
    api.py
    mcp.py
    web.py
```

不要求每一项一开始都有独立文件：模块长到需要分开再拆。`scansci_pdf` 在迁移期间保留为旧实现，`academic_source` 新入口不再依赖它的 CLI/MCP/Web 层。

## 5. 领域契约：论文、获取任务与文件分开

### PaperRef

标识一篇论文，包含输入标识、规范化 DOI/arXiv/PMID 等已知标识及可选元数据。允许标识或元数据不完整，不以“必须先拿到 DOI”阻断所有来源。

### AcquisitionRequest

包含论文引用、需要的产物类型（初期默认 PDF）、来源策略、必要的获取选项。远端请求不包含客户端 `output_dir`。

- 请求的成功条件由需要的产物决定，不由某一个下载函数或全文长度阈值决定。
- 区分必需产物与尽力获取的附加产物；PDF 成功但可选附件失败不能变成整篇失败。
- 不让接口暴露十几个互相覆盖的布尔开关。将允许来源和访问方式归入类型化 policy，保留少量有明确含义的预设。

### Artifact

表示一个实际存在的文件：`id`、`kind`、`media_type`、`filename`、`size`、关联论文与来源记录。服务器存储位置是内部字段；HTTP 的下载路径由入口层生成，不写死到数据库。

来源信息至少记录来源名称、来源 URL（如有）、获取时间、取得方式。衍生产物额外记录 `derived_from`，使原生文档与转换结果不会混淆。

Artifact 不等同于 PDF。初期覆盖 PDF、现有 Markdown、附件、引用导出和批量报告；未来新增结构化文档不需要修改传输机制。具体 JATS 源和转换方案不在本文设计范围。

### AcquisitionResult / Attempt

结果包含满足情况、论文元数据、产物列表、尝试摘要和必要的下一步提示。失败原因使用少量稳定分类，如 not_found、auth_required、rate_limited、network_error、invalid_document、unsupported；详细站点错误保留在 attempt 中。

“需要登录”不能编码成“让远端用户运行服务器本地命令”的字符串。返回结构化原因和机构/来源信息，CLI 与 Web 各自决定展示方式。

## 6. 保留抓取能力，但不保留所有旧编排

统一来源边界的概念形状：

```python
class Source(Protocol):
    def supports(self, paper: PaperRef, request: AcquisitionRequest) -> bool: ...
    def acquire(self, paper: PaperRef, request: AcquisitionRequest,
                context: SourceContext) -> SourceOutcome: ...
```

- `supports` 是廉价的本地判断，不把网络探测藏在注册和匹配阶段。
- `SourceContext` 提供本次配置快照、HTTP/浏览器/机构会话访问与独立工作目录，不是任意服务的查找器。
- 适配器可以落盘到工作目录，返回临时文件与来源信息；不能决定公共文件 URL、用户导出目录或整套任务状态。
- 正式文件校验和登记由公共层完成，站点特有校验仍留在适配器内部。
- 元数据查询与全文文件获取并非一种能力：不要强迫搜索提供者实现下载接口。
- 浏览器和 WebVPN 首先是访问能力；某个站点适配器可以使用它们。过渡期允许旧机构级联暂时作为粗粒度来源，最终不让两个完整编排器互相递归调用。

### 抓取边界补充复核

以下位置已由主代理抽查，不仅是目录命名推断；可调用不代表迁移后行为已经通过运行验证。

| 能力 | 保留单位 | 适配要求 |
|---|---|---|
| Unpaywall | `sources/unpaywall.py:24-116` 的候选提取与 `try_unpaywall` | 候选提取可直接复用；获取函数保留原网络/下载逻辑，传入 config 和工作路径，归一化 None、失败字典、成功字典。 |
| 出版商规则 | `sources/publishers.py:618-694` 的来源映射及对应旧函数 | 映射数据可迁移，注册表不是已完成的可执行插件体系；`publisher_strategies/base.py:90-108` 的 download 默认尚未实现。 |
| 出版商浏览器 | `_publisher_strategies_core` + `browser_engine` + backend/cookies | 作为组件簇保留，不把单个 Nature 等包装函数误认为无状态适配器。`_publisher_strategies_core.py:2029-2073` 内部自带可视浏览器/登录回退。 |
| WebVPN | `sources/instsci.py:1003-1038` 的渠道及 URL/会话依赖 | 自带校园代理→浏览器→WebVPN HTTP 小级联。保留渠道内部机制，但不能和完整机构编排桥不加区分地重复执行。 |

浏览器过渡有三个实际限制：

1. `_publisher_strategies_core.py:31-54` 用模块级状态传失败原因。薄适配器不能仅调用后随手读取、就假定并行时归属正确；过渡阶段将这类旧浏览器调用串行化，后续改成调用返回值携带诊断。
2. `browser_engine.py:395-408` 的标签操作按同一线程使用。同步浏览器的创建、使用、关闭留在其所属工作线程，不能把每次页面操作分别扔进任意线程池线程。
3. 旧函数会主动打开可视浏览器。远端适配必须在实际回退分支中落实“禁止交互”的执行策略；若旧 config 无法可靠关闭该行为，需要针对入口做小幅改造，不能仅靠新层传一个无人读取的配置键。

这些是复用边界的必要修正，不是重写出版商抓取规则。全局状态逐步收敛到浏览器组件实例与调用结果；不为此建立复杂隔离平台。

### 一个调度策略，单篇与批量共享

保留现有有价值的选源经验和受限竞速，不把所有站点一律串行化，也不照搬多层嵌套竞速。

单篇获取由统一引擎执行；批量负责排队、汇总和恢复，复用同一成功判定。现有批量元数据查询、来源预分流可作为批量优化保留，但不能成为另一套成功契约。

网络调用先保留成熟的同步实现，通过工作线程接入 ASGI；没有必要把每个 requests 抓取器重写为 async。HTTP 并发和浏览器会话并发分别受限，避免一次批量打开大量浏览器。适配器自带的重试在迁移期间显式记录，避免外层再次盲目重试。

## 7. 文件与状态：SQLite + 文件目录

```text
data/
  catalog.sqlite
  uploads/<id>/<original-name>
  artifacts/<id>/<filename>
  work/<job-id>/<attempt-id>/
  sessions/
```

- SQLite 管理论文/产物关联、上传记录、获取任务和逐项结果；文件内容仍在文件系统。
- 文件登记基于已完成且通过校验的文件，不通过扫描用户输出目录猜测缓存命中。
- 检索已存产物时考虑标识、产物类型、来源及当前 policy；例如限制来源的请求不能无条件复用不符合策略的旧文件。
- 抓取器原有站点专用缓存、域名评分和会话数据可先保留，不把所有缓存强行合并进同一张表。
- 上传原扩展名必须保留，现有解析器依赖它区分 BibTeX、CSV、XLSX。
- CLI 的 `--output` 是将已取得产物导出/复制到用户指定位置，不改变内部存储布局。远端 CLI 则通过 HTTP 下载后在客户端保存。
- Cookie/浏览器会话不是可公开取回的普通产物，不注册到通用 artifact 下载接口。

## 8. 一个服务、两种协议、一套业务

建议统一 HTTP 应用提供：

```text
/mcp                              MCP transport
/api/v1/search                    Search
/api/v1/uploads                   Upload an input file
/api/v1/lists/parse                Parse upload_id or inline input
/api/v1/acquisitions               Submit acquisition/batch
/api/v1/jobs/{id}                  Inspect status and results
/api/v1/artifacts/{id}/content     Retrieve exact artifact bytes
/                                 Optional web UI
```

接口是提案，不是已经存在的路由。FastAPI 和 MCP 都调用相同服务方法与类型化结果，不互相用 HTTP 调用，也不由 REST 直接调用 MCP 工具函数。

MCP 工具建议收敛为 search、resolve、parse_list、acquire、job_status 等用户动作；管理/诊断工具单独分组，逐项决定保留，不把旧 18 个工具当作必须永远保持的领域模型。旧工具名可以在兼容层委托到新服务。

### 长请求不要依赖 HTTP 连接一直存在

浏览器下载和批量已经是现实中的长操作，建议使用轻量任务记录：

- 提交获取返回 job_id；HTTP 可以立即返回 202。
- MCP 可以短暂等待，完成就返回结果，否则返回相同 job_id；不能因为客户端超时而重复启动整套获取。
- 第一版只需同进程工作队列/线程与 SQLite 状态，不要 Celery、Redis 或独立 worker 服务。
- 重启时将未完成任务明确标为 interrupted；批量可基于已保存逐项结果重新提交未完成项。不承诺恢复浏览器执行现场。
- 运行期取消若要支持，必须区分“停止继续调度”和“已中止执行”；初期不必承诺强杀所有浏览器/线程。

建议状态为 queued/running/succeeded/partial/failed/interrupted；批量 partial 表示只满足了部分条目。按这些真实用户需要设计，不建立通用工作流状态机。

### 远端使用体验

文献列表通过 HTTP 上传并得到 upload_id，MCP/REST 都使用它；直接提供标识列表或文本时不要求先上传。获取结果中给出 artifact ID 与相对取回路径，客户端依据已配置的服务器基址下载。

仅能调用 MCP、不能发送普通 HTTP 的客户端，不能被宣称自动支持本地文件上传。文本列表可提供内联输入；二进制表格需要配套 CLI/HTTP 上传能力。MCP resource 是否增加取决于实际客户端支持，不为协议齐全而预先实现。

stdio 保持可用，但由 CLI 适配层读取本地文件并导入服务；服务器绝对路径不是跨协议的公共标识。

## 9. 配置、登录和部署

将配置明确分为运行环境（目录、监听、网络、并发）、来源设置（密钥/机构/站点）、请求策略三部分。应用启动时构造依赖，本次任务使用配置快照；不让任意源函数随时读取和改写全局配置。

本地模式可以明确请求打开浏览器登录；远端模式默认返回 auth_required，不自动假定存在用户可操作的服务器桌面。先支持明确的管理员会话配置/受控 cookie 导入，是否提供远程浏览器界面以后单独决定。

初期面向单用户或可信网络。保留基本文件 ID 边界、上传大小限制，必要时使用已有反向代理认证；不建设多租户权限平台。这里不是给任意公开互联网暴露机构凭据管理接口的设计。

容器显式绑定可达地址，持久化 data 目录。HTTP 应用的生命周期统一管理 MCP session manager、执行器和浏览器。SDK 的挂载路径和启动/关闭行为必须先在锁定版本验证。

## 10. 分阶段迁移：目标可以重设计，切换仍可验证

### A. 固定能力清单与新契约

列出现有来源、出版商特殊处理、浏览器后端、机构通道、输入格式及批量优化；标记依赖和迁移去向。先确认 PaperRef、Artifact、AcquisitionRequest/Result 及远端文件语义。

不要仅以旧测试全部通过作为保留能力的证据；旧测试可能只锁定了旧接口形状。

### B. 建立新应用骨架与端到端通路

实现组合应用、存储、上传、任务与文件取回。先接一个简单来源，再把旧引擎临时包装为 legacy 来源，以保持可用性。旧引擎只当过渡黑盒，不成为新核心的永久依赖；其本地文件结果必须导入新存储。

HTTP/stdio/CLI 都经新应用服务。新增功能只加在新架构，避免同时维护两套业务实现。

### C. 按来源族迁移核心能力

逐步从旧编排器中抽出 OA/出版商 HTTP、浏览器、机构通道。先保留请求头、URL 规则、cookie 处理和站点特例，改变依赖传入和结果输出方式。来源族迁移后退出旧总调度，避免同一次任务重复调用。

同一阶段统一单篇与批量的结果和缓存语义，保留能证明有用的批量查询优化。

### D. 切换公开入口并清理

删除不再使用的双重 MCP、旧 Web 业务分支、重复级联与旧缓存入口。兼容旧命令/响应只保留用户确实使用的部分，不为未知外部用户承诺无限兼容。

最后完成包名、CLI 名、配置目录和容器命名迁移，提供一次性旧配置/已有文件导入，而不是运行时永久读取两套状态。

遵守原 Apache-2.0 许可及归属要求；闭源 Cython 源码不在本仓库，方案不依赖它。Python 后备及其依赖需要实际验证，不把许可证中的“完整可用”当作测试结论。

## 11. 有价值的验收

- 相同输入经 API、MCP、CLI 调用得到相同业务结果，格式差异仅发生在边界层。
- 客户端和服务器没有共享目录时，上传列表、批量获取、下载产物闭环成立。
- 单篇与批量对成功、失败、来源限制和产物类型的判断一致。
- 有代表性的来源适配器保留站点行为；URL/HTML fixture 与模拟网络覆盖稳定逻辑，必要的真实站点 smoke test 手动、按需运行。
- 任务中断有可解释状态，已经完成的产物仍可取回；重启不把半成品宣布成功。
- 统一应用 MCP 生命周期可用，stdio 不回归。

不要求测试每个私有 helper，不测试框架自身，不为了覆盖率 mock 掉所有关键业务，不把不可控文献站点测试放进默认 CI。

## 12. 本次不做

JATS 源实现、模型清洗、分布式任务、对象存储、多租户、复杂权限、插件热加载、浏览器远程桌面、全量 async 重写，以及脱离实际客户端需求的协议扩展。

用户已批准应用边界与轻量任务 API，并明确不要求旧 CLI/MCP 兼容。后续重点是实际站点与机构会话验收，以及逐步缩小遗留来源组件；核心选择仍是“保留来源知识，统一业务和文件契约”。
