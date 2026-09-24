# academic-source 实现与复核记录

本轮落实新的应用层和远端文件闭环，不再以旧 CLI/MCP/API 兼容性为目标。原站点抓取代码作为来源实现保留，未进行全库重写。

## 已完成

| 部分 | 实现 |
|---|---|
| 公共契约 | 类型化获取请求、逐篇结果、产物、来源记录和任务状态；请求不接受客户端 output_dir |
| 应用服务 | 列表解析、发现/标识规范化、单篇与批量统一获取、可选 Markdown/SI/BibTeX 导出 |
| 获取调度 | 完整 OA/出版商来源枚举与机构通道接入；来源策略、受限 HTTP 并发、浏览器任务线程归属 |
| 存储 | SQLite 记录上传、任务、产物和结果缓存；实际文件由独立目录持久化 |
| 长任务 | 单进程任务执行、逐项结果持久化、有界等待、重启后的 interrupted 状态 |
| 远端接口 | 一个 FastAPI 应用承载 /mcp、/api/v1 与简易 Web；上传列表→任务→文件取回 |
| CLI | academic-source serve/mcp/get/batch；本地和远端模式，文件保存到客户端 |
| 非交互运行 | 默认无头；保留有效会话下载能力，真实认证分支不等待用户登录 |
| 项目维护 | 新包名、uv 锁文件、参考 jcli 的 Ruff/Pyrefly hook；纯 Python 构建，不要求缺失的 Cython 源码 |
| 发布 | 原 PyPI/插件与品牌资产已移除；GHCR 发布由独立 workflow 管理，不发布 PyPI |

新入口不会调用旧的 sources.download、PaperFetcher.fetch 或旧 MCP 工具作为总编排器。新的来源适配直接调用对应站点函数；机构 broker 和浏览器 batch 复用明确的子阶段。

## 来源保留范围

- 出版商 URL/HTML/浏览器知识继续使用现有 publisher 映射与站点函数。
- OA 包括 Unpaywall、OpenAlex、Semantic Scholar、OpenAIRE、DOAJ、Crossref 页面、EuropePMC、PMC、CORE，以及有配置时的 OpenAlex Content API。
- 保留 Sci-Hub/LibGen 路由，严格遵守来源策略和 scihub_enabled。SciBban 仍只是上游已有的空实现，不把它算成新增可用能力。
- 保留 CARSI、WebVPN、EZProxy、机构会话 broker、机构浏览器 batch 与 SAGE CN 的对应路径。能否实际获取仍取决于安装的浏览器、有效会话、机构网络和订阅。
- arXiv 显式版本会保留到实际下载 URL，而不只保留在展示字段里。

## 主代理审查后修正的问题

1. 补回子代理最初遗漏的来源/机构通道，修复忽略 scihub_enabled 和 oa_first 无灰色回退的问题。
2. 将“非交互”从禁用整个通道改为保留已登录无头会话，只限制人工认证分支；向机构浏览器显式传入本次配置。
3. PDF 必须可读取且有页面，不能凭文件头宣布成功；不以正文长度判断 PDF 下载是否成功。
4. 来源竞速不再提前排入全部候选；找到结果后不启动更多来源，已开始的请求结束后再回收工作目录。
5. 原 HTTP 测试只连接 FakeApplication/FakeStore；改为真实 Application、SQLite、文件存储，加唯一的来源 I/O 替身。远端 CLI 测试使用真实 loopback HTTP。
6. 成功 PDF 单独缓存；可选转换失败可重试，不重复下载 PDF，也不永久缓存失败的转换结果。
7. 拆开列表、发现、导出与获取任务逻辑；抽出共享传输序列化，MCP 不依赖 REST 路由实现。
8. 清除旧包导入时修改进程代理/TLS 的副作用；修复旧浏览器可用性检查的 UnboundLocalError。
9. 修复基线测试对 Windows 平台、可选浏览器包、外部 CLI 和并发完成顺序的错误假设。
10. 通过 SDK lifespan 将第三方来源/转换器的 stdout 输出转到 stderr，防止污染 MCP stdio；用真实子进程完成获取来验证，不仅检查工具列表。
11. 修复 EZProxy 把带 libproxy 的正常论文域名误当登录页面的问题，并让实际认证阻碍返回 auth_required。

## 测试的“思想消融”审查

下表是测试价值判断，不声称运行过全自动 mutation testing。另外实际做了两次受控破坏探针：临时移除出版商/CARSI 的非交互 guard，两项对应测试均失败，随后立即恢复生产代码。审查还修正了“在 mock 中抛 AssertionError，却被被测代码捕获”的虚通过风险。

| 测试组 | 删除后可能漏掉的真实故障 | 有意义的破坏方式 |
|---|---|---|
| 存储重开/中断/缓存 | 文件记录只在内存、重启丢状态、删除文件后返回空下载 | 移除 SQLite 写入、去掉文件存在性检查 |
| 真 HTTP 文件闭环 | 上传并未进入真实解析器，返回服务器路径而非可取回文件 | 跳过上传存储、改错 artifact ID→文件映射 |
| MCP→HTTP 共用状态 | 两个入口各自创建任务/存储，或路径变为 /mcp/mcp | 使用不同 Application、删掉 session manager 生命周期 |
| 远端 CLI | 将本机路径交给服务器、忘记在客户端保存字节 | 不上传文件、把结果路径当客户端路径 |
| 单篇/批量结果与可读 PDF | 失败字典被当成功、HTML/截断 PDF 入库 | 去掉 success=False 判断或只检查文件头 |
| 逐项进度/有界等待 | 第二篇仍执行时第一篇结果不可见，Agent 被阻塞到整个批次结束 | 移除逐项 save_job、把 wait 的 timeout 忽略 |
| 可选导出重试 | 临时转换失败永久缓存，或每次重试重新抓 PDF | 缓存含警告的不完整导出结果、删除 PDF 基础缓存 |
| 来源策略 | 无前置成功时误入禁用来源，或遗漏机构路径 | 去掉禁灰条件、删掉某个来源族 |
| 非交互与线程归属 | 自动弹窗/长时间等人登录、跨线程操作浏览器 | 去掉真实登录分支 guard、把浏览器放进 HTTP 线程池 |
| 列表与标识 | 内联 BibTeX 不识别，arXiv/标题行丢失，版本丢失 | 只按 DOI 列解析、把版本从实际请求中剥离 |

已去掉或修正那些前置来源提前成功、导致所谓“后续来源禁用”断言根本未触达目标分支的测试。源策略测试让前置来源失败后再观察完整调用路径。

## 明确的范围与限制

- 本轮不实现原生 JATS、PDF→JATS 或模型清洗。
- 没有做真实出版商成功率、机构账号或订阅权限验收；离线测试通过不代表外部网站永远可用。
- 原批量预分流/S2 批量元数据优化、动态来源评分、可选外部 instsci 总级联没有直接搬入新引擎；其代码保留，不把它们默认为新引擎已实现的功能。
- 一个数据目录使用一个运行实例。任务执行暂固定为一个主工作线程，HTTP 来源可以受限并行；不支持多进程 worker 共享这个目录。
- 关闭会等待正在执行的请求结束，取消尚未执行的任务；不承诺强杀任意第三方浏览器调用或恢复浏览器现场。
- 机构登录管理仍需明确配置/已有会话；没有新增远程桌面或凭据管理平台。
- 旧 CLI/MCP/Web 入口、插件和品牌资产已经移除；scansci_pdf 仍包含站点规则、机构抓取与列表解析相关模块。旧 sources 调度器被延迟加载，仅供未迁移的历史 pipeline 路径使用；其余无法确认无用的站点知识没有为清理而重写。
- GHCR 镜像已由 GitHub Actions 实际构建并发布；默认镜像不内置浏览器。NAS 使用独立 Python 环境与 systemd 完成实测，不将镜像构建等同于容器运行验收。

## 本轮离线验收

- 默认 `pytest -q` 只收集 `tests/academic_source` 和 `tests/providers`。初始精简后为 42 项；加入 Elsevier API-first 和默认 XML 回归后，代码提交 `d769470` 为 54 passed，Ruff/Pyrefly 通过，GitHub CI 与 GHCR 构建均成功。不把此前旧测试的 382 passed / 4 skipped 作为当前验收指标。
- Ruff lint、Ruff format、Pyrefly basic 与提交 hook 均通过；`uv lock --check --offline` 通过。
- `uv build --offline --out-dir .tmp/dist` 成功生成 `academic_source-0.1.0` 的 sdist 与 pure-Python wheel；wheel 包含两套源码包与公开的 webvpn.json，不包含编译扩展或生成的加密学校数据库。
- 只保留适用于新引擎的来源行为回归：SAGE CN 的实际文章 ID、有效机构会话与真实授权端点；OpenAIRE 嵌套 JSON/XML 全文 URL；Sci-Hub 下载 Referer；出版商 DOI 前缀路由。删除根层旧接口、固定工具表、旧批量/竞速/缓存与旧编排测试。每组保留测试直接碰到对应来源函数，删除会漏掉路由错误、会话误用或 URL 解析回退；移除目标分支应使断言失败。来源站点与机构在线验收仍未执行。
