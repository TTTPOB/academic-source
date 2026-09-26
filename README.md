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

两种 MCP 均提供 `search`、`resolve`、`parse_list`、`acquire`、`job_status`。HTTP MCP 的产物包含 `download_url`，通过 HTTP 下载；独立 stdio 不依赖 HTTP，产物仅返回 ID 等元数据，另有 `export_artifacts(artifact_ids, output_dir)` 将已登记文件复制到 stdio **进程所在机器**的指定目录，返回文件实际路径。若进程在容器中，目录须由容器挂载到所需位置；路径不是客户端机器的远程路径。不同 ID 即使文件同名也不会相互覆盖。

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

运行 `serve`、`mcp` 或本地获取时，第一次 Ctrl+C 会停止接收新任务并排空已接受的队列；再次 Ctrl+C 立即强退，未完成任务在下次启动时标为 `interrupted`。SIGTERM 也请求优雅退出，但不计入 Ctrl+C 次数。远端 CLI 退出不会取消服务器上的任务。

## 服务器：Docker / GHCR

代码推送到个人仓库 `TTTPOB/academic-source` 的 `main` 后，Actions 构建并推送 `ghcr.io/tttpob/academic-source:main` 和对应的 `sha-<完整提交 SHA>`；推送与 `pyproject.toml` 版本一致的 `vX.Y.Z` tag 后，再发布同名 `:vX.Y.Z` 与 `:sha-...`。PR 和其他 fork 仅构建，不推送；没有 `latest` 标签，也不发布 PyPI。以下示例跟随 `main`，生产部署请将 `:main` 替换为构建产出的不可变 `:sha-<完整提交 SHA>`：

```bash
docker run -d --name academic-source -p 8000:8000 \
  -v academic-source-data:/data/academic-source \
  ghcr.io/tttpob/academic-source:main
```

**镜像包含 HTTP/OA、vpnsci/fast 和 Playwright CDP 客户端依赖，没有内置 Chromium、Patchright、CloakBrowser 或远程桌面。**机构/浏览器能力取决于实际配置、会话、浏览器安装与运行环境；不要把默认镜像当成完整浏览器镜像。本地构建可以运行 `docker compose up --build`，与 GHCR 发布互不影响。镜像尚未推送前，上述 GHCR 拉取命令不可用。

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
  "max_upload_bytes": 20971520,
  "source_config": {
    "email": "your-real-email@example.org",
    "scihub_enabled": false,
    "browser_backend": "patchright"
  }
}
```

Unpaywall 需要真实联系邮箱。机构凭据、代理等放在 `source_config` 中；保留旧来源实现的配置键，但不会自动读取旧 `~/.scansci-pdf/config.json`。已有机构配置请明确迁入新设置，路径指向服务器上的会话文件。

来源实验、可选浏览器依赖与外部 Chrome (CDP) 部署见 [专题文档](docs/SOURCES_AND_CDP.md)。

SQLite 保存上传、任务和产物记录，文件保存在同一数据目录。一个数据目录由一个运行实例使用；服务已运行时，本地 CLI 使用 `--server` 连接它。重启将未完成任务标记为 interrupted，已完成产物仍可取回，不承诺恢复浏览器执行现场。旧配置中的 `job_workers: 1` 可读取但已不再需要，其他值报错。

`academic-source prune` 默认只预览；停服务后执行 `academic-source prune --apply` 才删除超过 30 天且无任务引用的上传、无效缓存、无任务或有效缓存引用的产物及空工作目录。现有任务及有效缓存引用始终保留；服务占用数据目录时拒绝本地清理。

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

- 当前架构：[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 原项目说明：[上游 README](https://github.com/Rimagination/scansci-pdf#readme)（历史参考，不是本项目操作手册）
- 许可证：Apache-2.0，保留上游版权与归属；未包含的闭源 Cython 源码不是新架构的依赖。

`src/academic_source` 是新应用，`src/scansci_pdf` 保留仍被调用的来源及机构实现；原插件和 PyPI 发布入口已移除。
