# ADR 0013：PMC 收录的论文从 PMC Cloud 下载

- 状态：Accepted。取代 [ADR 0012](0012-supplementary-files.md) 的第 1 条（补充材料先查 Europe PMC），0012 的其余各条继续有效。
- 实施情况：已实现。单元测试覆盖查号、文件列表、正文 PDF、补充材料匹配与并行下载；各接口的线上表现见下文实测。部署后的 NAS 验收见「验证要点」。

## 背景

PMC 收录的论文原来通过 Europe PMC 的 REST API（`www.ebi.ac.uk/europepmc/webservices/rest`）获取：用它查 PMCID 和全文链接，读全文 XML，下载补充材料压缩包。这个接口很慢，也不稳定。2026-11-19 至 20 日从 NAS 实测：

| 请求 | 结果 |
| --- | --- |
| DOI 检索 | 0.5–60 s，时有 503 和超时 |
| 全文 XML（PMC8371605） | 首字节约 53 s |
| 补充材料压缩包（PMC8371605，28.9 MB） | 首字节 33–53 s，再下载 19–27 s |

从本机经 WARP 出口和从直连出口测，结果相同；同一主机上的静态页面只要 0.1–0.6 s，所以慢在 REST 服务本身，不在网络出口。其他项目也遇到了同样的问题，例如 biomcp 的 issue 2294 提到压缩包请求卡住，pubmed-mcp-server 2.10.15 和 2.10.18 为 5xx 加了重试。线上日志里，EuropePMC 来源成功 22 次，`network_error` 15 次，`blocked` 6 次。

这个压缩包接口还只提供开放获取子集。作者手稿 PMC6687398 会返回 HTTP 200 和一条「not open access」的错误记录。

NCBI 在 2026-02-12 宣布，从 2026 年 8 月起，PMC Cloud Service 成为 PMC 文件的主要分发方式，FTP 打包文件和 OA Web Service（`oa.fcgi`）停用。实测 `oa.fcgi` 已返回 404。

## 决策

1. **查号**：用 NCBI ID Converter（`pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/`）把 DOI 转成 PMCID。
2. **列文件**：在公开 S3 桶 `pmc-oa-opendata` 里按前缀 `PMCID.` 列出对象（ListObjectsV2），取最新版本的目录 `PMCID.N/`，同时拿到每个文件的大小。开放获取论文的目录里有 PDF、JATS XML、插图和补充文件；作者手稿只有 XML、纯文本和 JSON。
3. **正文 PDF**：
   - `PMC` 来源下载桶里的 `PMCID.N.pdf`。
   - `EuropePMC` 来源下载 Europe PMC 网站的 `europepmc.org/api/getPdf?pmcid=...`。作者手稿在桶里没有 PDF，这个接口有。
   - OA 快路径对每篇只查一次 PMCID，先试桶里的 PDF，再试 getPdf。Unpaywall 和 OpenAlex 给出的 PMC 位置只换算成 getPdf（后改为先试桶，见 [ADR 0018](0018-pmc-locations-bucket-first.md)），不再尝试 Europe PMC 文章页、`?pdf=render` 和 PMC 的 `/pdf/` 页面。
4. **补充材料第一步改为 PMC Cloud**，不再回退到 Europe PMC：
   - 从桶里读 JATS XML，取每个文件的标题和说明；
   - 跳过文章正文自己的插图；
   - 下载前按列表里的大小检查单文件 200 MB、单篇 600 MB 的上限，写入时也按这个大小截断；
   - 一篇论文的文件最多 6 个并行下载，桶所在主机在请求闸门里允许 6 个并发、不设启动间隔；
   - 产物 `note` 记为「Obtained from PMC (PMCID)」，来源页写 PMC 文章页。
   PMC Cloud 没有文件时，照旧走 ADR 0012 的出版商规则。
5. 项目不再调用 Europe PMC REST API。来源名 `EuropePMC` 和 `PMC` 保持不变，已有的来源计划和日志格式不受影响。
6. 补充材料的缓存版本从 2 升到 3，旧的缓存结果会重新获取一次。

## 理由与替代方案

PMC Cloud 各步骤的实测（2026-11-19 至 20 日，NAS 和本机）：

| 请求 | 耗时 |
| --- | --- |
| ID Converter | 0.4–2.3 s |
| S3 文件列表 | 1–2 s |
| 单个文件 | 首字节约 1.1 s，单连接 2–4 MB/s |
| 桶里的 PDF（PMC8371605，3.65 MB） | 首块 1.11 s |
| getPdf（PMC8371605） | 首块 1.14 s |
| getPdf（作者手稿 PMC6687398，3.39 MB） | 首块 0.79 s |

下载方式的比较（PMC8371605 的 6 个补充文件，NAS）：

| 方式 | 耗时 |
| --- | --- |
| 逐个下载 | 17.2 s |
| 6 个线程各下一个文件 | 3.8–4.2 s |
| 一个 10 MB 文件分成 4 段并行 | 2.7–3.4 s，整段下载为 3.2–3.4 s |

单连接速度是瓶颈，按文件并行就能拿到主要收益；同一文件分段下载基本没有提升。因此只用 Python 线程池按文件并行。aria2c 或 boto3 要多一个依赖或外部程序，在这组数据下没有额外收益，没有采用。

其他做法：

- **保留 Europe PMC REST 作为后备**：它慢到首字节就要几十秒，作为后备只会拖长失败时间，而且它能提供的文件 PMC Cloud 都有。
- **用 efetch 取 JATS**（1.2–1.7 s）：桶里已有同一份 XML，少一个服务。
- **用 PMC 或 Europe PMC 的网页取 PDF**：Europe PMC 文章页和 `?pdf=render` 返回 Cloudflare 403；PMC 的 `/pdf/` 页面返回「Preparing to download」脚本页或 reCAPTCHA。

## 后果

- PMC 收录的开放获取论文，正文和补充材料都不再经过浏览器或 Europe PMC REST。
- 作者手稿的正文仍靠 getPdf。这是 Europe PMC 网站使用的接口，没有公开承诺，以后可能变化。它并不是每篇作者手稿都有：部署后抽查 3 篇，PMC6687398 返回 PDF，PMC6407643 和 PMC6690346 返回 HTTP 500 和 `{"error": "Failed to retrieve PDF for pmcid: ..."}`；短时间连续请求还会收到 429。失败在尝试日志里记为 `network_error`，获取流程照常进入后面的来源（PMC6407643 最后由 OpenAlexContent 取得）。
- 作者手稿在 PMC Cloud 里没有补充文件，补充材料会走出版商规则。
- ID Converter 请求只带 `tool=academic-source`，没有带 NCBI 建议的 `email` 参数，响应里会有一条提醒，目前不影响结果。
- 桶里的文件由 NCBI 定期更新，新收录的论文可能晚几天出现。

## 验证要点

已验证：

- 单元测试：查号、分页列表、最新版本选择、作者手稿无 PDF；补充材料与 JATS 的匹配、插图跳过、大小上限、并行下载按 JATS 顺序返回；正文的两个来源和 OA 快路径。
- 本机实测：PMC8371605、PMC3902920、PMC6687398 能查到 PMCID 并列出文件；10.1093/bioinformatics/btz076 不在 PMC，返回空。
- 全仓搜索不再有 Europe PMC REST 和旧 ID Converter 的调用。

部署后在 NAS 上验证（2026-11-20，版本 2a7c47e）：

- PMC8371605：6 个补充文件全部来自 PMC，没有正文插图，服务端耗时 9.1 s（原来 Europe PMC 的压缩包仅首字节就要 33–53 s）；
- PMC3902920：2 个补充文件来自 PMC，服务端耗时 6.1 s；
- 作者手稿 10.1016/j.cell.2019.05.031：PMC 没有补充文件，3 个文件经 Elsevier API 取得；
- 不在 PMC 的 10.1093/bioinformatics/btz076：补充文件经 Oxford 出版商页面取得；
- 4 篇未缓存的 PMC 论文正文都取得了，其中 3 篇由 Unpaywall 给出的地址先成功，没有走到 PMC 这一步；作者手稿 PMC6407643 的 getPdf 返回 500，最后由 OpenAlexContent 取得。

还没在线上看到 `PMC` 来源本身取得正文的例子：这一步排在 Unpaywall 和 OpenAlex 之后，常见论文在前两步就已成功。
