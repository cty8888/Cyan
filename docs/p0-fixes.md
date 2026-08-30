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

---

## 8. 压缩到保留段后无法再瘦，超窗直接 FATAL

**现象**：切点改成按 Assistant 之后，单次任务理论上能压。但 `keep_recent_turns = 2` 是硬下限：`find_keep_from` 必须「Assistant 条数严格大于 2」才有切点。两轮大文件读取就能顶满 DeepSeek 常见的 64k 窗口；此时体积够大、切点为 `None`，`needs_compact` 恒为 False。默认窗口还写死 256k，触发线约 227k，压缩永远轮不到。API 一拒就是 `BadRequestError` → `FATAL_ERROR`，没有压完再打。

**原因**：

- 切点失败时体积判断根本轮不到，保留段本身就能超过真实窗口。
- `max_context_tokens = 256000` 和 `deepseek-chat` 的常见上限对不上。
- `chars/4` 粗估会偏低（中文更甚）；估错之后没有「超窗 → 压缩 → 重试」。

**修复**：

- `resolve_keep_from` 优先保留 `keep_recent_turns` 轮，切不到或仍超窗时降到 1 轮，再不行把 system 之后全部收成摘要，只留下当前用户任务。
- Loop 出门前最多连压 `keep + 1` 次，避免「摘要 + 最近两轮」仍然撑满。
- 默认窗口改为 64k，对齐 `deepseek-chat` 常见上限。
- 厂商把超窗标成 400，识别为 `LLMContextOverflowError`：先做紧急压缩（`max_keep=0`）再重试一次；没有可压历史才 FATAL。
- 摘要请求按窗口均分工具正文，降低总结那次自己先超窗的概率。

**改动**：`src/coding_agent/session/compact.py`、`src/coding_agent/core/loop.py`、`src/coding_agent/core/runtime.py`、`src/coding_agent/settings/compact.py`、`src/coding_agent/llm/deepseek.py`、`src/coding_agent/errors.py`、`src/coding_agent/cli/commands.py`

**测试**：`tests/test_compact.py` — `test_fallback_compacts_two_assistant_rounds`、`test_emergency_cut_drops_all_assistant_rounds`、`test_needs_compact_when_only_two_assistant_rounds`、`test_loop_compacts_two_rounds_in_one_task`、`test_loop_retries_after_context_overflow`；`tests/test_deepseek_errors.py`

---

## 9. bash 绕开路径沙箱、敏感文件和「始终允许」粒度

**现象**：README 写的三道防线——路径必须在工作区内、不能自动改 `.git/`、`.env` 每次确认、改已有文件必须先读——全部挂在 `write_file` / `edit_file` 上。模型改走 `bash` 做同一件事，判定链只看命令正则，不看碰了哪条路径。`echo SECRET >> .env`、`echo hacked > .git/config`、`cat ~/.ssh/id_rsa`、`cd /tmp && echo x > a` 都能跑。Plan 模式把 `cat` / `printenv` 当只读放行，API Key 和区外文件会进上下文。对 `git status` 选「始终允许」得到 `exec:git`，随后 `git commit` 不再问。

**原因**：`PermissionManager` 对 EXEC 只跑 `blocked_command` / `restricted_command` / `sensitive_command`。`restricted_path` / `sensitive_path` / `resolve_path` 只在 `ToolCapability.WRITE` 上生效。白名单按第一个 token 记键，`echo` / `python` / `git` 一次放行整类命令。cwd 越界只在命令结束后拉回，写入已经发生。

**修复**：

- 新增 `command_paths.py`：从命令里抽出能看清的路径（重定向、`cat` / `echo` / `cd` / `cp` 等），相对 bash cwd 解析后套同一套区外拒绝、Restricted、Sensitive。
- `cd /tmp && echo x > a` 按段跟踪 cwd，后续相对路径也能看到越界。
- Plan 下只读命令若读 `.env` 或 `printenv`，改为强制确认；区外仍直接拒绝。
- `python -c`、命令替换、`$VAR` 重定向等解析不到的标成不透明：每次确认，且不能「始终允许」。
- 白名单执行头改为 `git status` 而不是整条 `git`；`echo` / `python` / `env` / `bash` / `sh` / `tee` 等过宽命令头禁止写入「始终允许」。
- `bash.run()` 对区外路径和写 `.git/` 再拦一遍。

**改动**：`src/coding_agent/security/command_paths.py`、`src/coding_agent/security/permissions.py`、`src/coding_agent/security/allowlist.py`、`src/coding_agent/security/shell.py`、`src/coding_agent/security/paths.py`、`src/coding_agent/tools/builtin/bash.py`、`src/coding_agent/cli/renderer.py`

**测试**：`tests/test_command_paths.py`；`tests/test_permissions.py` — `test_bash_write_env_is_forced`、`test_bash_write_git_dir_is_restricted`、`test_bash_read_outside_is_denied`、`test_git_status_whitelist_does_not_cover_commit`；`tests/test_bash.py` — `test_leaving_workspace_is_rejected`、`test_redirect_outside_is_rejected`、`test_write_git_dir_is_rejected`

---

## 10. finish_reason=length 被当成任务完成

**现象**：模型打到补全上限时厂商返回 `finish_reason=length`。Loop 只看「有没有 tool_calls」，没有就 `COMPLETED`。用户看到半截总结，会话当成功结束。

**原因**：`parse_completion` 读了 `finish_reason`，`AgentLoop` 从未看它。

**修复**：无 tool_calls 且 `finish_reason` 为 `length` / `max_tokens` 时，回喂一条「输出被截断，请继续」的用户消息再跑一轮；连续截断达到失败上限则按 `MAX_ITERATIONS` 停。带 tool_calls 的截断仍执行工具。

**改动**：`src/coding_agent/core/loop.py`、`src/coding_agent/core/prompts.py`

**测试**：`tests/test_loop.py` — `test_truncated_reply_continues_instead_of_completing`、`test_repeated_truncation_stops_task`、`test_truncated_reply_with_tool_calls_still_runs`

---

## 11. 首条用户消息超窗无法紧急压缩

**现象**：第一句贴进大段代码，还没有 Assistant。`find_keep_from(keep<=0)` 没有切点，emergency compact 失败，直接 FATAL。即便压成了，把原文整段插回窗口还是满的。

**原因**：紧急切点只认 Assistant；摘要请求和写回都不截用户正文。

**修复**：没有 Assistant 但用户正文超过 `DEFAULT_TOOL_RESULT_CHARS` 时，紧急切点切在末尾。摘要请求按同一上限截用户/助手正文；写回保留段时超长 User 只留开头。短消息仍不压，避免误伤。

**改动**：`src/coding_agent/session/compact.py`

**测试**：`tests/test_compact.py` — `test_emergency_cut_without_assistant_when_user_is_huge`、`test_loop_retries_after_overflow_on_huge_first_user`

---

## 12. read_file 读密钥文件自动放行

**现象**：`READ` 在敏感路径检查之前直接 `allow()`。`read_file .env` / `id_rsa` 不询问；`bash cat .env` 却要强制确认。

**原因**：判定链把「只读」当成一律安全。

**修复**：`read_file` / `list_dir` 命中 `sensitive_path` 时 `force=True` 确认，Plan / Bypass 也不能跳过。普通源码读取仍自动放行。

**改动**：`src/coding_agent/security/permissions.py`

**测试**：`tests/test_permissions.py` — `test_read_env_is_forced_in_plan`、`test_read_env_is_forced_in_bypass`、`test_read_id_rsa_is_forced`

---

## 13. 通配 / 递归搜索绕过路径分析

**现象**：Plan 下 `grep -r`、`rg`、`cat *` 算只读放行。解析器只看字面路径，`.env` 和私钥会被灌进上下文。

**原因**：`command_paths` 抽不出通配展开后的目标；`rg` 默认递归。

**修复**：未加引号的 `*` / `?` / `[]`、带 `-r` 的 grep、以及 `rg` / `ag` 标成无界读取，强制确认且不能「始终允许」。引号里的 `*`（如 `echo '2 * 3'`）不算。

**改动**：`src/coding_agent/security/command_paths.py`、`src/coding_agent/security/messages.py`

**测试**：`tests/test_command_paths.py` — `test_recursive_grep_is_unbounded`、`test_rg_is_unbounded`、`test_glob_cat_is_unbounded`；`tests/test_permissions.py` — `test_plan_grep_recursive_is_forced`、`test_plan_rg_is_forced`、`test_plan_cat_glob_is_forced`

---

## 14. bash 子进程继承宿主 API Key

**现象**：`run_process` 在 `env is None` 时把完整 `os.environ` 传给子进程。审批过的脚本能 `printenv DEEPSEEK_API_KEY`。

**原因**：子进程默认继承宿主环境，没有剥离调模型用的密钥。

**修复**：每条子进程都走 `build_subprocess_env()`：复制当前环境，去掉 `DEEPSEEK_API_KEY` 以及名字以 `_API_KEY` / `_ACCESS_TOKEN` 结尾的变量。

**改动**：`src/coding_agent/tools/process.py`

**测试**：`tests/test_process.py` — `test_api_key_env_names_are_secret`、`test_subprocess_env_drops_api_key`、`test_subprocess_does_not_inherit_api_key`
