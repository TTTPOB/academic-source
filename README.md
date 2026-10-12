# academic-source

面向本地与远端 Agent 的学术资源获取服务。从 [Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf) fork，保留出版商、浏览器和机构渠道的抓取实现，重新组织应用服务与文件接口。

**这是独立新项目，不兼容原项目的命令、MCP 工具名或 HTTP 响应；尚未发布 PyPI，也不会发布到原作者的 PyPI/插件命名空间。**

## 普通用户：安装并启动

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。从本 fork 的 GitHub `main` 安装独立 CLI 工具（无需 clone，也不运行 `uv sync`）：

```bash
uv tool install 'academic-source[fast,vpnsci] @ git+https://github.com/TTTPOB/academic-source.git@main'
# Explicitly disable HTTP auth for a trusted local browser session.
ACADEMIC_SOURCE_AUTH_MODE=off academic-source serve --host 127.0.0.1 --port 8000
```

**只有对应代码合并并推送到 GitHub 的 `main` 后，上述远端安装才会包含本次修改。**需要固定版本可把 `@main` 改成已推送的 `@v0.1.0` 等版本 tag；不要把未推送的 tag 当成现成发行版。已有本地 wheel 时也可以离线/从文件安装（路径和版本以实际构建产物为准）：

```bash
uv tool install ./dist/academic_source-0.1.0-py3-none-any.whl
academic-source --help
```

同一进程、同一端口提供：

- Web 页面：`http://127.0.0.1:8000/`
- REST 文档：`http://127.0.0.1:8000/docs`
- Streamable HTTP MCP：`http://127.0.0.1:8000/mcp`

HTTP 默认要求静态 Bearer 认证；上例仅为可信本地浏览器使用显式关闭。远端机器使用 `--host 0.0.0.0`，配置下节密钥并通过 HTTPS 反向代理访问。不要把带机构会话的服务匿名公开。

stdio MCP：

```bash
academic-source mcp
```

两种 MCP 均提供 `search`、`get_metadata`、`parse_list`、`acquire`、`job_status`。HTTP MCP 的产物包含绝对 `download_url`，可不带长期 Bearer 密钥下载一次；独立 stdio 不依赖 HTTP，产物仅返回 ID 等元数据，另有 `export_artifacts(artifact_ids, output_dir)` 将已登记文件复制到 stdio **进程所在机器**的指定目录，返回文件实际路径。若进程在容器中，目录须由容器挂载到所需位置；路径不是客户端机器的远程路径。不同 ID 即使文件同名也不会相互覆盖。

## HTTP 认证与可分享部署

HTTP 默认 `auth_mode="required"`。服务端必须至少配置一把静态密钥；缺失或显式空数组会在创建业务服务和 worker 前启动失败。多把密钥权限相同，可以分别分发与轮换；没有用户、角色、scope 或 OAuth 自动发现。MCP 客户端需支持预配置 `Authorization` header。

```bash
# Placeholders only: replace each value with a distinct high-entropy random key.
export ACADEMIC_SOURCE_API_KEYS='["REPLACE_WITH_RANDOM_KEY_1","REPLACE_WITH_RANDOM_KEY_2"]'
academic-source serve --host 0.0.0.0 --port 8000

# Client environment: one of the server keys. Do not pass secrets as CLI arguments.
export ACADEMIC_SOURCE_API_KEY='REPLACE_WITH_RANDOM_KEY_1'
curl -H @- \
  'https://YOUR_SERVER/api/v1/search?query=example' \
  <<< "Authorization: Bearer $ACADEMIC_SOURCE_API_KEY"
```

示例中的 Bash `<<<` 把 header 经 stdin 交给 curl，避免将密钥展开到进程参数。

服务端 `ACADEMIC_SOURCE_API_KEYS` 是 JSON 字符串数组，每项为非空、无空白的 ASCII 字符串；建议各自使用至少 32 随机字节的 URL-safe 编码。客户端 `ACADEMIC_SOURCE_API_KEY` 是其中一把密钥，不是 JSON。密钥不要放入 URL、命令行参数、普通配置或版本库；使用部署平台的 secret 环境变量，或由 systemd 的 root-only `EnvironmentFile` 注入。Docker 可用权限 `0600`、不入库的 env 文件配合 `--env-file`。服务启动日志只记录认证模式。

所有 API、上传、任务、永久 artifact 下载、全部 MCP 方法、Web、docs/openapi 默认受保护。仅有两项匿名例外：

- 精确的 `GET /api/v1/status`，返回固定存活状态，供健康探针使用。
- `/downloads/<token>` 专用下载命名空间，由下载路由验证一次性 capability；HEAD/POST 等不会消耗 token。它不是永久 artifact 端点的匿名旁路。

无密钥或密钥错误统一返回 `401` 和 `WWW-Authenticate: Bearer`。只接受 `Authorization: Bearer ...`，不会因出现 Cloudflare header 放行。若同时使用 Cloudflare Access，控制面客户端还需提供其凭据；应用不会将 Bearer 密钥转发给学术上游。公网必须使用 HTTPS，并限制 origin 的直接访问。

浏览器地址栏、当前 Web UI 与 Swagger UI 不会自动附带 Bearer；认证模式下第一版面向可配置 header 的 HTTP/MCP 客户端，不提供登录页、cookie 会话或 OAuth。可信本地或完全由认证反代隔离的部署可以显式设置 `ACADEMIC_SOURCE_AUTH_MODE=off`；不要依赖监听 loopback 自动关闭认证。stdio MCP、本地 `get/batch/prune` 不经过 HTTP，无需服务密钥。

添加新密钥后重启服务，迁移客户端，再移除旧密钥并重启即可轮换。密钥变更只在启动时读取；已签发下载 URL 有独立生命周期，重启会使其失效。

## 检索与元数据

HTTP `GET /api/v1/search?query=...&limit=10` 与 MCP `search(query, limit)` 只从 OpenAlex 检索，默认保留其相关性顺序。`limit` 为 1–100；只取一页有界候选（最多 200 条），当前只返回带 DOI 的候选，分类过滤后再截断，因此结果可能少于请求数量。默认排除明确的审稿报告、组件/补充材料、词条/数据对象、书籍、paratext、勘误/撤稿通知、会议摘要及已标记撤稿的原文；保留研究文章、综述、预印本、完整会议论文与未知类型。另有少量完整泛标题规则（如 `Foreword`、`Manuscript Submission Guideline`），不会因标题含 Review 或 Guideline 就排除。

结果保留已有书目信息，以及可用的 OpenAlex `type`、`is_retracted` 和 ISO `publication_date`；缺失字段省略，false/0 仍保留。不承诺上游类型准确、覆盖所有编辑材料或消除短词/基因名称歧义。真实无命中返回 `[]`；OpenAlex 网络、HTTP 或 JSON 错误通过 HTTP 502 或 MCP 工具错误返回，不伪装无命中。

HTTP `POST /api/v1/metadata` 与 MCP `get_metadata(id_type="doi", doi="...")` / `get_metadata(id_type="arxiv", arxiv_id="...")` 按必填的 `id_type` 选择元数据来源：`doi` 查询 **Crossref**，`arxiv` 查询 **arXiv 原生 API**。请求示例：

```json
{"id_type":"doi","doi":"10.1234/example"}
```

```json
{"id_type":"arxiv","arxiv_id":"1706.03762v1"}
```

请求只提供与 `id_type` 对应的 `doi` 或 `arxiv_id`；缺失、错配或同时提供两种 ID 会被拒绝。所有响应均返回 `id_type`、`metadata_source`（`crossref/arxiv`）、`metadata_status`，以及规范化的请求 `doi` 或 `arxiv_id`。DOI 接受裸 DOI、`doi:` 前缀和 `doi.org` URL；arXiv 接受新旧 ID、`arxiv:` 前缀、`arxiv.org/abs/` 或 `pdf/` URL，以及 `10.48550/arXiv.` DOI 形式。来源由声明类型决定：同一个 arXiv DOI 在 `id_type="doi"` 时仍查 Crossref。

- `metadata_status=found`：找到匹配记录，返回可用的 `title/authors/year`。Crossref 另保留 `type/relations/review_type/updates`；arXiv 另保留 `abstract/published/updated/version`。缺失字段省略，找到记录不保证字段齐全或覆盖所有 DOI 注册机构。arXiv feed 的真实发表 DOI 单列为 `published_doi`，不会替换预印本的请求身份。
- `metadata_status=not_found`：Crossref 返回 404，或 arXiv 返回合法空 Atom feed。`metadata_status=unavailable`：网络、HTTP、JSON/XML、错误 entry 或记录不匹配等失败，附简短 `metadata_error`。这些状态仍保留请求身份；意外内部错误直接传播。

arXiv 的响应 `arxiv_id` 不带 `arxiv:` 前缀，并保留请求版本（如 `1706.03762v1`）。[arXiv API 版本规则](https://info.arxiv.org/help/api/user-manual.html#_a_note_on_article_versions)：未指定版本查询 latest，`version` 是返回记录 ID 中的实际版本（若缺失则省略）；指定 `vN` 必须得到该版本；返回记录缺失版本或版本不匹配时为 `unavailable`，不会改查 latest。`published` 是首版提交日期，`updated` 是返回版本提交日期。

标题、PMID、PMCID、ISBN 及与声明类型不符的输入返回 HTTP 400 / MCP 工具错误；缺失或错误 `id_type` 由请求 schema 拒绝。

标题使用 `search` 查 OpenAlex 候选；`get_metadata` 不做标题推断。获取请求中的标题继续通过 OpenAlex 相似度匹配（0.75 阈值，拒绝不同 DOI 同分最佳匹配），不依赖 Crossref 元数据查询成功。直接 DOI 或 arXiv 获取也无需先调用 `get_metadata`；内部 DOI→PMC 标识转换和 PDF 候选发现保持原链路。

元数据查询不参与关键词召回，不逐条补全搜索结果，也不重排结果。需要某个 DOI 或 arXiv 记录的补充元数据时，单独调用 `get_metadata`。

## 本地与远端 CLI

```bash
# Local acquisition and export.
academic-source get '10.1234/example' --output ./papers
academic-source batch ./references.bib --output ./papers

# Upload a client-local list and save returned files on the client.
# ACADEMIC_SOURCE_API_KEY must contain one server key when HTTP auth is required.
academic-source batch ./references.csv \
  --server https://YOUR_SERVER --output ./papers
academic-source get '10.1234/example' \
  --server https://YOUR_SERVER --output ./papers --policy legal_only
```

这些标识符仅为命令示例，不是可下载论文。

运行 `serve`、`mcp` 或本地获取时，第一次 Ctrl+C 会停止接收新任务并排空已接受的队列；再次 Ctrl+C 立即强退，未完成任务在下次启动时标为 `interrupted`。SIGTERM 也请求优雅退出，但不计入 Ctrl+C 次数。远端 CLI 退出不会取消服务器上的任务。

## 服务器：Docker / GHCR

代码推送到个人仓库 `TTTPOB/academic-source` 的 `main` 后，Actions 构建并推送 `ghcr.io/tttpob/academic-source:main` 和对应的 `sha-<完整提交 SHA>`；推送与 `pyproject.toml` 版本一致的 `vX.Y.Z` tag 后，再发布同名 `:vX.Y.Z` 与 `:sha-...`。PR 和其他 fork 仅构建，不推送；没有 `latest` 标签，也不发布 PyPI。以下示例跟随 `main`，生产部署请将 `:main` 替换为构建产出的不可变 `:sha-<完整提交 SHA>`：

```bash
docker run -d --name academic-source -p 8000:8000 \
  --env-file ./academic-source.env \
  -v academic-source-data:/data/academic-source \
  ghcr.io/tttpob/academic-source:main
```

`--env-file` 指定的文件需由部署者创建并保持私有，内容例如（仅占位值）：

```dotenv
ACADEMIC_SOURCE_API_KEYS=["REPLACE_WITH_RANDOM_KEY_1","REPLACE_WITH_RANDOM_KEY_2"]
```

**镜像包含 HTTP/OA、vpnsci/fast 和 Playwright CDP 客户端依赖，没有内置 Chromium、Patchright、CloakBrowser 或远程桌面。**机构/浏览器能力取决于实际配置、会话、浏览器安装与运行环境；不要把默认镜像当成完整浏览器镜像。本地构建可以运行 `docker compose up --build`，与 GHCR 发布互不影响。镜像尚未推送前，上述 GHCR 拉取命令不可用。

## HTTP 文件闭环

```bash
curl -H @- \
  -F 'file=@references.bib' https://YOUR_SERVER/api/v1/uploads \
  <<< "Authorization: Bearer $ACADEMIC_SOURCE_API_KEY"
# Use the returned id as upload_id.
curl -H @- \
  -H 'Content-Type: application/json' \
  -d '{"upload_id":"UPLOAD_ID","policy":"legal_only"}' \
  https://YOUR_SERVER/api/v1/acquisitions \
  <<< "Authorization: Bearer $ACADEMIC_SOURCE_API_KEY"
curl -H @- \
  https://YOUR_SERVER/api/v1/jobs/JOB_ID \
  <<< "Authorization: Bearer $ACADEMIC_SOURCE_API_KEY"
# Follow artifacts[].download_url to retrieve exact file bytes.
curl -H @- \
  -o paper.pdf https://YOUR_SERVER/api/v1/artifacts/ARTIFACT_ID/content \
  <<< "Authorization: Bearer $ACADEMIC_SOURCE_API_KEY"
```

HTTP API 和远端 CLI 使用受 Bearer 保护的永久 artifact 路径；上述下载仍需密钥。HTTP MCP 的 `acquire/job_status` 另返回一次性绝对 URL，独立下载工具直接 GET 该 URL，无需长期密钥：

```bash
curl -o paper.pdf 'DOWNLOAD_URL_FROM_MCP'
```

MCP 下载 URL 默认从当前请求的 scheme、host 与部署前缀生成，客户端不必拼接 host。需要固定外部地址时，可选设置 `ACADEMIC_SOURCE_PUBLIC_BASE_URL`（包含部署前缀）。反代应正确传递请求信息，Uvicorn 只应信任明确的代理来源；应用不会自行信任任意转发头。

一次性链接只支持整文件 GET，领取后即消耗，断线也需重新查询 job 领取新链接；Range、HEAD、POST 不消费。链接在进程重启或到期后失效，不支持断点续传。URL 本身是该文件的短期读取凭据，不要公开或写入常规日志；使用单服务进程，不在多个 worker 之间共享。Cloudflare 等外层代理需要只对下载命名空间放行并禁用缓存，控制面继续认证。

也可直接提交 `{"identifiers":["10.1234/example"]}` 或内联文献列表 `{"text":"..."}`。三种输入只能选择一种。只支持 MCP 调用、不能发普通 HTTP 的客户端，可以使用内联文本；上传 XLSX 等二进制文件需 CLI 或 HTTP 客户端。

可选请求字段：`markdown`、`supplementary`、`bibtex`。PDF 获取成功与附加转换是否成功分开报告。原生 JATS 与模型清洗尚未实现。

MCP、HTTP 和 CLI 的任务结果使用同一精简投影：成功单篇保留产物、命中的 `artifacts[].provenance.source/url`、非空元数据和警告；不返回逐来源未命中或重复尝试的 `attempts`。失败单篇不会消失，即使任务为 `partial`，仍保留其 `identifier`、`status` 和一个汇总 `reason`；只有配置、鉴权、带明确 `action` 的阻塞及需检查服务日志的内部错误保留非空 `message/action`。转换/导出警告和任务级 `error` 不受影响。空的单篇 `artifacts/metadata/warnings/message/action` 字段省略；顶层 `artifacts` 始终为列表；HTTP API 使用受保护的永久地址，HTTP MCP 使用一次性绝对 URL，stdio 使用本地导出。

这是响应契约的精简：客户端不要依赖响应中的 `attempts` 或空字段固定存在，应使用产物来源和单篇汇总状态。完整尝试仍存于原有内部任务记录，持久化结构不变；精简响应不能用来覆盖该记录。

## 配置与存储

默认数据目录 `~/.academic-source`，可用 `ACADEMIC_SOURCE_DATA_DIR` 指定。服务读取该目录的 `settings.json`，例如：

```json
{
  "interactive": false,
  "max_upload_bytes": 20971520,
  "source_config": {
    "email": "your-real-email@example.org",
    "scihub_enabled": false,
    "browser_backend": "patchright"
  }
}
```

`auth_mode`（默认 `required`）和 `public_base_url`（默认空、使用当前请求地址）可写入普通配置。`Settings.load()` 中 `ACADEMIC_SOURCE_AUTH_MODE`、`ACADEMIC_SOURCE_PUBLIC_BASE_URL`、`ACADEMIC_SOURCE_API_KEYS` 优先于文件；显式空 keys 不会回退到文件密钥。程序式 `Settings(...)` 使用传入值，不被这三个环境变量隐式覆盖。服务密钥使用环境变量注入，避免明文写入普通配置。

Unpaywall 需要真实联系邮箱。机构凭据、代理等放在 `source_config` 中；保留旧来源实现的配置键，但不会自动读取旧 `~/.scansci-pdf/config.json`。已有机构配置请明确迁入新设置，路径指向服务器上的会话文件。

来源实验、可选浏览器依赖与外部 Chrome (CDP) 部署见 [专题文档](<docs/SOURCES_AND_CDP.md>)。

SQLite 保存上传、任务和产物记录，文件保存在同一数据目录。一个数据目录由一个运行实例使用；服务已运行时，本地 CLI 使用 `--server` 连接它。重启将未完成任务标记为 interrupted，已完成产物仍可取回，不承诺恢复浏览器执行现场。旧配置中的 `job_workers: 1` 可读取但已不再需要，其他值报错。

`academic-source prune` 默认只预览；停服务后执行 `academic-source prune --apply` 才删除超过 30 天且无任务引用的上传、无效缓存、无任务或有效缓存引用的产物及空工作目录。现有任务及有效缓存引用始终保留；服务占用数据目录时拒绝本地清理。

## 开发者：源码与检查

```bash
git clone https://github.com/TTTPOB/academic-source.git
cd academic-source
uv sync --frozen --extra vpnsci --extra fast
ACADEMIC_SOURCE_AUTH_MODE=off uv run --frozen academic-source serve --host 127.0.0.1 --port 8000
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run ruff check src/academic_source tests/academic_source tests/providers
uv run ruff format --check src/academic_source tests/academic_source tests/providers
uv run pyrefly check
uv run pytest -q
```

Hook 参考 jcli：Ruff lint/format 与 Pyrefly basic。新代码执行这些检查，上游抓取模块不做全库格式化。默认测试不要求真实文献网络或浏览器安装。测试优先覆盖文件闭环、来源选择、批量结果、错误与任务生命周期，而非重复 mock 或框架行为。

## 架构与上游

- 当前架构：[docs/ARCHITECTURE.md](<docs/ARCHITECTURE.md>)
- 原项目说明：[上游 README](https://github.com/Rimagination/scansci-pdf#readme)（历史参考，不是本项目操作手册）
- 许可证：Apache-2.0，保留上游版权与归属；未包含的闭源 Cython 源码不是新架构的依赖。

`src/academic_source` 是新应用，`src/scansci_pdf` 保留仍被调用的来源及机构实现；原插件和 PyPI 发布入口已移除。
