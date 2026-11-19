# 架构决策记录（ADR）

这里记录各项设计为什么这样定。系统现在是什么样，见[架构文档](../ARCHITECTURE.md)。

## 什么时候写

改动架构边界、接口或数据契约、用户能观察到的行为（包括策略和默认值）时，在同一个变更里写一条。修正文案、修复不符合已有决策的缺陷、不改变行为的重构，不需要写。

## 怎么写

1. 复制[模板](TEMPLATE.md)，文件名为 `NNNN-kebab-case.md`，编号取索引里的下一个四位数。标题为 `ADR NNNN：决策名称`。
2. 「状态」取下面四个值之一：

   | 状态 | 含义 |
   | --- | --- |
   | Proposed | 已提出，等待确认 |
   | Accepted | 已确认采纳 |
   | Deprecated | 不再适用，也没有替代 |
   | Superseded | 被另一条 ADR 取代 |

3. 「实施情况」单独写：代码是否已实现，做过哪些验证。一条决策可以已采纳但尚未实现，也可以已实现但仍在等确认。
4. 替用户选的默认值如果还没得到确认，在正文里标「待确认」。确认后改成确定的写法。
5. 取代旧决策时保留旧文件，把它的状态改为 Superseded，新旧两条互相链接。
6. 把新记录加进下面的索引。

## 索引

| ADR | 状态 | 实施情况 |
| --- | --- | --- |
| [0001：按需移植上游站点知识](0001-manual-upstream-porting.md) | Accepted | 持续执行 |
| [0002：Provider 归属与重写规则](0002-provider-ownership.md) | Accepted | 见记录 |
| [0003：来源边界使用封闭错误类型](0003-closed-source-errors.md) | Accepted | 见记录 |
| [0004：以论文为单位并行调度](0004-paper-level-scheduling.md) | Proposed | 未实现 |
| [0005：逐来源诊断留在日志和任务记录里](0005-acquisition-observability.md) | Accepted，含一项待确认 | 已实现 |
| [0006：已知非开放获取时跳过纯 OA 来源](0006-skip-oa-only-sources.md) | Proposed | 已实现 |
| [0007：取消任务时做完当前这篇再停](0007-job-cancellation.md) | Accepted | 已实现 |
| [0008：默认安装是命令行客户端，服务端是可选项](0008-client-first-packaging.md) | Accepted | 已实现 |
| [0009：agent skill 随包发布](0009-bundled-agent-skill.md) | Accepted | 已实现 |
