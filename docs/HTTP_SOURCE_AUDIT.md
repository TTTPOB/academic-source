# HTTP 来源核对清单

本清单覆盖 `PROVIDERS` 的全部来源，说明每个来源归入哪种后端及依据；后端如何决定并行方式见[论文并行调度](PARALLEL_SCHEDULING.md)。链接里的行号对应撰写时的代码，函数名是稳定的定位依据。从[注册表](<../src/academic_source/sources/registry.py#L104>)及[出版商分派表](<../src/scansci_pdf/sources/publishers.py#L619-L675>)追到实际函数。借用来源经 [`call_legacy`](<../src/academic_source/sources/legacy.py#L68-L109>) 调用；该适配器不发请求，也不打开浏览器。

- **`browser`**：任一路径可能驱动应用侧浏览器（包含 CDP），或委托独立的浏览器进程。
- **`http`**：无浏览器，所有 HTTP 请求经 `network.fetch`；空实现也归这类。
- **`http_ungated`**：无浏览器，但至少一条普通 HTTP 路径绕过 HTTP 闸门。同一来源也可能使用 `network.fetch`。

表中反复出现的调用链：

- [`fetch_json` → `fetch`](<../src/scansci_pdf/network.py#L130-L176>) → [`request_response`](<../src/scansci_pdf/upstream_client.py#L306-L384>)；下表的 `download_pdf` 调用如不传 `cookies`，走同一 `fetch`，传 `cookies` 则是[独立 Session](<../src/scansci_pdf/pdf_utils.py#L206-L256>)。`_write_pdf_atomic` 只[写入本地文件](<../src/scansci_pdf/sources/publishers.py#L139-L153>)。

## 常规来源与非浏览器命名的出版商来源

| 标签 | 函数及位置 | 可达机制 | 调用链 | 后端 |
|---|---|---|---|---|
| Unpaywall | [`try_unpaywall` · unpaywall.py:64](<../src/scansci_pdf/sources/unpaywall.py#L64-L116>) | 普通 HTTP：有闸门 | `fetch` 查 JSON → 提取 URL → `download_pdf`。 | `http` |
| OpenAlexOA | [`try_openalex_oa` · openalex.py:22](<../src/scansci_pdf/sources/openalex.py#L22-L51>) | 普通 HTTP：有闸门 | `openalex_params` 拼参数 → `fetch_json` → `download_pdf`。 | `http` |
| OpenAlexContent | [`try_openalex_content_api` · openalex.py:54](<../src/scansci_pdf/sources/openalex.py#L54-L109>) | 普通 HTTP：有闸门 | `fetch_json` 查作品 → `fetch(stream=True)` 下载 → 原子写盘。 | `http` |
| OpenAIRE | [`try_openaire` · openaire.py:58](<../src/scansci_pdf/sources/openaire.py#L58-L122>) | 普通 HTTP：有闸门 + 绕过闸门 | 两处 XML 回退使用 `Session.get`；JSON 路径 `fetch_json`，PDF 路径 `download_pdf`。此入口不是同名的 `oa_discovery.try_openaire`。 | `http_ungated` |
| DOAJ | [`try_doaj` · oa_discovery.py:94](<../src/scansci_pdf/sources/oa_discovery.py#L94-L135>) | 普通 HTTP：有闸门 + 绕过闸门 | `Session.get` 查元数据 → `download_pdf`。 | `http_ungated` |
| CrossrefPage | [`try_crossref_page_scrape` · crossref.py:45](<../src/scansci_pdf/sources/crossref.py#L45-L160>) | 普通 HTTP：有闸门 + 绕过闸门 | `Session.head/get` 解析 DOI、取页面 → 正则提取链接（不执行 JS）→ `download_pdf`。 | `http_ungated` |
| EuropePMC | [`try_europepmc` · europepmc.py:14](<../src/scansci_pdf/sources/europepmc.py#L14-L32>) | 普通 HTTP：有闸门 | `lookup_pmcid` 查 PMCID → getPdf → `download_pdf`。 | `http` |
| CORE | [`try_core` · core_api.py:28](<../src/scansci_pdf/sources/core_api.py#L28-L46>) | 普通 HTTP：有闸门 | `fetch_json` → 枚举候选 URL → `download_pdf`。 | `http` |
| PMC | [`try_pmc` · europepmc.py:35](<../src/scansci_pdf/sources/europepmc.py#L35-L50>) | 普通 HTTP：有闸门 | `lookup_pmcid` 查 PMCID → `find_article` 列桶 → `download_pdf`。 | `http` |
| Crossref | [`try_crossref` · crossref.py:16](<../src/scansci_pdf/sources/crossref.py#L16-L42>) | 普通 HTTP：有闸门 | `fetch_json` → 过滤 PDF 链接 → `download_pdf`。 | `http` |
| NatureDirect | [`try_nature_direct` · nature.py:13](<../src/scansci_pdf/sources/nature.py#L13-L39>) | 普通 HTTP：有闸门 | `download_pdf` 直取；失败后 `fetch` 查 HTML → 正则取链接 → `download_pdf`。 | `http` |
| PublisherDirect | [`try_publisher_direct` · publishers.py:207](<../src/scansci_pdf/sources/publishers.py#L207-L256>) | 普通 HTTP：绕过闸门 | [`resolve_doi`](<../src/scansci_pdf/sources/publishers.py#L184>) 用 `Session.head`，PDF 用 `Session.get`；[`_load_publisher_cookies`](<../src/scansci_pdf/sources/publishers.py#L24>) → [`inject_cookies`](<../src/scansci_pdf/browser_cookies.py#L327>) / [`load_saved_cookies`](<../src/scansci_pdf/browser_cookies.py#L218>) 只从文件载入。 | `http_ungated` |
| CopernicusDirect | [`try_copernicus_direct` · _publisher_strategies_core.py:3222](<../src/scansci_pdf/_publisher_strategies_core.py#L3222-L3277>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 直接 `requests.head/get`，PDF 直取失败走 `_browser_download_with_fallback`（见下）。 | `browser` |
| MDPIDirect | [`try_mdpi_direct` · publishers.py:263](<../src/scansci_pdf/sources/publishers.py#L263-L318>) | 普通 HTTP：绕过闸门 | 页面和 PDF 各用 `Session.get`，本地解析和写盘。 | `http_ungated` |
| ElsevierAPI | [`try_elsevier_api` · _publisher_strategies_core.py:2535](<../src/scansci_pdf/_publisher_strategies_core.py#L2535-L2700>) | 普通 HTTP：绕过闸门 | PDF/XML 用 `Session.get`，[对象](<../src/scansci_pdf/_publisher_strategies_core.py#L2383>)和[文本](<../src/scansci_pdf/_publisher_strategies_core.py#L2483>)回退仍是同一 Session；[`_persist_api_cookies`](<../src/scansci_pdf/_publisher_strategies_core.py#L2140>) → [`merge_cookies`](<../src/scansci_pdf/browser_cookies.py#L334>) 更新共享磁盘 Cookie，不打开浏览器。 | `http_ungated` |
| PNASDirect | [`try_pnas_direct` · publishers.py:389](<../src/scansci_pdf/sources/publishers.py#L389-L422>) | 普通 HTTP：绕过闸门 | `Session.get` → PDF 校验和写盘。 | `http_ungated` |
| SpringerTDM | [`try_springer_tdm` · springer_tdm.py:74](<../src/scansci_pdf/sources/springer_tdm.py#L74-L110>) | 普通 HTTP：绕过闸门 | [`fetch_fulltext_xml`](<../src/scansci_pdf/sources/springer_tdm.py#L28-L71>) 使用 `Session.get`，输出 XML。 | `http_ungated` |
| PLOSDirect | [`try_plos_direct` · publishers.py:429](<../src/scansci_pdf/sources/publishers.py#L429-L481>) | 普通 HTTP：绕过闸门 | `Session.get` → PDF 校验和写盘。 | `http_ungated` |
| FrontiersDirect | [`try_frontiers_direct` · publishers.py:488](<../src/scansci_pdf/sources/publishers.py#L488-L522>) | 普通 HTTP：绕过闸门 | `Session.get` → PDF 校验和写盘。 | `http_ungated` |
| BMCDirect | [`try_bmc_direct` · publishers.py:529](<../src/scansci_pdf/sources/publishers.py#L529-L567>) | 普通 HTTP：绕过闸门 | 候选逐个 `Session.get` → PDF 校验和写盘。 | `http_ungated` |
| arXiv | `_arxiv_source` → [`download_arxiv_pdf` · arxiv.py:21](<../src/scansci_pdf/sources/arxiv.py#L21-L45>) | 普通 HTTP：有闸门 | `fetch(stream=True)` 下载 → 校验和写盘。 | `http` |

## 出版商浏览器入口

以下是 [`PUBLISHER_TOOL_MAP` 和 `_FN_MAP`](<../src/scansci_pdf/sources/publishers.py#L95-L132>) 的有效交集中的浏览器入口，按[动态解析规则](<../src/scansci_pdf/sources/publishers.py#L598-L616>)核对到实际函数。共用链可在有缓存的出版商登录状态且地址像 PDF 时走[独立 Session](<../src/scansci_pdf/_publisher_strategies_core.py#L1270-L1310>)，也可在浏览器尝试后用独立 Session 再试。`browser_backend` 决定浏览器的启动或 CDP 连接方式；CDP 模式或 `interactive=false` 时不再回退到可见浏览器。表中「共享链」指 [`_browser_download_with_fallback`](<../src/scansci_pdf/_publisher_strategies_core.py#L2083-L2130>) → [`_browser_download` → `browser_engine.create_tab`](<../src/scansci_pdf/_publisher_strategies_core.py#L1644-L1696>)。映射里的 `arXiv` 没有函数条目，`GenericBrowser` 有函数条目但没有路由引用；`OpenAlexOA` 的函数条目也没有出版商路由引用，但 `_plan()` 会直接列出它。

| 标签 | 函数及位置 | 可达机制 | 调用链 | 后端 |
|---|---|---|---|---|
| ElsevierBrowser | [`try_elsevier_browser` · _publisher_strategies_core.py:2705](<../src/scansci_pdf/_publisher_strategies_core.py#L2705-L2767>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | Cell Press 专用入口[建标签](<../src/scansci_pdf/_publisher_strategies_core.py#L2768-L2777>)，另走共享链。 | `browser` |
| WileyBrowser | [`try_wiley_browser` · _publisher_strategies_core.py:2906](<../src/scansci_pdf/_publisher_strategies_core.py#L2906-L2936>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| IEEEBrowser | [`try_ieee_browser` · _publisher_strategies_core.py:2937](<../src/scansci_pdf/_publisher_strategies_core.py#L2937-L2950>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| ACSBrowser | [`try_acs_browser` · _publisher_strategies_core.py:2951](<../src/scansci_pdf/_publisher_strategies_core.py#L2951-L2970>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| RSCBrowser | [`try_rsc_browser` · _publisher_strategies_core.py:2971](<../src/scansci_pdf/_publisher_strategies_core.py#L2971-L2989>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| AIPBrowser | [`try_aip_browser` · _publisher_strategies_core.py:2990](<../src/scansci_pdf/_publisher_strategies_core.py#L2990-L3003>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| SpringerBrowser | [`try_springer_browser` · _publisher_strategies_core.py:3004](<../src/scansci_pdf/_publisher_strategies_core.py#L3004-L3023>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| APSBrowser | [`try_aps_browser` · _publisher_strategies_core.py:3024](<../src/scansci_pdf/_publisher_strategies_core.py#L3024-L3042>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| TandFBrowser | [`try_tandfonline_browser` · _publisher_strategies_core.py:3043](<../src/scansci_pdf/_publisher_strategies_core.py#L3043-L3061>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| IOPBrowser | [`try_iop_browser` · _publisher_strategies_core.py:3062](<../src/scansci_pdf/_publisher_strategies_core.py#L3062-L3080>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| OxfordBrowser | [`try_oxford_browser` · _publisher_strategies_core.py:3081](<../src/scansci_pdf/_publisher_strategies_core.py#L3081-L3099>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| ACMBrowser | [`try_acm_browser` · _publisher_strategies_core.py:3100](<../src/scansci_pdf/_publisher_strategies_core.py#L3100-L3118>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| NatureBrowser | [`try_nature_browser` · _publisher_strategies_core.py:3119](<../src/scansci_pdf/_publisher_strategies_core.py#L3119-L3133>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| RoyalSocietyBrowser | [`try_royalsociety_browser` · _publisher_strategies_core.py:3195](<../src/scansci_pdf/_publisher_strategies_core.py#L3195-L3221>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| ScienceBrowser | `try_science_browser` · _publisher_strategies_core.py | 应用侧浏览器 | `ScienceStrategy.download_sync`：在已登录的标签页里用同源 `fetch` 读取阅读器页面，解析带签名的 PDF 地址，再用 `fetch_pdf_in_tab` 取回 PDF；失败时回退到导航阅读器页面的旧流程。全程不从 Python 直接向 science.org 发请求。 | `browser` |
| SAGEBrowser | [`try_sage_browser` · _publisher_strategies_core.py:3143](<../src/scansci_pdf/_publisher_strategies_core.py#L3143-L3168>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |
| ASCEBrowser | [`try_asce_browser` · _publisher_strategies_core.py:3169](<../src/scansci_pdf/_publisher_strategies_core.py#L3169-L3194>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 共享链。 | `browser` |

## 灰色与机构来源

| 标签 | 函数及位置 | 可达机制 | 调用链 | 后端 |
|---|---|---|---|---|
| SciBban | [`try_scibban` · scibban.py:8](<../src/scansci_pdf/sources/scibban.py#L8-L10>) | 无请求 | 空实现直接返回 `None`。 | `http` |
| LibGen | [`try_libgen` · libgen.py:23](<../src/scansci_pdf/sources/libgen.py#L23-L115>) | 应用侧浏览器 + 普通 HTTP（有闸门及绕过闸门） | `_try_libgen_mirror` 用 `Session.get`，`download_pdf` 的无会话状态分支走闸门，Cloudflare/403 分支调用 [`browser_engine.solve_url`](<../src/scansci_pdf/browser_engine.py#L486>)。 | `browser` |
| Sci-Hub | [`try_scihub` · scihub.py:870](<../src/scansci_pdf/sources/scihub.py#L870-L924>) | 应用侧浏览器 + 普通 HTTP（有闸门及绕过闸门） | `_try_scihub_impl` 可提交 `_browser_first_download` / `_race_browser_domains` 到内置线程池；[`try_scihub_domain`](<../src/scansci_pdf/sources/scihub.py#L575-L686>) 先用 `fetch`，也可回退到浏览器；[带会话状态的 PDF 下载](<../src/scansci_pdf/sources/scihub.py#L725-L736>)可绕过闸门。 | `browser` |
| CARSI | [`try_carsi` · carsi_source.py:16](<../src/scansci_pdf/sources/carsi_source.py#L16-L65>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | [`CARSIClient.download_via_browser`](<../src/scansci_pdf/sources/carsi.py#L132-L219>) → `_download_via_cloakbrowser` → 页面导航；[DOI 解析](<../src/scansci_pdf/sources/instsci.py#L311-L327>)用独立的 `requests.get`。 | `browser` |
| SAGECN | `_sage_cn_source` | 普通 HTTP：绕过闸门 | [`try_sage_cn_authorized`](<../src/scansci_pdf/sources/sage_cn.py#L82-L139>) 检查和载入磁盘 Cookie，用 `Session.get` 取文章和 PDF。 | `http_ungated` |
| WebVPN | [`try_vpnsci` · vpnsci.py:25](<../src/scansci_pdf/sources/vpnsci.py#L25>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 别名 → [`try_instsci`](<../src/scansci_pdf/sources/instsci.py#L1007-L1042>) → [`_try_instsci_browser`](<../src/scansci_pdf/sources/instsci.py#L715-L744>) → `browser_backend.launch`；校园连接器先试 SOCKS5，留存的登录状态可用于 [WebVPN HTTP 回退](<../src/scansci_pdf/sources/instsci.py#L278-L327>)。 | `browser` |
| EZProxy | [`try_ezproxy` · ezproxy.py:106](<../src/scansci_pdf/sources/ezproxy.py#L106-L163>) | 应用侧浏览器 + 普通 HTTP（绕过闸门） | 先用独立的 `requests.head` 解析 DOI，再调用 `browser_backend.launch`。 | `browser` |
| SessionBroker | `_broker_source` | 文件队列 → 独立浏览器进程 | [`_try_session_broker`](<../src/scansci_pdf/institutional/instsci_bridge.py#L243-L273>) 仅在有出版商配置且 broker 已运行时提交；[`submit_broker_job`](<../src/scansci_pdf/institutional/session_broker.py#L85-L125>) 写文件并等结果。[模块约定](<../src/scansci_pdf/institutional/session_broker.py#L1-L5>)为每个出版商保留一个 CloakBrowser 上下文；仓库无队列消费者实现，无法核实部署的 worker。 | `browser` |
| InstitutionalBrowser | `_publisher_batch_source` | 应用侧浏览器 + 普通 HTTP（绕过闸门） | [`_try_browser_batch_download`](<../src/scansci_pdf/institutional/instsci_bridge.py#L294-L352>) → [`_launch_context` / `fetch_one`](<../src/scansci_pdf/institutional/publisher_batch.py#L439-L455>) → 持久上下文、打开页面；[PDF 回退](<../src/scansci_pdf/institutional/publisher_batch.py#L1853-L1910>)用独立的 `requests.get`。 | `browser` |

## OA 快路径（按阶段分流）

[`acquire_oa`](<../src/academic_source/sources/oa.py#L306-L410>)先用 [`discover` / `_metadata`](<../src/academic_source/sources/oa.py#L79-L193>)发出有闸门的元数据请求，再用 [`_http_candidate`](<../src/academic_source/sources/oa.py#L222-L303>)验证候选 PDF。OpenAlexContent 的候选来自 OpenAlex 元数据。快路径只用 HTTP，不开浏览器；被拦截的候选留给后面的出版商来源处理。
