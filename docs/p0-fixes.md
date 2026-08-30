# P0 修复记录（2026-08-30）

会在真实编码任务里破坏会话或误杀任务的逻辑问题。分层和事件流骨架未改。

---

## 1. 同批 tool_call 提前终止时不补齐回复

**现象**：模型一轮里发起多条工具调用。中途因「连续失败」或「重复调用」停任务时，同批后面还没执行的 call 没有对应的 tool 消息。交互模式会话不重置，下一句用户输入会带着残缺配对打 API，典型结果是请求失败。

**原因**：OpenAI 兼容协议要求：一条 assistant 里有几条 `tool_call`，后面就必须有几条对应的 tool 回复。Ctrl-C 路径会给未执行的 call 补一条失败结果；`TOOL_FAILURES` / `REPEATED_CALLS` 却直接 `return`，不补。

**修复**：任何 `stop_reason` 离开 `_run_tool_calls` 之前，都按 Ctrl-C 同样给未响应的 call 补一条「任务已终止，该工具调用未执行」。

**改动**：`src/coding_agent/core/loop.py`（`_respond_unanswered`）

**测试**：`tests/test_loop.py` — `test_early_stop_pairs_remaining_tool_calls`、`test_repeated_calls_pair_remaining_in_batch`

---

## 2. 重复调用检测误杀正常编码流程

**现象**：同一文件读三次、同一条 `pytest` 跑三次、交替读 A/B 再读 A，任务可能被 `REPEATED_CALLS` 掐死。保险丝本意是拦住空转，却把常见工作流当成死循环。

**原因**：

- 注释写「连续相同」，实现却是在长度为 8 的窗口里**数次数**。`A, B, A, B, A` 也会把 A 判成 3 次。
- 只有「成功改文件且带有效 diff」才清空计数。`read_file` / `list_dir` / 成功的 `bash` 都不算进展。

**修复**：

- 只统计**连续**相同的「工具 + 参数」；中间换成另一次调用，从 1 重新计。
- 任意一次工具**成功**（读到、命令成功、写入成功）都清空计数。
- 仍会拦住：同一失败动作连做 3 次、中间没有任何成功进展。

**改动**：`src/coding_agent/session/types.py`、`src/coding_agent/session/session.py`、`src/coding_agent/core/loop.py`

**测试**：`tests/test_session.py`、`tests/test_loop.py` — `test_successful_reread_does_not_count_as_repeat`、`test_alternating_failed_reads_are_not_repeated_calls`

---

## 3. 读文件：模型看见的和系统认定的「已读」都不对

两件独立的事叠在一起。

### 3.1 工具声称读完，组窗却再截一刀

**现象**：`read_file` 头上写「共 N 行，当前展示 1–N」，模型实际只收到前半段。按没看见的后半段去做 `edit_file` 会对不上，或按残缺印象 `write_file` 整文件覆盖。

**原因**：结果被截两次，尺子不一样长。

1. 工具里截（原 6 万字符），截完写入 `tool_history`。
2. 组窗再截（3 万字符），只放进发给模型的请求，不写回 Session。

工具按 6 万判断「读完了」，模型按 3 万决定「能看见多少」。

**修复**：两层默认都用 `DEFAULT_TOOL_RESULT_CHARS`（3 万）。对当前内置工具，第二层通常不再少给；它仍是组窗安全带（新工具忘了自己截、或以后只改组窗上限时还能兜住）。`tool_history` 存的仍是第一层之后的正文。

**改动**：`src/coding_agent/settings/tools.py`、`src/coding_agent/context/types.py`

### 3.2 只读了开头，就被允许整份覆盖

**现象**：模型 `limit=10` 只读了前 10 行，系统打上「本会话已读过」，随后 `write_file` / `edit_file` 放行，没见过的后半段被覆盖。

**原因**：「已读」通行证发得太松——只要这一次读取请求自己成功结束，就算读过。分段读成功 ≠ 见过全文。

**修复**：只有一次就把文件从头到尾读完，才 `mark_read`。分段 `limit`、字符预算截断、从中间读，都不发证。

**改动**：`src/coding_agent/tools/builtin/read_file.py`

**测试**：`tests/test_read_file.py`、`tests/test_context.py` — `test_read_budget_does_not_exceed_context_truncation`

---

## 4. 压缩看上一轮尺寸，不看即将发出的整包

**现象**：第一轮上下文还很小，模型读了几个大文件、跑了几条输出很长的命令。这些结果已经进 Session，下一轮马上要再问模型。系统仍觉得「上一轮不大，不用压」，带着刚膨胀的上下文打 API。窗口一超就是 `FATAL_ERROR`，压缩根本没机会跑。

**原因**：`needs_compact` 主判断是 `last_prompt_tokens`（上一轮任务请求的 `usage.prompt_tokens`）。工具结果是问完模型之后才写入的，所以「上一问有多大」和「这一问马上要发出去有多大」不是同一个数。`last_prompt_tokens == 0` 时才会 `estimate_session_tokens`；一旦有过上一轮数字，估算被跳过，整体被忽略。

**修复**：

- 主判断改为当前整体：Loop 用量过组窗的 wire（`ContextBuilder.build_messages`）做 JSON 字符数 / 4 粗估，超阈值就压。
- `last_prompt_tokens` 只作补充：上一轮 API 回报已经超阈值，这轮出门前也先压。
- 直接调用 `needs_compact`、未传入估算值时，仍用 Session 粗估兜底。

**改动**：`src/coding_agent/session/compact.py`、`src/coding_agent/core/runtime.py`

**测试**：`tests/test_compact.py` — `test_needs_compact_when_tools_grew_session`、`test_needs_compact_prefers_outgoing_wire_estimate`、`test_loop_compacts_when_session_grew_after_last_call`

---

## 5. Ctrl-C 落在 AssistantReply 时不补齐 tool 回复

**现象**：模型先说一句话，再带上 `tool_calls`。Loop 把这条 assistant 写入会话并 `yield AssistantReply` 给界面显示，此时工具还没跑。用户按 Ctrl-C，任务结束，会话里留下「下了单没回执」的 tool_call。交互模式不重置，下一句用户输入带着残缺配对打 API，典型结果是请求失败。审批面板里的 Ctrl-C 还会被吃成「拒绝这一次」，任务继续跑。

**原因**：OpenAI 兼容协议要求每条 `tool_call` 都有对应 tool 回复。`_run_tool_calls` 里提前终止 / 其中的 Ctrl-C 会补齐；中断若落在更前面的 `yield AssistantReply`（或 `_run_tool_calls` 还没进），最外层直接 `_finish(USER_ABORT)`，不走 `_respond_unanswered`。CLI 在 `stream.send()` 上接到中断时也曾只 `break`、不把中断 throw 回 generator。

**修复**：

- `_finish` 离开前检查最近一条带 `tool_calls` 的 assistant，尚未回复的 call 补一条失败结果。
- CLI 消费事件流时，`send()` 上的 Ctrl-C 也走 `_abort`，把中断抛回 Loop。
- 审批时 Ctrl-C 不再当成 DENY，向上抛出以中断整次任务；EOF 仍视为拒绝本次。

**改动**：`src/coding_agent/core/loop.py`（`_pair_pending_tool_calls`）、`src/coding_agent/cli/app.py`、`src/coding_agent/cli/renderer.py`

**测试**：`tests/test_loop.py` — `test_interrupt_after_assistant_reply_pairs_tool_calls`

---

## 6. 压缩切点按用户问题数，单次任务压不到

**现象**：一次任务里用户只说一句话，后面全是模型读文件、跑命令。对话体积会涨，但 `needs_compact` 一直是 False，压缩不跑。交互里连问两句也一样；要第三句用户输入才第一次有可压区间。组窗只截单条 30k，超窗后 `LLMError` 直接 `FATAL_ERROR`。架构上对话变瘦只靠 compact，没有滑动窗口，切点错了就等于没有上下文管理。

**原因**：`find_keep_from` 按真正的 `UserMessage` 条数切，默认 `keep_recent_turns = 2`，且必须**严格多于 2 条**才认为有东西可压。体积判断排在切点后面，切点失败时根本轮不到。摘要请求还带着被压段的工具全文，即便切点改对了，总结那次自己也能先超窗。

**修复**：

- 切点改为倒数第 `keep_recent_turns` 条 `AssistantMessage`（紧前若是 User 则一并保留，避免拆开「用户问题 + 第一轮回复」）。同一条用户任务里模型轮次多于保留数，就能压掉更早的工具轮。
- 当前问题还没有 assistant 回复时，退回按用户任务切，多轮交互行为不变。
- 切在当前任务内部时，用户那句话会进摘要请求，写回时再插回保留段，避免丢掉当前任务原文。
- 摘要请求的工具正文按 `DEFAULT_TOOL_RESULT_CHARS` 截尾，与组窗同一把尺子。

**改动**：`src/coding_agent/session/compact.py`、`src/coding_agent/settings/compact.py`

**测试**：`tests/test_compact.py` — `test_find_keep_from_cuts_early_rounds_in_one_task`、`test_compact_single_task_keeps_user_and_recent_rounds`、`test_compact_request_truncates_tool_text`、`test_needs_compact_single_task_when_tools_grow`、`test_loop_compacts_during_single_task`

---

## 7. 命令非零退出被当成工具失败，红灯测试掐死任务

**现象**：模型连跑三条失败的 `pytest` / 探测命令，任务被 `TOOL_FAILURES` 终止。用户拒绝、参数解析失败也算在同一条连续失败计数里；bash 把 `exit_code != 0` 写成 `ok=False`，保险丝把「命令红了」和「工具没跑成」绑在一起。

**原因**：Claude Code 的 Bash 是「工具跑完了就算成功，exit code 写在正文里给模型看」。这里 `bash.run` 在超时或非零退出时返回 `ToolRunResult(ok=False)`，`Session.finish_tool_execution` 据此累加 `consecutive_tool_failures`。默认上限 3，同批三条失败命令一轮就会停。

**修复**：

- 非零退出仍 `ok=True`，退出码留在正文和 `metadata.exit_code` 里给模型看。
- 超时（进程被杀掉、工具没有正常跑完）仍算失败。
- 连续失败只统计「工具没跑成」：读文件不存在、参数非法、权限拒绝、超时等。红灯测试不再清保险丝，也不再误杀任务。
- CLI 仍按退出码显示红叉，避免界面看起来像命令成功了。

**改动**：`src/coding_agent/tools/builtin/bash.py`、`src/coding_agent/cli/renderer.py`

**测试**：`tests/test_bash.py` — `test_nonzero_exit_is_still_ok`；`tests/test_loop.py` — `test_failing_commands_do_not_trip_tool_failures`
