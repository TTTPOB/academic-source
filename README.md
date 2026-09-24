# academic-source

面向本地与远端 Agent 的学术资源获取服务。从 [Rimagination/scansci-pdf](https://github.com/Rimagination/scansci-pdf) fork，保留出版商、浏览器和机构渠道的抓取实现，重新组织应用服务与文件接口。

**这是独立新项目，不兼容原项目的命令、MCP 工具名或 HTTP 响应；尚未发布 PyPI，也不会发布到原作者的 PyPI/插件命名空间。**

## 普通用户：安装并启动

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/)。从本 fork 的 GitHub `main` 安装独立 CLI 工具（无需 clone，也不运行 `uv sync`）：

```bash
uv tool install 'academic-source[fast,vpnsci] @ git+https://github.com/TTTPOB/academic-source.git@main'
academic-source serve --host 127.0.0.1 --port 8000
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

远端机器上显式使用 `--host 0.0.0.0`；此阶段面向单用户/可信网络，公网部署应通过已有反向代理认证。不要把带机构会话的服务裸露在公网。

stdio MCP：

```bash
academic-source mcp
```

MCP 工具为 `search`、`resolve`、`parse_list`、`acquire`、`job_status`。获取是可查询的任务，产物提供 ID 和 HTTP 下载路径，不要求客户端能访问服务器磁盘。

## 本地与远端 CLI

```bash
# Local acquisition and export.
academic-source get '10.1234/example' --output ./papers
academic-source batch ./references.bib --output ./papers

# Upload a client-local list and save returned files on the client.
academic-source batch ./references.csv \
  --server http://SERVER:8000 --output ./papers
academic-source get '10.1234/example' \
  --server http://SERVER:8000 --output ./papers --policy legal_only
```

这些标识符仅为命令示例，不是可下载论文。

## 服务器：Docker / GHCR

代码推送到个人仓库 `TTTPOB/academic-source` 的 `main` 后，Actions 构建并推送 `ghcr.io/tttpob/academic-source:main` 和对应的 `sha-<完整提交 SHA>`；推送与 `pyproject.toml` 版本一致的 `vX.Y.Z` tag 后，再发布同名 `:vX.Y.Z` 与 `:sha-...`。PR 和其他 fork 仅构建，不推送；没有 `latest` 标签，也不发布 PyPI。以下示例跟随 `main`，生产部署请将 `:main` 替换为构建产出的不可变 `:sha-<完整提交 SHA>`：

```bash
docker run -d --name academic-source -p 8000:8000 \
  -v academic-source-data:/data/academic-source \
  ghcr.io/tttpob/academic-source:main
```

**镜像只有 HTTP/OA 等轻量默认能力及可选的 vpnsci/fast Python 依赖，没有内置 Chromium、Patchright、CloakBrowser 或远程桌面。**机构/浏览器能力取决于实际配置、会话、浏览器安装与运行环境；不要把默认镜像当成完整浏览器镜像。本地构建可以运行 `docker compose up --build`，与 GHCR 发布互不影响。镜像尚未推送前，上述 GHCR 拉取命令不可用。

## HTTP 文件闭环

```bash
curl -F 'file=@references.bib' http://SERVER:8000/api/v1/uploads
# Use the returned id as upload_id.
curl -H 'Content-Type: application/json' \
  -d '{"upload_id":"UPLOAD_ID","policy":"legal_only"}' \
  http://SERVER:8000/api/v1/acquisitions
curl http://SERVER:8000/api/v1/jobs/JOB_ID
# Follow artifacts[].download_url to retrieve exact file bytes.
curl -o paper.pdf http://SERVER:8000/api/v1/artifacts/ARTIFACT_ID/content
```

也可直接提交 `{"identifiers":["10.1234/example"]}` 或内联文献列表 `{"text":"..."}`。三种输入只能选择一种。只支持 MCP 调用、不能发普通 HTTP 的客户端，可以使用内联文本；上传 XLSX 等二进制文件需 CLI 或 HTTP 客户端。

可选请求字段：`markdown`、`supplementary`、`bibtex`。PDF 获取成功与附加转换是否成功分开报告。原生 JATS 与模型清洗尚未实现。

## 配置与存储

默认数据目录 `~/.academic-source`，可用 `ACADEMIC_SOURCE_DATA_DIR` 指定。服务读取该目录的 `settings.json`，例如：

```json
{
  "interactive": false,
  "job_workers": 1,
  "max_upload_bytes": 20971520,
  "source_config": {
    "email": "your-real-email@example.org",
    "scihub_enabled": false,
    "browser_backend": "patchright"
  }
}
```

Unpaywall 需要真实联系邮箱。机构凭据、代理等放在 `source_config` 中；保留旧来源实现的配置键，但不会自动读取旧 `~/.scansci-pdf/config.json`。已有机构配置请明确迁入新设置，路径指向服务器上的会话文件。

若有 Elsevier API key（配置 `source_config.elsevier_api_key` 或服务进程的 `ELSEVIER_API_KEY` 环境变量），`10.1016/` 的 Cell/Elsevier DOI 在 `fastest`、`legal_only` 策略下先尝试 ElsevierAPI；取得可读 PDF 即停止，不再探测其他来源/浏览器，失败则照常回退。能否取得非 OA 正文取决于 key 的实际订阅权益；校园网有利于机构回退渠道，但不保证 API 授权。Nature、Science DOI 和显式的其他来源策略不受此捷径影响。

浏览器渠道为可选能力。若需要 Patchright 后端，工具安装时选择额外依赖 `academic-source[fast,vpnsci,patchright]`（仍使用上述 Git URL）；还需在运行环境中安装 Chromium。开发版示例：

```bash
uv sync --frozen --extra vpnsci --extra fast --extra patchright
uv run --frozen patchright install chromium
```

默认不自动弹出可视登录窗口。无会话时应返回需要认证的结果，而不是假定远端客户端与服务器共享桌面。这个版本未提供远程桌面或网页登录管理后台。

SQLite 保存上传、任务和产物记录，文件保存在同一数据目录。一个数据目录由一个运行实例使用；服务已运行时，CLI 使用 `--server` 连接它。重启将未完成任务标记为 interrupted，已完成产物仍可取回，不承诺恢复浏览器执行现场。

## 开发者：源码与检查

```bash
git clone https://github.com/TTTPOB/academic-source.git
cd academic-source
uv sync --frozen --extra vpnsci --extra fast
uv run --frozen academic-source serve --host 127.0.0.1 --port 8000
uv run pre-commit install --hook-type pre-commit --hook-type pre-push
uv run ruff check src/academic_source tests/academic_source tests/providers
uv run ruff format --check src/academic_source tests/academic_source tests/providers
uv run pyrefly check
uv run pytest -q
```

Hook 参考 jcli：Ruff lint/format 与 Pyrefly basic。新代码执行这些检查，上游抓取模块不做全库格式化。默认测试不要求真实文献网络或浏览器安装。测试优先覆盖文件闭环、来源选择、批量结果、错误与任务生命周期，而非重复 mock 或框架行为。

## 架构与上游

- 架构设计：`docs/ARCHITECTURE_PROPOSAL.md`
- 当前模块协作契约：`docs/IMPLEMENTATION_CONTRACT.md`
- 原项目说明：[上游 README](https://github.com/Rimagination/scansci-pdf#readme)（历史参考，不是本项目操作手册）
- 许可证：Apache-2.0，保留上游版权与归属；未包含的闭源 Cython 源码不是新架构的依赖。

`src/academic_source` 是新应用，`src/scansci_pdf` 保留仍被调用的来源及机构实现；原插件和 PyPI 发布入口已移除。
