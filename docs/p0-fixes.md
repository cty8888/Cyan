# P0 修复记录（2026-08-30）

三处会在真实编码任务里破坏会话或误杀任务的逻辑问题。分层和事件流骨架未改。

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
