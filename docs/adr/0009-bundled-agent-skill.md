# ADR 0009：agent skill 随包发布

- 状态：Accepted
- 实施情况：已实现。单元测试覆盖安装命令、`path` 字段和 `unsupported` 的说明文字；wheel 里确认包含 skill 文件。

## 背景

agent 是这个服务的主要使用者。2026-11-18 至 19 日，一台机器上有 25 个 agent 会话通过 MCP 使用了本服务，共 83 次提交、185 个 DOI。检查这些会话的记录，发现几类反复出现的用法问题：

| 现象 | 数据 |
|---|---|
| 反复查询任务状态 | 86 个任务查询 602 次，单个任务最多 26 次；响应累计约 130 万字符 |
| 一篇论文提交一个任务 | 83 次提交里 30 次只有 1 篇；多个任务排队，总耗时中位 141 秒、P90 714 秒 |
| 用标题或网址当标识符 | 13 篇失败里 5 篇是 `unsupported`，结果里没有说明文字 |
| 手工下载一次性链接 | 42 个链接用 curl 取回 |
| 自己从 PDF 抽文字 | 2 次，服务端已经给了 Markdown |

命令行客户端（[ADR 0008](0008-client-first-packaging.md)）能解决前几项，但 agent 需要被告知怎样用它。

## 决策

1. **skill 放在包里**：`src/academic_source/skills/academic-source/`，随 wheel 发布。
2. **`asource setup skill` 负责安装**：复制到 `--skill-dir` 指定的目录，默认是 `$DSH_AGENTS_HOME/skills`，没设这个变量时为 `~/.agents/skills`。目标下的 `academic-source/` 整个替换。
3. **skill 分两个文件**：
   - `SKILL.md`：用法，以及上表各项问题对应的正确做法。agent 加载 skill 时读到的就是它。
   - `references/schema.md`：输出 JSON 的字段和现成的 jq、Python 查询，需要解析输出时才读。
4. **输出里给出本机路径**：`get`、`batch`、`download` 把文件存到本机后，在 JSON 的每个产物上加 `path` 字段。
5. **`unsupported` 失败带说明**：标识符不是 DOI 或 arXiv 号、按标题也找不到 DOI 时，返回 `message` 和 `action=provide_doi_or_arxiv_id`。

## 理由与替代方案

- **skill 放在单独的仓库或用户的全局目录里手工维护**：命令行改了以后 skill 容易过时。放在包里，两者在同一个提交里改，安装的 skill 与安装的命令行版本一致。
- **把常见错误单独写成一个文件**：agent 只在觉得需要时才去读附加文件，而这些错误恰恰发生在它觉得自己会用的时候。所以正确做法直接写在 `SKILL.md` 的用法旁边。
- **安装时保留用户对副本的修改**：需要记录哪些文件由安装命令管理，并处理冲突。这份 skill 只有两个文件，源文件在仓库里，整个替换更简单。
- **让 agent 按 `<产物 id>-<文件名>` 自己拼本机路径**：要求每个 agent 记住命名规则。输出里直接给 `path`，一条 jq 就能取到。

## 后果

- 升级 `asource` 后要重跑 `asource setup skill`，安装的副本才会更新。
- 在安装出来的副本里做的改动，下次安装时丢失。
- `unsupported` 失败现在带 `message`，旧调用方多收到一个字段，不影响已有字段。
- 单线程排队造成的等待，skill 只能靠「一次提交全部论文」来缓解，根治要等并行调度（[ADR 0004](0004-paper-level-scheduling.md)）。

## 验证要点

已验证：

- `asource setup skill --skill-dir DIR` 生成 `DIR/academic-source/SKILL.md` 和 `references/schema.md`，并清掉目录里原有的文件。
- 下载后输出 JSON 的顶层产物和每篇论文的产物都有 `path`，指向实际保存的文件。
- 提交一个找不到 DOI 的标题，结果为 `reason=unsupported`，带 `action` 和说明文字。

未验证：

- agent 按这份 skill 工作后，上表各项问题是否减少。需要在实际使用一段时间后再检查会话记录。
