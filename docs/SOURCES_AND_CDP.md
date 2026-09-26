# 来源实验与外部 Chrome 部署

以下为来源行为、可选浏览器依赖及 Linux CDP 参考部署；返回 [README](../README.md)。

若有 Elsevier API key（配置 `source_config.elsevier_api_key` 或服务进程的 `ELSEVIER_API_KEY` 环境变量），`10.1016/` 的 Cell/Elsevier DOI 在 `fastest`、`legal_only` 策略下先尝试 ElsevierAPI；取得可读 PDF 即停止，不再探测其他来源/浏览器，失败则照常回退。能否取得非 OA 正文取决于 key 的实际订阅权益及请求的网络路径；校园网不保证 API 授权。Nature、Science DOI 和显式的其他来源策略不受此捷径影响。

ElsevierAPI 先试直接 PDF，失败时 XML 回退使用默认表示寻找 PDF 附件；这不表示 `view=FULL` 在 API 中普遍无效：曾有请求路径返回 400，另一请求路径对默认 XML 与 `FULL` 均返回 200。XML 元数据 200 或 PDF 200 也不等于正文授权（后者可能只是单页图文摘要）；只有经 PDF 内容校验的结果才会被接受。

浏览器渠道为可选能力。若需要 Patchright 后端，工具安装时选择额外依赖 `academic-source[fast,vpnsci,patchright]`（安装 Git URL 见 [README](../README.md)）；还需在运行环境中安装 Chromium。开发版示例：

```bash
uv sync --frozen --extra vpnsci --extra fast --extra patchright
uv run --frozen patchright install chromium
```

### 使用外部常驻 Chrome（CDP）

应用只安装 Playwright **客户端**，不安装或捆绑 Chrome。`browser_backend=cdp` 延迟连接已有 Chrome 的默认持久 context，只新建/关闭自己的标签页。以下是一套**Linux 同宿主参考部署**：[`deploy/chrome.compose.yml`](../deploy/chrome.compose.yml) 托管 [LinuxServer Chrome](https://github.com/linuxserver/docker-chrome) 的有头桌面和原始 TCP 转发，不让应用自己启动浏览器。部署前须按实际环境检查端口、持久化 profile 与站点获取；此示例不代表 NAS 或生产验收。Chrome 的 profile 保存在 `deploy/chrome-data/academic-profile`，属于非默认 profile，不随应用重启丢失；`deploy/chrome-init.sh` 仅在容器启动、Chrome 尚未运行时清除该 profile 的陈旧 Singleton 链接。

```bash
# 在 Linux 服务器上，从仓库根目录执行；确保当前用户可写 deploy/chrome-data。
mkdir -p deploy/chrome-data
chmod 700 deploy/chrome-data
PUID=$(id -u) PGID=$(id -g) docker compose -f deploy/chrome.compose.yml up -d
curl -fsS http://127.0.0.1:19222/json/version
```

Chrome 有头运行于容器内桌面；GUI 的 HTTPS 端口 `3001`、CDP `19222` 都**只绑定宿主 loopback**。在个人电脑建立 `ssh -L 3001:127.0.0.1:3001 USER@HOST`，访问 `https://127.0.0.1:3001/` 并接受容器自签证书，仅**人工**完成合法机构登录/验证。LinuxServer 桌面默认没有认证；SSH 端口转发仅保护远程入口，仍应限制服务器本地用户和容器访问，绝不能开放 GUI/CDP 到公网。关闭 SSH/浏览器客户端不会关闭由 Compose 托管的 Chrome；`restart: unless-stopped` 在容器运行时崩溃或主机重启后恢复服务。更新或重建前运行 `docker compose -f deploy/chrome.compose.yml down`，确保旧容器停止；不要在 Chrome 运行时手动清除 profile 锁，也不要同时启动第二个同 profile 的 Chrome。保留 `deploy/chrome-data` 即保留登录会话（其数据敏感，妥善备份和限权）。

```bash
uv tool install 'academic-source[fast,vpnsci,cdp] @ git+https://github.com/TTTPOB/academic-source.git@main'
mkdir -p "${ACADEMIC_SOURCE_DATA_DIR:-$HOME/.academic-source}"
```

将 `ACADEMIC_SOURCE_DATA_DIR/settings.json`（默认 `~/.academic-source/settings.json`）的 `source_config` 配为：

```json
{"interactive": false, "source_config": {"browser_backend": "cdp", "browser_cdp_url": "http://127.0.0.1:19222", "scihub_enabled": false}}
```

保存设置后，先以前台方式验证（停止用 `Ctrl+C`）：

```bash
academic-source serve --host 127.0.0.1 --port 8000
```

长期运行可用以下后台容器**替代**前台进程；不要同时运行两个实例访问同一数据目录。此例使用 Linux 宿主网络，镜像已包含 CDP 客户端但不包含 Chrome：

```bash
docker run -d --name academic-source --restart unless-stopped --network host \
  --user "$(id -u):$(id -g)" \
  -e HOME=/data/academic-source \
  -e ACADEMIC_SOURCE_DATA_DIR=/data/academic-source \
  -v "${ACADEMIC_SOURCE_DATA_DIR:-$HOME/.academic-source}:/data/academic-source" \
  ghcr.io/tttpob/academic-source:main \
  academic-source serve --host 127.0.0.1 --port 8000
```

该 URL 必须从**应用进程**可达。启动时工作线程安排一次非致命 CDP 预检（`browser_cdp_timeout` 默认 5 秒）：连接并检查默认 context 后断开，不开页、不清理标签页；失败只警告，不阻断 HTTP 来源。正式浏览器获取仍按需连接，预检结果不作为永久可用状态。HTTP/OA/Elsevier API 获取不需要 CDP 工作连接，但配置 CDP 时仍可能发生上述启动预检。`browser_pdf_timeout`（默认 120 秒）和 `browser_pdf_max_bytes`（默认 100 MiB）可在 `source_config` 调整；超时/过大不登记半成品。

Science 快路径在 `cache_dir/science_http_state.json` 成对保存 UA 与 cookie 快照；旧全局 UA 缓存不迁移，下一次浏览器成功后重新取得。快照只表示配对，不保证站点接受。`science_http_proxy` 设置非空值时优先使用该代理；未设置或为 `null` 时继承代理：本地浏览器优先 `browser_static_proxy`、再 `network_proxy`；CDP 优先 `SCANSCI_PDF_PROXY`、再 `network_proxy`，不假定外部 Chrome 的出口。设为空字符串则 HTTP 直连。Science 阅读器的普通加载宽限由 `science_reader_grace`（默认 5 秒）控制，遇到 challenge 时的等待上限由 `science_reader_timeout`（默认 60 秒）控制。

CDP 正式工作连接借用默认 context，在 `cache_dir/cdp_owned_targets.json` 登记确切创建的 target，下一次正式连接可恢复并仅关闭登记页；断开连接不关闭外部 Chrome 或 context。`SIGKILL` 恰逢建页与登记间不能保证回收，历史未知归属页不会清理。普通 CDP 路径不适用 `browser_restart_every`，不建议将其设为非零；CDP forward 仍保留。部署方仍须自行验证权限、目标站点获取及 HTTP/MCP 产物取回；不承诺每篇文章可下载、改变 IP、解验证码或任何隐身特性。`cdp` 与本地 Camoufox 依赖版本冲突，请二选一安装；本地 Patchright/CloakBrowser 仍可配置。应用本身不提供远程桌面或网页登录管理后台。

