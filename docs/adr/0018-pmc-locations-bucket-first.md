# ADR 0018：Unpaywall 和 OpenAlex 给出的 PMC 位置先从 PMC Cloud 取

- 状态：Accepted
- 实施情况：已实现。单元测试覆盖桶里有 PDF 和没有 PDF 两种情况，以及同一篇只列一次桶目录。

## 背景

[ADR 0013](0013-pmc-cloud.md) 规定 Unpaywall 和 OpenAlex 给出的 PMC 位置（PMC 或 Europe PMC 的文章页）只换算成 Europe PMC 的 getPdf。PMC 收录的论文因此常在 Unpaywall 这一步就由 getPdf 下载成功，轮不到后面更快的 PMC Cloud 桶。

例如 10.1038/s41467-021-21650-1（6.8 MB）：getPdf 下载约 9–10 s；同一个文件从桶里取，列目录约 1.7 s，下载约 2.5 s。

## 决策

Unpaywall 或 OpenAlex 的候选指向 PMC 位置时，先按其中的 PMCID 列出桶目录：桶里有 PDF 就先下载它，再退回 getPdf。桶里没有 PDF 的（作者手稿）直接用 getPdf。

同一次获取里，每个 PMCID 只列一次桶目录，后面 PMC 这一步复用结果。

## 理由与替代方案

- **把 PMC 这一步整体挪到 Unpaywall 之前**：每篇不在 PMC 的论文都要多一次 PMCID 查询（0.4–2 s），而换来的只是 PMC 论文快几秒。现在的做法只在遇到 PMC 位置时才多一次列目录。

## 后果

- PMC 收录论文的正文更多由 `PMC` 来源取得，下载约快一倍。
- 桶里没有 PDF 的论文，比原来多一次列目录（约 1–2 s）。

## 验证要点

已验证：

- 单元测试：PMC 位置在桶里有 PDF 时由 `PMC` 取得，没有时由 getPdf 取得；两种情况都不再查询后面的元数据。

部署后验证：

- 一篇未缓存、Unpaywall 给出 PMC 位置的论文由 `PMC` 来源取得。
