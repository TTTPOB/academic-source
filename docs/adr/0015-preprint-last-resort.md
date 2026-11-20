# ADR 0015：预印本只在其他版本都拿不到时才用

- 状态：Accepted
- 实施情况：已实现。单元测试覆盖版本判断、排序和最后兜底；线上验证见「验证要点」。

## 背景

OA 快路径从 Unpaywall 和 OpenAlex 取候选地址，第一个下载成功的有效 PDF 就被采用。Unpaywall 的候选按「仓储地址在前、出版商地址在后」排序，因为出版商页面常被反爬拦截；而 bioRxiv、arXiv 这类预印本服务器也算仓储，于是预印本经常排在第一位。

2026-11-20 对生产上存下的 229 份正文 PDF 逐份检查首页。去掉 7 份本来请求的就是预印本的，剩下 222 份里有 **46 份（20.7%）是预印本**，涉及 43 个 DOI：45 份来自 bioRxiv，1 份来自 Research Square；44 份由 Unpaywall 的候选取得，2 份由 OpenAlexContent 取得。

复测三个例子（从 NAS 用服务的网络栈，按旧顺序）：

| DOI | 第一个候选 | 后面的正式版或接收稿 |
| --- | --- | --- |
| 10.1371/journal.pbio.3002000 | bioRxiv PDF，200，3.6 s | PMC（getPdf）200，5.7 s；PLOS 出版商 PDF 200，2.6 s，两者内容相同 |
| 10.1038/s41593-019-0364-9 | bioRxiv PDF，200，8.6 s | PMC 接收稿 getPdf 500；PMC Cloud 桶里有正式版 PDF，0.7 s |
| 10.1016/j.cell.2018.11.029 | bioRxiv PDF，200，5.7 s | PMC 接收稿 getPdf 500；cell.com 出版商 PDF 403 |

Unpaywall 和 OpenAlex 的每个位置都标了 `version`：`publishedVersion`（正式版）、`acceptedVersion`（接收稿）、`submittedVersion`（预印本），旧代码没有用它。

## 决策

1. 候选带上「是否预印本」。判断依据是位置的 `version`；`version` 缺失时，按主机判断（arxiv.org、biorxiv.org、medrxiv.org、chemrxiv.org、researchsquare.com、ssrn.com、preprints.org、osf.io）。从预印本页面里找到的 PDF 链接也算预印本。
2. OA 快路径先试所有非预印本候选，包括 PMC Cloud 和 getPdf；预印本候选先放到一边。
3. 快路径失败后，照常走出版商、灰色和机构来源。**全部失败后**才试放在一边的预印本。
4. 用预印本时，结果的 `warnings` 写明：「Only a preprint could be fetched: this PDF is the version submitted before peer review, not the published article.」
5. 请求的标识本身就是预印本时（bioRxiv/medRxiv 的日期或纯数字后缀、10.64898、Research Square、SSRN、Preprints.org、ChemRxiv、OSF、arXiv），预印本照常直接使用。10.1101 也是冷泉港期刊（如 `10.1101/gr.…`、`10.1101/gad.…`）的前缀，所以只按后缀格式认定 bioRxiv/medRxiv。
6. 正文结果的缓存版本加 1。已缓存的 229 篇在下次请求时重新获取，其中的预印本会被正式版替换（拿得到的话）。

## 理由与替代方案

- **完全不用预印本**：版本最干净，但只有预印本公开的论文就拿不到了。
- **只在结果里标注版本，不改顺序**：五分之一的结果仍是预印本，问题没有解决。
- **预印本放在快路径末尾，而不是整条链的末尾**：机构订阅或出版商来源往往能拿到正式版（例如 Elsevier API、Science 浏览器路径），放在快路径末尾会让预印本抢在这些来源前面。代价是只有预印本可用的论文，要等整条链走完才拿到结果。
- **只删掉那 43 个 DOI 的缓存**：要改生产数据库，而且以后发现的同类结果还得再删。整体加缓存版本更简单，代价是已缓存的论文各重新下载一次。

## 后果

- 正式版和预印本都公开时，现在拿到正式版。上面三个例子里，PLOS 那篇改由 PMC 或出版商取得，Nature 那篇改由 PMC Cloud 取得。
- 只有预印本可用的论文，结果会慢一些，并带上述警告。
- 其他来源（如 CORE、OpenAIRE）自己找到的预印本，不经过这条规则，仍可能被直接采用。
- 正文缓存全部失效一次。

## 验证要点

已验证：

- 单元测试：预印本 DOI 的判断（含冷泉港期刊前缀）；Unpaywall 和 OpenAlex 候选的版本标记与排序；正式版优先于更早出现的预印本；预印本在出版商来源之后才被尝试并带警告；请求预印本 DOI 时直接使用。

部署后验证：

- 上面三个 DOI 重新获取：PLOS 和 Nature 那两篇拿到正式版；Cell 那篇如果正式版拿不到，则拿到预印本并带警告。
