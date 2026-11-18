# HTTP 来源核对清单

[论文并行调度设计](PARALLEL_SCHEDULING.md)需要知道每个来源能否在论文线程上执行。本清单逐个函数沿调用链核对，给出结论。范围是来源计划里出现的全部来源、出版商路由能返回的全部函数、OA 快路径和 arXiv 入口。核对基于 2026-11-18 的代码（提交 `a6009c3`），`scansci_pdf` 的行号以该提交为准；`academic_source` 里的函数只写函数名，包装函数在 `sources/registry.py`，OA 快路径在 `sources/oa.py`。

两种结论：

- **`http`**：所有对外请求都经过 `network.fetch`，没有浏览器分支。可以在论文线程执行。
- **`browser`**：只能在唯一的浏览器线程上执行。除了真正会打开浏览器的来源，还包括两类：没有核对清楚的；不开浏览器但用自己的 `requests.Session`、绕过 HTTP 闸门的。后一类在「触达浏览器」一列写「否」，改为经 `network.fetch` 发请求并重新核对后可以改标 `http`。

表中反复出现的调用链：

- [`fetch_json` → `fetch`](<../src/scansci_pdf/network.py#L130-L176>) → [`request_response`](<../src/scansci_pdf/upstream_client.py#L306-L384>)；下表的 `download_pdf` 调用如不传 `cookies`，走同一 `fetch`，传 `cookies` 则是[独立 Session](<../src/scansci_pdf/pdf_utils.py#L206-L256>)。`_write_pdf_atomic` 只[写入本地文件](<../src/scansci_pdf/sources/publishers.py#L139-L153>)。

## 常规来源与非浏览器命名的出版商来源

| 标签 | 函数及位置 | 只走 `network.fetch` | 触达浏览器与调用链 | 结论 |
|---|---|---|---|---|
| Unpaywall（含出版商别名） | [`try_unpaywall` · unpaywall.py:64](<../src/scansci_pdf/sources/unpaywall.py#L64-L116>) | 是 | 否；`fetch` 查 JSON → 提取 URL → `download_pdf`。 | `http` |
| OpenAlexOA | [`try_openalex_oa` · openalex.py:22](<../src/scansci_pdf/sources/openalex.py#L22-L51>) | 是 | 否；`openalex_params` 拼参数 → `fetch_json` → `download_pdf`。 | `http` |
| OpenAlexContent | [`try_openalex_content_api` · openalex.py:54](<../src/scansci_pdf/sources/openalex.py#L54-L109>) | 是 | 否；`fetch_json` 查作品 → `fetch(stream=True)` 下载 → 原子写盘。 | `http` |
| OpenAIRE | [`try_openaire` · openaire.py:58](<../src/scansci_pdf/sources/openaire.py#L58-L122>) | 否 | 否；两处 XML 回退使用 `Session.get`；JSON 路径 `fetch_json`，PDF 路径 `download_pdf`。此入口不是同名的 `oa_discovery.try_openaire`。 | `browser` |
| DOAJ | [`try_doaj` · oa_discovery.py:94](<../src/scansci_pdf/sources/oa_discovery.py#L94-L135>) | 否 | 否；`Session.get` 查元数据 → `download_pdf`。 | `browser` |
| CrossrefPage | [`try_crossref_page_scrape` · crossref.py:45](<../src/scansci_pdf/sources/crossref.py#L45-L160>) | 否 | 否；`Session.head/get` 解析 DOI、取页面 → 正则提取链接（不执行 JS）→ `download_pdf`。 | `browser` |
| EuropePMC | [`try_europepmc` · europepmc.py:34](<../src/scansci_pdf/sources/europepmc.py#L34-L49>) | 是 | 否；`fetch_json` → 提取全文链接 → `download_pdf`。 | `http` |
| CORE | [`try_core` · core_api.py:28](<../src/scansci_pdf/sources/core_api.py#L28-L46>) | 是 | 否；`fetch_json` → 枚举候选 URL → `download_pdf`。 | `http` |
| PMC | [`try_pmc` · europepmc.py:52](<../src/scansci_pdf/sources/europepmc.py#L52-L74>) | 是 | 否；`fetch_json` 查 PMCID → `download_pdf`。 | `http` |
| Crossref | [`try_crossref` · crossref.py:16](<../src/scansci_pdf/sources/crossref.py#L16-L42>) | 是 | 否；`fetch_json` → 过滤 PDF 链接 → `download_pdf`。 | `http` |
| NatureDirect | [`try_nature_direct` · nature.py:13](<../src/scansci_pdf/sources/nature.py#L13-L39>) | 是 | 否；`download_pdf` 直取；失败后 `fetch` 查 HTML → 正则取链接 → `download_pdf`。 | `http` |
| PublisherDirect | [`try_publisher_direct` · publishers.py:207](<../src/scansci_pdf/sources/publishers.py#L207-L256>) | 否 | 否；[`resolve_doi`](<../src/scansci_pdf/sources/publishers.py#L184>) 用 `Session.head`，PDF 用 `Session.get`；[`_load_publisher_cookies`](<../src/scansci_pdf/sources/publishers.py#L24>) → [`inject_cookies`](<../src/scansci_pdf/browser_cookies.py#L327>) / [`load_saved_cookies`](<../src/scansci_pdf/browser_cookies.py#L218>) 只从文件载入。 | `browser` |
| CopernicusDirect | [`try_copernicus_direct` · _publisher_strategies_core.py:3222](<../src/scansci_pdf/_publisher_strategies_core.py#L3222-L3277>) | 否 | 是；直接 `requests.head/get`，PDF 直取失败走 `_browser_download_with_fallback`（见下）。 | `browser` |
| MDPIDirect | [`try_mdpi_direct` · publishers.py:263](<../src/scansci_pdf/sources/publishers.py#L263-L318>) | 否 | 否；页面和 PDF 各用 `Session.get`，本地解析和写盘。 | `browser` |
| ElsevierAPI | [`try_elsevier_api` · _publisher_strategies_core.py:2535](<../src/scansci_pdf/_publisher_strategies_core.py#L2535-L2700>) | 否 | 否；PDF/XML 用 `Session.get`，[对象](<../src/scansci_pdf/_publisher_strategies_core.py#L2383>)和[文本](<../src/scansci_pdf/_publisher_strategies_core.py#L2483>)回退仍是同一 Session；[`_persist_api_cookies`](<../src/scansci_pdf/_publisher_strategies_core.py#L2140>) → [`merge_cookies`](<../src/scansci_pdf/browser_cookies.py#L334>) 更新共享磁盘 Cookie，不打开浏览器。 | `browser` |
| ScienceDirect | [`try_science_direct` · publishers.py:325](<../src/scansci_pdf/sources/publishers.py#L325-L382>) | 否 | 是；`_fetch_pdf_camofox` → [`camofox.solve_url`](<../src/scansci_pdf/camofox.py#L86-L115>) 向远端浏览器提交建标签和导航；返回值包装成 `requests.Response` 并不改变后端。 | `browser` |
| PNASDirect | [`try_pnas_direct` · publishers.py:389](<../src/scansci_pdf/sources/publishers.py#L389-L422>) | 否 | 否；`Session.get` → PDF 校验和写盘。 | `browser` |
| SpringerTDM | [`try_springer_tdm` · springer_tdm.py:74](<../src/scansci_pdf/sources/springer_tdm.py#L74-L110>) | 否 | 否；[`fetch_fulltext_xml`](<../src/scansci_pdf/sources/springer_tdm.py#L28-L71>) 使用 `Session.get`，输出 XML。 | `browser` |
| PLOSDirect | [`try_plos_direct` · publishers.py:429](<../src/scansci_pdf/sources/publishers.py#L429-L481>) | 否 | 否；`Session.get` → PDF 校验和写盘。 | `browser` |
| FrontiersDirect | [`try_frontiers_direct` · publishers.py:488](<../src/scansci_pdf/sources/publishers.py#L488-L522>) | 否 | 否；`Session.get` → PDF 校验和写盘。 | `browser` |
| BMCDirect | [`try_bmc_direct` · publishers.py:529](<../src/scansci_pdf/sources/publishers.py#L529-L567>) | 否 | 否；候选逐个 `Session.get` → PDF 校验和写盘。 | `browser` |
| arXiv（独立入口） | `_arxiv_source` → [`download_arxiv_pdf` · arxiv.py:21](<../src/scansci_pdf/sources/arxiv.py#L21-L45>) | 是 | 否；`fetch(stream=True)` 下载 → 校验和写盘。 | `http` |

## 出版商浏览器入口

以下是 [`PUBLISHER_TOOL_MAP` 和 `_FN_MAP`](<../src/scansci_pdf/sources/publishers.py#L95-L132>) 的有效交集中的浏览器入口，按[动态解析规则](<../src/scansci_pdf/sources/publishers.py#L598-L616>)核对到实际函数。表中「共享链」指 [`_browser_download_with_fallback`](<../src/scansci_pdf/_publisher_strategies_core.py#L2083-L2130>) → [`_browser_download` → `browser_engine.create_tab`](<../src/scansci_pdf/_publisher_strategies_core.py#L1644-L1696>)。映射里的 `arXiv` 没有函数条目，`GenericBrowser` 有函数条目但没有路由引用；`OpenAlexOA` 的函数条目也没有出版商路由引用，但 `_plan()` 会直接列出它。

| 标签 | 实际函数及位置 | 只走 `network.fetch` | 触达浏览器与调用链 | 结论 |
|---|---|---|---|---|
| ElsevierBrowser | [`try_elsevier_browser` · _publisher_strategies_core.py:2705](<../src/scansci_pdf/_publisher_strategies_core.py#L2705-L2767>) | 否 | 是；Cell Press 专用入口[建标签](<../src/scansci_pdf/_publisher_strategies_core.py#L2768-L2777>)，另走共享链。 | `browser` |
| WileyBrowser | [`try_wiley_browser` · _publisher_strategies_core.py:2906](<../src/scansci_pdf/_publisher_strategies_core.py#L2906-L2936>) | 否 | 是；共享链。 | `browser` |
| IEEEBrowser | [`try_ieee_browser` · _publisher_strategies_core.py:2937](<../src/scansci_pdf/_publisher_strategies_core.py#L2937-L2950>) | 否 | 是；共享链。 | `browser` |
| ACSBrowser | [`try_acs_browser` · _publisher_strategies_core.py:2951](<../src/scansci_pdf/_publisher_strategies_core.py#L2951-L2970>) | 否 | 是；共享链。 | `browser` |
| RSCBrowser | [`try_rsc_browser` · _publisher_strategies_core.py:2971](<../src/scansci_pdf/_publisher_strategies_core.py#L2971-L2989>) | 否 | 是；共享链。 | `browser` |
| AIPBrowser | [`try_aip_browser` · _publisher_strategies_core.py:2990](<../src/scansci_pdf/_publisher_strategies_core.py#L2990-L3003>) | 否 | 是；共享链。 | `browser` |
| SpringerBrowser | [`try_springer_browser` · _publisher_strategies_core.py:3004](<../src/scansci_pdf/_publisher_strategies_core.py#L3004-L3023>) | 否 | 是；共享链。 | `browser` |
| APSBrowser | [`try_aps_browser` · _publisher_strategies_core.py:3024](<../src/scansci_pdf/_publisher_strategies_core.py#L3024-L3042>) | 否 | 是；共享链。 | `browser` |
| TandFBrowser | [`try_tandfonline_browser` · _publisher_strategies_core.py:3043](<../src/scansci_pdf/_publisher_strategies_core.py#L3043-L3061>) | 否 | 是；共享链。 | `browser` |
| IOPBrowser | [`try_iop_browser` · _publisher_strategies_core.py:3062](<../src/scansci_pdf/_publisher_strategies_core.py#L3062-L3080>) | 否 | 是；共享链。 | `browser` |
| OxfordBrowser | [`try_oxford_browser` · _publisher_strategies_core.py:3081](<../src/scansci_pdf/_publisher_strategies_core.py#L3081-L3099>) | 否 | 是；共享链。 | `browser` |
| ACMBrowser | [`try_acm_browser` · _publisher_strategies_core.py:3100](<../src/scansci_pdf/_publisher_strategies_core.py#L3100-L3118>) | 否 | 是；共享链。 | `browser` |
| NatureBrowser | [`try_nature_browser` · _publisher_strategies_core.py:3119](<../src/scansci_pdf/_publisher_strategies_core.py#L3119-L3133>) | 否 | 是；共享链。 | `browser` |
| RoyalSocietyBrowser | [`try_royalsociety_browser` · _publisher_strategies_core.py:3195](<../src/scansci_pdf/_publisher_strategies_core.py#L3195-L3221>) | 否 | 是；共享链。 | `browser` |
| ScienceBrowser | [`try_science_browser` · _publisher_strategies_core.py:3134](<../src/scansci_pdf/_publisher_strategies_core.py#L3134-L3140>) | 否 | 是；[`ScienceStrategy.download_sync`](<../src/scansci_pdf/publisher_strategies/science.py#L70-L84>) → 共享链。 | `browser` |
| SAGEBrowser | [`try_sage_browser` · _publisher_strategies_core.py:3143](<../src/scansci_pdf/_publisher_strategies_core.py#L3143-L3168>) | 否 | 是；共享链。 | `browser` |
| ASCEBrowser | [`try_asce_browser` · _publisher_strategies_core.py:3169](<../src/scansci_pdf/_publisher_strategies_core.py#L3169-L3194>) | 否 | 是；共享链。 | `browser` |

## 灰色与机构来源

| 标签 | 函数及位置 | 只走 `network.fetch` | 触达浏览器与调用链 | 结论 |
|---|---|---|---|---|
| SciBban | [`try_scibban` · scibban.py:8](<../src/scansci_pdf/sources/scibban.py#L8-L10>) | 无请求 | 否；空实现直接返回 `None`。 | `http`（空实现） |
| LibGen | [`try_libgen` · libgen.py:23](<../src/scansci_pdf/sources/libgen.py#L23-L115>) | 否 | 是；`_try_libgen_mirror` 用 `Session.get`，Cloudflare/403 分支调用 [`browser_engine.solve_url`](<../src/scansci_pdf/browser_engine.py#L486>)。 | `browser` |
| Sci-Hub | [`try_scihub` · scihub.py:870](<../src/scansci_pdf/sources/scihub.py#L870-L924>) | 否 | 是；`_try_scihub_impl` 可提交 `_browser_first_download` / `_race_browser_domains` 到内置线程池；[`try_scihub_domain`](<../src/scansci_pdf/sources/scihub.py#L575-L686>) 也可回退到浏览器。 | `browser` |
| CARSI | [`try_carsi` · carsi_source.py:16](<../src/scansci_pdf/sources/carsi_source.py#L16-L65>) | 否 | 是；[`CARSIClient.download_via_browser`](<../src/scansci_pdf/sources/carsi.py#L132-L219>) → `_download_via_cloakbrowser` → 页面导航。 | `browser` |
| SAGECN | `_sage_cn_source` | 否 | 否；[`try_sage_cn_authorized`](<../src/scansci_pdf/sources/sage_cn.py#L82-L139>) 检查和载入磁盘 Cookie，用 `Session.get` 取文章和 PDF。 | `browser` |
| WebVPN | [`try_vpnsci` · vpnsci.py:25](<../src/scansci_pdf/sources/vpnsci.py#L25>) | 否 | 是；别名 → [`try_instsci`](<../src/scansci_pdf/sources/instsci.py#L1007-L1042>) → [`_try_instsci_browser`](<../src/scansci_pdf/sources/instsci.py#L715-L744>) → `browser_backend.launch`。 | `browser` |
| EZProxy | [`try_ezproxy` · ezproxy.py:106](<../src/scansci_pdf/sources/ezproxy.py#L106-L163>) | 否 | 是；下载入口本身就调用 `browser_backend.launch`，不是仅登录时才使用浏览器。 | `browser` |
| SessionBroker | `_broker_source` | 无本地 HTTP | 可能（进程外）；[`_try_session_broker`](<../src/scansci_pdf/institutional/instsci_bridge.py#L243-L273>) → [`submit_broker_job`](<../src/scansci_pdf/institutional/session_broker.py#L85-L125>) 写队列文件、等结果；外部执行器不在此调用链内，不能证明它不访问浏览器。 | `browser` |
| InstitutionalBrowser | `_publisher_batch_source` | 否 | 是；[`_try_browser_batch_download`](<../src/scansci_pdf/institutional/instsci_bridge.py#L294-L352>) → [`_launch_context` / `fetch_one`](<../src/scansci_pdf/institutional/publisher_batch.py#L439-L455>) → 持久上下文、打开页面。 | `browser` |

## OA 快路径（按阶段分流）

| 标签/阶段 | 函数及位置 | 只走 `network.fetch` | 触达浏览器与调用链 | 结论 |
|---|---|---|---|---|
| OA 快路径整体 | `acquire_oa` | 否 | 是；先逐服务发现并验证 HTTP 候选；没成功时，CDP 配置下重试最多两个 `blocked` 候选。 | `browser`（混合；不整体派发） |
| Unpaywall、OpenAlexOA、EuropePMC、PMC 元数据 | `discover` / `_metadata` | 是 | 否；逐服务 `fetch`，再构造候选；OpenAlexContent 属于 OpenAlex 产出的候选，无单独元数据请求。 | `http` |
| 上述来源和 OpenAlexContent 的候选验证 | `_http_candidate` | 是 | 否；流式 `fetch`；HTML 解析只产出 URL，检测拦截只返回状态。 | `http` |
| OACDP | `_browser_candidate` | 否 | 是；`browser_engine.create_tab` → `navigate_tab` / `get_snapshot` → `fetch_pdf_in_tab` → `close_tab`。 | `browser` |
