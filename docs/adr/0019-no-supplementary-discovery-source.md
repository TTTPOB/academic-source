# ADR 0019：检索暂不加补充来源，只说明 OpenAlex 可能漏收

- 状态：Accepted（第 1 条被 [ADR 0020](0020-search-backends.md) 取代）
- 实施情况：已实现，只改了 README 和 agent skill 的说明，代码不变。漏收的原因与各候选来源的表现是 2026-11-21 实测的，见「理由与替代方案」。

## 背景

`search` 只从 OpenAlex 找论文（[ADR 0014](0014-search-filters.md)）。2026-11-20 的一次 agent 会话按期刊和日期列出 Cell、Nature、Science 在 2026-07-20 至 11-20 的全部研究论文，最后从 PubMed 或出版社目录补了 asource 没返回的文章：Science 17 篇，Nature 6 篇，Cell 6 篇。

问题是：要不要再接入一个来源，补上 OpenAlex 漏掉的文章。

## 决策

1. `search` 仍然只用 OpenAlex 发现论文，不加 Crossref、PubMed 或其他补充来源。
2. 在 README 和 agent skill 里说明，按期刊和日期列出的结果可能不全，需要完整清单时用 Crossref 或 PubMed 核对。具体写明下面四点：
   - 最近一两天上线的文章可能还没入库；
   - 个别文章上线几周后仍未收录，或者收录了但没有期刊归属，按期刊查不到；
   - OpenAlex 的 `publication_date` 一般是在线首发日，可能比 PubMed 的日期早几周；
   - OpenAlex 会把部分研究论文标成 `review`，用 `--type article` 筛选时会漏掉它们。
3. skill 里「列出期刊某段时间全部文章」的示例去掉 `--type article`。默认已经排除书籍、勘误等非论文类型；`article` 筛不掉被标成 `article` 的新闻，却会丢掉被标成 `review` 的研究论文。

以后漏收的例子明显增多，或者有了能同时覆盖下面两类漏收的来源，再重新考虑。

## 理由与替代方案

逐篇核对了上面那批文章漏收的原因（OpenAlex、Crossref 实测于 2026-11-21）：

| 期刊 | 篇数 | 原因 |
| --- | --- | --- |
| Science | 9 | 日期定义不同。OpenAlex 的发表日期（2026-05-21 至 07-09）比 agent 采用的 PubMed 日期早 6–10 周，落在窗口之前。不算漏收 |
| Science | 8 | 都是 11-19 上线，OpenAlex 11-20 才建记录，而检索在 11-20 当天进行。第二天都能查到 |
| Science | 1 | 10.1126/science.aed8712：出版社还没登记到 Crossref（404），OpenAlex 只从 PubMed 收到它，`primary_location` 是 PubMed，没有期刊归属，按期刊查不到 |
| Nature | 3 | 10.1038/s41586-026-10438-2、-10992-9、-10935-4 被 OpenAlex 标成 `review`，agent 加了 `--type article` |
| Nature | 3 | 10.1038/s41586-026-11161-8（10-21 上线）、-11219-7（10-28）、-11228-6（11-04）至 11-21 仍不在 OpenAlex |
| Cell | 6 | 原始检索文件没留下，无法逐篇还原。188 篇现在都能按条件查到；从入库时间看，可能是入库延迟加上「日期记为当月 1 日」 |

真正的漏收是两类：OpenAlex 没收录（Nature 3 篇），以及收录了但没有期刊归属（Science 1 篇）。没有哪个候选来源能同时补上这两类：

- **Crossref，按 ISSN 和在线日期列出**：Nature 那 3 篇都在，上线当天就已登记。aed8712 在 Crossref 里不存在；Science 在 11-10 至 11-14 只登记了 5 条，出版社登记明显滞后。
- **PubMed**：能按期刊查到 aed8712，Nature 那 3 篇没查。只覆盖生物医学。合并检索还有三处难点：[ADR 0014](0014-search-filters.md) 原样转发的 OpenAlex filter 没法转换成 PubMed 语法；两边的相关度排序不同，合并后的顺序没有意义；两边的日期定义不同，上面 Science 的 9 篇就是这个原因，合并时必须另定以哪个日期为准。
- **Scopus Search API**：服务端的 Elsevier key 可以用，靠校园网 IP 取得授权，每周 2 万次。按 DOI 能查到全部 4 篇；按 ISSN 能查到 Nature 那 3 篇，查不到 aed8712（它挂在 MEDLINE 刊名「Science New York N Y」下，没有 Science 的 ISSN）。Science 在 11-10 后入库的研究论文按 ISSN 查是 0 条。尚未出印刷版的文章 `coverDate` 是占位值 2026-01-01。另受机构授权和商业条款限制。效果和 Crossref 相同，约束更多。
- **OpenAlex 按 DOI 开头筛选**（如 `doi_starts_with:10.1126/science.`）：能查到 aed8712，但要为每本期刊维护 DOI 写法对照表，不通用。11-12 这一天比按 ISSN 多出的另 5 条都是被标成 `dataset` 的新闻。
- **出版社 RSS**：不能按日期或类型筛选，只有最近的条目，还要逐家适配。

接入任何一个来源都要新增依赖和合并逻辑，只能补上其中一类漏收。目前只有一次会话、三本期刊的证据，所以先只把限制写清楚。

## 后果

- 按期刊和日期列出的结果仍可能漏掉少量文章。需要完整清单的调用方要自己用 Crossref 或 PubMed 核对。
- 没有新增依赖和维护成本，检索行为与返回格式不变。

## 验证要点

已验证：

- README 和 skill 写明上述四点；skill 示例不再带 `--type article`。
- 上表各条原因可以复查：OpenAlex `works?filter=doi:…`、Crossref `/works/{doi}` 和 `/journals/{issn}/works?filter=from-online-pub-date:…`。

未验证：

- Nature 那 3 篇在 PubMed 里有没有。
- 出版社向 Crossref 登记的一般延迟，以及这类漏收在其他期刊上的比例。
