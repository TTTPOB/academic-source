# ADR 0003：来源边界使用封闭错误类型

- 状态：Accepted
- 实施情况：已实现（提交 `4ea6557`），单元测试覆盖。生产验收结果见完成报告。

## 背景

一次来源尝试失败时，原因此前是一个字符串。字符串的取值没有统一定义，散在各处：

- 借用的来源函数用三种方式报告失败：抛异常；返回带 `error_type`、`reason` 或 `action` 的字典；返回 `None`，原因留在 `_publisher_strategies_core` 的模块级变量里。
- 一篇论文的所有来源都失败后，要选一个原因代表整体结果，并把内部原因映射成对外原因。这段逻辑是 `LegacySources.acquire()` 末尾一个手写的优先级列表加一张字符串映射表。

新增一种原因时，没有任何机制提醒要同时更新优先级和映射。

## 决策

1. **错误是一组封闭的类型**，定义在 `sources/errors.py`：`Paywall`、`Blocked`、`AuthRequired`、`RateLimited`、`ConfigNeeded`、`NetworkError`、`InvalidPDF`、`NotFound`、`BrowserError`、`InternalError`、`UnknownLegacyError`。每种是一个不可变的 dataclass，带自己的结构化字段，例如 `Blocked.kind` 区分 Cloudflare、验证码页和 HTTP 状态码，`RateLimited.retry_at` 是可重试的时间。这是 Rust 的 thiserror 库所代表的风格，用 Python 的 dataclass 联合类型实现。
2. **来源边界用返回值**：一次尝试返回 `SourceSuccess` 或上述某种错误。来源内部可以抛异常，在边界转换一次。
3. **借用来源的三种报告方式只在一处转换**：`sources.legacy.call_legacy()`。那个模块级变量也只在这里读取。
4. **消费方用 `match` 加 `assert_never`**。`present_error()` 为每种错误给出尝试记录里的原因字符串、汇总时的优先级和对外原因。漏掉一种时 pyrefly 报错。
5. **对外 JSON 的 `reason` 字符串全部保持原值。** 客户端和已保存的任务记录依赖它们。
6. **借用来源返回不认识的原因时原样保留**，包在 `UnknownLegacyError` 里。

## 理由与替代方案

- 继续用字符串：无法让类型检查器发现漏处理的情况。
- 让异常直接传到编排层：每个调用点都要重复解释上游的异常种类，返回 `None` 的那种方式也无法用异常表达。
- 把不认识的原因统一改成 `internal_error`：上游新增的原因会被悄悄改写，排查时丢失信息。

## 后果

新增一种错误需要同时写出它的原因字符串和优先级，否则类型检查不通过。

那个模块级变量本身没有变，整个进程仍然只有一份。只在一处读取使问题集中，但没有使它线程安全。这是 [ADR 0004](0004-paper-level-scheduling.md) 把浏览器来源限制在单个线程的原因之一。

## 验证要点

- 三种上游报告方式各有测试，转换后的类型正确。
- 每种错误的原因字符串、汇总优先级和对外原因与引入类型之前一致。
- 引入类型之前写下的任务记录能正常读取。
- `pyrefly check src/academic_source` 无错误。
