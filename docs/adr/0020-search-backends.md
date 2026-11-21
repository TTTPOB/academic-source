# ADR 0020：检索可选 Crossref 和 PubMed 后端，各后端表达式原样透传

- 状态：Accepted
- 实施情况：已实现，见文末「实施记录」。

## 背景

`search` 只从 OpenAlex 找论文。[ADR 0019](0019-no-supplementary-discovery-source.md) 核对过一批漏收：有的文章 OpenAlex 还没收录，Crossref 里有（Nature 3 篇）；有的文章出版社还没登记到 Crossref，OpenAlex 里没有期刊归属，只有 PubMed 能按期刊查到（Science 1 篇）。当时决定不加补充来源，只在文档里提示结果可能不全。

可这样一来，需要完整清单的 agent 只能绕开 asource，自己调 Crossref 或 PubMed 的接口。用户希望这类查询也走 asource。

合并几个来源有三个难点：OpenAlex 的 filter 语法转换不到其他来源；各来源的相关度排序不同，合并后的顺序没有意义；各来源的日期定义不同。

## 决策

1. `search` 增加参数 `backend`，可选 `openalex`（默认）、`crossref`、`pubmed`。命令行为 `--backend`，HTTP、MCP 和 Python 客户端用同名参数。
2. **不自动合并**。一次检索只查一个后端，要合并由调用方自己做。
3. **`filter` 按后端原样透传**，asource 不做语法转换，也不检查语法：
   - `openalex`：OpenAlex `works` 的 `filter`，同 [ADR 0014](0014-search-filters.md)；
   - `crossref`：Crossref `/works` 的 `filter`，例如 `from-online-pub-date:2026-10-01,type:journal-article`；
   - `pubmed`：一段 PubMed 检索式，和其他条件用 `AND` 连接，例如 `2026/10/01:2026/11/20[epdat]`。

   后端拒绝时，把它的错误说明原样交给调用方，和 ADR 0014 一样。
4. 结构化参数只在能明确对应时才支持。用了所选后端不支持的参数，直接报错并说明原因，不偷偷忽略：

   | 参数 | crossref | pubmed |
   | --- | --- | --- |
   | 关键词 | `query` | 检索式正文 |
   | `journal` | `issn` filter | `[issn]` |
   | `year` | `from-pub-date` / `until-pub-date` | `[dp]` |
   | `author` | `query.author` | `[au]` |
   | `type` | `type` filter，取 Crossref 的类型名 | `[pt]`，取 PubMed 的类型名 |
   | `sort` | 只支持 `publication_date` | 只支持 `publication_date` |
   | `oa`、`min_citations`、`in` | 不支持 | 不支持 |

   `journal` 给的是刊名时，沿用 OpenAlex 的期刊解析拿到 ISSN-L，再用于其他后端。`year` 用的是各后端自己的默认日期，Crossref 是 issued 日期，PubMed 是 `[dp]`；要用在线日期，用 `filter` 透传各自的原生字段。
5. ADR 0014 的「默认排除非论文和撤稿」只用于 OpenAlex。其他后端不加默认排除，类型由调用方自己筛。
6. 返回格式不变，仍是同一种 JSON 数组。`source` 字段写明来自哪个后端。后端没有的字段（如引用数、`is_oa`）就省略。摘要缺失时，沿用 [ADR 0017](0017-full-abstracts-and-paging.md) 的按 DOI 补摘要。PubMed 结果自带摘要，另加 `pmid` 字段；没有 DOI 的 PubMed 记录也保留。
7. `limit` 上限仍是 1000。Crossref 每页 100 条，按 `offset` 翻页；PubMed 先用 `esearch` 取 PMID，再分批 `efetch`。两者的翻页上限都是 1 万条左右，1000 的上限碰不到。
8. 排序按后端能力处理，不在本地重排：
   - Crossref 不给 `sort` 时保留 Crossref 的顺序（有关键词时按相关度）；`sort=publication_date[:asc|:desc]` 换成 `sort=issued&order=…`。
   - PubMed 只支持 `publication_date` 降序（ESearch 的 `sort=pub date`），`:asc` 直接报错。不给 `sort` 时，只有筛选条件就按日期降序，有关键词或作者就用 PubMed 自己的顺序。
9. PubMed 对检索式的问题多数只给告警，照样返回放宽后的结果。asource 把这些告警当作错误：括号不配对、悬空的运算符、找不到的引号短语、`fieldsnotfound`，有结果时的 `phrasesnotfound`，都返回 400，附上 PubMed 的原话和实际执行的 `querytranslation`。没有结果时的 `phrasesnotfound` 仍返回 `[]`。

## 理由与替代方案

- **自动合并**：要解决上面三个难点，任何一种合并规则都会悄悄改变某个来源的语义。让调用方显式选后端，结果的含义一目了然。
- **把 OpenAlex 的 filter 转换成其他语法**：字段对不上，转换难免出错，而且不可能覆盖全部字段。原样透传和 ADR 0014 的思路一致。
- **给每个后端单独起参数名**（如 `--crossref-filter`）：参数会越来越多，而同一时刻只有一个后端生效，一个 `filter` 就够了。代价是同一个参数在不同后端下语法不同，要靠帮助信息和 skill 说清楚。
- **只加 Crossref 或只加 PubMed**：两者各补一类漏收（见 ADR 0019），只加一个仍有缺口。PubMed 只覆盖生物医学，Crossref 的摘要和类型取决于出版社登记了什么，两者互补。

## 后果

- [ADR 0019](0019-no-supplementary-discovery-source.md) 的第 1 条「不加补充来源」被本条取代；它要求的文档提示仍然有效。README 和 skill 里「用 Crossref 或 PubMed 核对」改成用对应的 `--backend`。
- `filter` 的语法随后端变化，帮助信息和 skill 要分别给出三种示例。
- 新增两条上游依赖。Crossref 和 PubMed 都免费，PubMed 不带 API key 时每秒最多 3 次请求。
- 检索结果的契约多了 `pmid` 字段，并允许没有 DOI 的条目。

## 验证要点

- 单元测试（`tests/academic_source/test_search_backends.py`）：各后端的参数换算；不支持的参数报错；`filter` 原样透传；后端报错的说明传到四个入口；Crossref 的 `offset` 翻页与排序参数；PubMed 分批取回、保留 ESearch 顺序、告警转为错误；结果格式与 `source` 字段。
- 线上（2026-11-21，本地模式 `asource search`）：`--backend crossref --journal 0028-0836` 加 10-19 至 11-06 的 `online-pub-date` 窗口返回 282 条，包含 10.1038/s41586-026-11161-8、-11219-7、-11228-6；`--backend pubmed --journal 0036-8075` 加 `2026/11/12[epdat]` 返回 46 条，包含 10.1126/science.aed8712（PMID 43019420）。

## 实施记录

2026-11-21 实现。实现中确认的几点：

- **Crossref 不能用游标翻页**。最初按决策第 7 条的原稿用游标（`cursor=*`），实测带游标时 Crossref 不按相关度排序：`query=Deep residual learning for image recognition` 不带游标时第 2 条是 10.1109/cvpr.2016.90，带游标时前几条全是无关的 10.1067 条目。Crossref 在游标模式下也拒绝按日期排序（HTTP 400 "Sorting by [issued, ...] is not supported when using a cursor"）。因此改为 `offset` 翻页，相关度和日期排序走同一条路径。
- **PubMed 结果的日期**：有完整的电子出版日期（`ArticleDate[@DateType='Electronic']`）时，`publication_date` 和 `year` 取它，否则取期号的 `PubDate`。这样和 `[epdat]` 检索对得上。`--year` 仍按 `[dp]` 筛选。
- **PubMed 拼错的字段标签识别不了**。`lamin[xyz]` 返回 HTTP 200，诊断字段全为空，`querytranslation` 和直接搜 `lamin` 一样。不维护本地标签表就无法把它和合法的改写（如 `[issn]` 译成 `[Journal]`）区分开，所以不处理，在帮助信息和 skill 里提示。
- **PubMed 的 `NOT` 只能写在 `query` 里**。`filter` 以 `AND (...)` 接在后面，`filter` 写 `NOT preprint[pt]` 会变成 `AND (NOT preprint[pt])`，PubMed 对此报 `NOT` 告警，按第 9 条返回 400。
- PubMed 收录 bioRxiv、medRxiv 等预印本，`doudna ja[au] AND 2025[dp]` 的前几条都是预印本。文档建议用 `NOT preprint[pt]` 排除，不设默认排除，与第 5 条一致。
