# 斜杠命令手册

交互 REPL 里以 `/` 开头的都是斜杠命令，其余输入都当作自然语言任务。命令由
[`cli/commands.py`](../src/cyan/cli/commands.py) 的 `build_default_commands()` 统一注册，
`/help` 的输出直接从这份注册表生成，不需要手动同步。

## 一览

| 命令 | 别名 | 用法 | 作用 |
| --- | --- | --- | --- |
| [`/help`](#help) | | `/help` | 显示本帮助 |
| [`/tools`](#tools) | | `/tools [limits\|<字段> <值>]` | 列出已注册工具；查看/修改工具限制 |
| [`/mode`](#mode) | | `/mode <模式>` | 切换权限模式：plan / default / accept_edits |
| [`/permissions`](#permissions) | | `/permissions [allow\|ask\|deny\|remove] <规则>` | 列出或增删权限规则 |
| [`/usage`](#usage) | | `/usage` | 显示本会话的 token 与调用统计 |
| [`/stream`](#stream) | | `/stream [on\|off]` | 查看或切换流式输出 |
| [`/instructions`](#instructions) | | `/instructions` | 列出已加载的指令层（identity + cyan.md + Skills + MEMORY.md） |
| [`/skills`](#skills) | | `/skills [on\|off\|enable\|disable <name>]` | 列出 Skills；本会话开关自动注入；开关单个 |
| [`/skill`](#skill) | | `/skill [<name>\|clear]` | 为下一条任务手动指定/取消强调一个 Skill |
| [`/memory`](#memory) | | `/memory` | 列出项目自动记忆文件 |
| [`/compact`](#compact) | | `/compact [show\|set <字段> <值>]` | 压缩较早对话；查看/修改压缩策略 |
| [`/loop`](#loop) | | `/loop [<字段> <值>]` | 查看或修改循环限制（轮次上限等） |
| [`/context`](#context) | | `/context [<字段> <值>]` | 查看或修改上下文截断策略 |
| [`/model`](#model) | | `/model [<名字>]` | 查看或切换模型 |
| [`/status`](#status) | | `/status` | 一屏汇总模型/权限/流式/上下文/统计 |
| [`/todos`](#todos) | | `/todos [clear]` | 查看当前任务清单（`todo_write` 维护） |
| [`/changes`](#changes) | | `/changes` | 列出本次会话改动过的文件 |
| [`/history`](#history) | | `/history` | 列出用户消息（完整日志） |
| [`/rewind`](#rewind) | | `/rewind <序号或id> [restore\|summarize-up\|summarize-from]` | 回退到某条用户消息 |
| [`/sessions`](#sessions) | | `/sessions` | 列出本工作区已保存的会话 |
| [`/resume`](#resume) | `/continue` | `/resume [<id 或前缀>]` | 切换到另一个会话 |
| [`/new`](#new-clear) | | `/new` | 开始新会话（旧日志保留） |
| [`/clear`](#new-clear) | | `/clear` | 同 `/new` |
| [`/cwd`](#cwd) | | `/cwd` | 显示工作区根目录（沙箱边界，不是 bash 的 `$PWD`） |
| [`/exit`](#exit) | `/quit` | `/exit` | 退出 |

四个"运行时策略"命令（`/compact` `/loop` `/tools` `/context`）都遵循同一个模式：改的是
`Runtime` 持有的策略副本（`compact_policy` / `loop_limits` / `tool_limits` /
`context_policy`），**只影响当前会话**，不写回 `AgentSettings`，进程重启或开新会话后
恢复启动时的默认值。它们的 `<字段> <值>` 语法也完全一致：字段名按 dataclass 声明的
类型自动转换（`bool` 认 `1/true/on/yes`，`int`/`float` 按对应类型解析，其它当字符串），
类型转换失败或字段不存在都会报错且不改动任何值。

输入框敲 `/` 后会自动弹出候选列表（随打字实时过滤，不用按 Tab，方向键选择、
Enter/Tab 确认），出现空格进入参数阶段后不再干扰。由 `cli/completion.py` 的
`SlashCommandCompleter` 实现，数据直接来自命令注册表，新增命令不需要再手动同步。
自然语言任务里的 `@path` 引用见下方，不是斜杠命令。

## `@` 文件引用

不是斜杠命令，而是自然语言任务里可以混用的引用语法：敲 `@` 后同样会弹出工作区内
文件路径的实时候选（`cli/completion.py` 的 `FileReferenceCompleter`），补全或直接手打
`@relative/path.py` 都可以。

提交任务时，`cli/file_refs.py` 的 `extract_file_refs()` 会从任务文本里挑出所有
`@path` 引用，读出当时的文件内容，打包成结构化的 `FileBlock`（不是简单的文本替换）。
这些 `FileBlock` 跟任务文本的 `TextBlock` 一起构成本轮 `UserMessage`：

- 发给模型前，`UserMessage.to_api()` 会把每个 `FileBlock` 展开成
  `[文件 path]` + 代码块，拼在任务文本后面；
- 随事件日志一起持久化（`session/session.py`），resume 时能从磁盘完整重建
  （`session/view.py`）；
- 压缩摘要按渲染后的完整内容（文本 + 文件快照）判断是否超限、如何截断
  （`session/compact.py`），不会因为文件很大就漏判。

引用只在工作区沙箱内、指向真实存在的文件时才生效；不存在、逃出工作区、或看起来
像邮箱地址一类正常文本里的 `@`，会原样当自然语言处理，不报错也不中断输入。单个
文件超过 `/tools` 里的 `max_file_bytes` 上限会被跳过，超过 `max_file_read_chars`
的内容会截尾（截断规则跟 `read_file` 工具一致）。

---

## Skills

跟 `cyan.md` 一样是磁盘上的文件，但**自动叠进 system prompt 这条路径启动时默认关闭**：
会话里用 `/skills on` 打开全部，或 `/skills enable <name>` 只启用某一个，立刻生效、
不必重启。环境变量 `CYAN_ENABLE_SKILLS=1` 只改**启动默认值**。默认关闭是因为 Skill
正文往往不短、数量也可能不少，默认全量塞进 system 对 token 开销不友好。总开关
打开之后，一个 Skill 是一个目录 + 一份 `SKILL.md`：

```
---
name: debugging-methodology
description: 遇到报错、测试失败、运行结果跟预期不一致时使用
---

<正文：给模型看的详细步骤/checklist>
```

两层发现，跟 `cyan.md` 的用户级/项目级完全对齐：

- 个人级：`~/.cyan/skills/<name>/SKILL.md`（跨项目共享的个人偏好，不进 git）
- 项目级：`{workspace}/.cyan/skills/<name>/SKILL.md`（跟这个项目相关的约定，可以进 git）
- 同名冲突时项目级覆盖个人级

发现到的每个 skill，其「触发条件（description）+ 完整正文」会整篇渲成一层塞进
`PromptStack`（跟 `read_file` 那种"先给摘要、按需再读全文"的机制不同：个人级 skill
存在工作区之外，`read_file` 的沙箱本来就读不到它，所以选择直接整篇嵌入，不依赖模型
主动去读）。缺 frontmatter、缺 `name`/`description` 字段的目录会被静默跳过。

总开关打开后是"常驻自动注入"——每一轮请求都带着，模型自己判断用不用。如果想明确
要求"这一次任务请优先照某个 skill 做"，用 `/skill <name>` 手动指定（不受总开关与
per-skill 开关限制，见下）；如果想彻底关掉某个 skill 不让它进 system prompt，用
`/skills disable <name>`。总开关（启动默认 / `/skills on|off`）、per-skill 开关、
`/skill` 手动指定是三个独立的旋钮，详见下方 `/skills` 与 `/skill` 命令说明。

---

## `/help`

```
/help
```

打印本手册对应的一览表（内容直接从命令注册表生成）。

---

## `/tools` {#tools}

```
/tools                    # 列出已注册的工具（名字、能力、说明）
/tools limits             # 查看当前 ToolLimits 副本
/tools <字段> <值>         # 修改 ToolLimits 的某个字段
```

`ToolLimits` 可改字段：

| 字段 | 说明 |
| --- | --- |
| `max_tool_output_chars` | `bash` 等工具回喂模型的输出上限 |
| `max_file_read_chars` | `read_file` 单次读取上限。建议不超过 `/context` 的 `max_tool_result_chars`，否则组窗还会再截一刀 |
| `max_dir_entries` | `list_dir` 树形列表的条目上限 |
| `max_glob_results` | `glob` 按 mtime 返回的文件上限 |
| `max_file_bytes` | `read_file` / `write_file` 单次进内存上限 |
| `max_bash_timeout_ms` | `bash` 的 `timeout_ms` 上限 |
| `max_process_output_chars` | 子进程 stdout 入内存上限 |

示例：`/tools max_bash_timeout_ms 1200000` 把单次 bash 超时上限放宽到 20 分钟。

---

## `/mode`

```
/mode plan
/mode default
/mode accept_edits
```

必须带参数；不带参数只打印用法。当前模式看 `/status`。切换后立即写回 sidecar（`session.persist_head()`），下一次工具调用就按新模式判定：

- `plan`：只读规划，禁止写文件，`bash` 仅放行只读命令
- `default`：写/执行都需要逐次确认
- `accept_edits`：自动批准普通写入与工作区内文件系统命令，执行仍需确认

---

## `/permissions`

```
/permissions                       # 列出当前生效的全部规则
/permissions allow <规则>
/permissions ask <规则>
/permissions deny <规则>
/permissions remove <规则>
```

规则写法（详见 [README「安全模型」](../README.md#安全模型)）：`Bash(pytest *)`、
`Read(.env)`、`Edit(src/**)`、`WebFetch(domain:example.com)`。`allow`/`ask`/`deny`
写入工作区 `.cyan/settings.local.json`；`remove` 可以删 local / 项目 / 用户三层里的规则，
但删不掉内置规则。改完立即 `reload()` 生效，不需要重启。

---

## `/usage`

```
/usage
```

打印本会话累计的模型调用次数、工具调用次数、输入/输出/合计 token，以及历史消息条数
（复用 `session.stats()`）。

---

## `/stream`

```
/stream            # 查看当前是开还是关
/stream on
/stream off
```

直接改 `AgentSettings.llm.stream`（进程级，不是 Runtime 副本）——`DeepSeekClient`
每次调用都读同一个 `LLMSettings` 对象，改完下一次模型调用立刻生效，不需要重建客户端。

---

## `/instructions`

```
/instructions
```

列出当前会叠进 system 的 Prompt Layer（身份 system prompt + `cyan.md` 各层 +
本会话实际注入的 Skills + `MEMORY.md` 索引），显示每层的来源路径、字数，以及是否被截断；不打印全文。被 `/skills disable` 关掉、或总开关关闭且未单独 `enable` 的 skill 不会出现在这里。

---

## `/skills` {#skills}

```
/skills                          # 列出发现的 skill（含是否真正叠进 system）
/skills on                       # 本会话打开自动注入：所有未 disable 的 skill 进 system
/skills off                      # 本会话关掉自动注入（含刚才 enable 的单个）
/skills enable <name>            # 启用某一个：立刻叠进本会话，并写入 skills.json
/skills disable <name>           # 关掉某一个：立刻从本会话移除，并写入 skills.json
```

列出模式下每条显示 name、层级（个人/项目）、**是否真正叠进 system**、description
与来源路径；不打印正文。状态三档：`启用`（本会话会进 prompt，`/instructions` 能看到
对应层）、`未注入`（没被 disable，但总开关关着且没单独 enable）、`已禁用`
（`/skills disable` 单独关掉的）。

`/skills on|off` 只改当前会话的 `PromptStack.skills_enabled`，进程重启后回到启动
默认值（环境变量 `CYAN_ENABLE_SKILLS=1` 则为开，否则为关）。`enable`/`disable`
还会写 `skills.json`，跨会话保留 per-skill 偏好；总开关关闭时 `enable <name>`
仍会把这一个立刻叠进**本会话**（不必先 `/skills on`）。

`disable`/`enable` 只是加/删一个开关标记，不动 `SKILL.md` 本身：开关状态写进跟该
skill 同一层级的 `skills.json`——个人级 skill 写 `~/.cyan/skills.json`，项目级写
`{workspace}/.cyan/skills.json`（内容形如 `{"disabled": ["name1", "name2"]}`）。
关掉之后 `/skills` 列表里仍能看到它（标"已禁用"），方便随时切回来；`/instructions`
和实际发给模型的 system prompt 里则完全看不到它的正文了。项目级 `skills.json` 可以
提交进 git，相当于团队约定"这个项目里不要用某个 skill"。

---

## `/skill` {#skill}

```
/skill                 # 列出可用 skill 名字
/skill <name>          # 手动指定：下一条任务额外强调这个 skill
/skill clear           # 取消已设置但还没被消费的提醒
```

跟自动注入的常驻层是两件独立的事：那边每轮都在、模型自己判断相关不相关；这里是
用户明确要求"这一次请优先照这个 skill 做"。指定后只对**下一条**任务生效一次——
提醒文本会拼进那条任务的 `UserMessage`（对话历史里能看到），发出去就自动清空，
不需要手动 `clear`（`clear` 是给"设置了但改主意不想用了"的场景用的）。

如果目标 skill 当前被 `/skills disable` 关掉了，`/skill <name>` 仍然会生效（手动
指定被当作一次明确的例外），但会额外提示一句"该 skill 当前处于禁用状态"，避免
误以为它一直都在生效。

---

## `/memory`

```
/memory
```

列出 `{workspace}/.cyan/memory/` 下的自动记忆文件及大小。若设置了
`CYAN_DISABLE_AUTO_MEMORY=1`，提示自动记忆已关闭。

---

## `/compact` {#compact}

```
/compact                       # 立即触发一次压缩
/compact show                  # 查看当前 CompactPolicy 副本
/compact set <字段> <值>        # 修改 CompactPolicy 的某个字段
```

`CompactPolicy` 可改字段：

| 字段 | 说明 |
| --- | --- |
| `max_context_tokens` | 触发压缩的窗口上限，须贴近模型实际窗口 |
| `reserve_tokens` | 给总结那次 chat 单独留出的余量 |
| `trigger_ratio` | 触发阈值 = `(max_context_tokens - reserve_tokens) * trigger_ratio` |
| `keep_recent_turns` | 优先保留的最近 Assistant 轮数，超窗时会自动降到 1 轮乃至全部压进摘要 |

不带参数时走原有行为：把较早对话压缩成摘要（`session.compact.resolve_keep_from` 选切点，
消息太少会提示"无需压缩"而不是报错）。压缩失败（比如模型调用异常）不改动会话。

---

## `/loop`

```
/loop                       # 查看当前 LoopLimits 副本
/loop <字段> <值>            # 修改 LoopLimits 的某个字段
```

`LoopLimits` 可改字段：

| 字段 | 说明 |
| --- | --- |
| `max_iterations` | 单次任务最多「模型 ↔ 工具」轮次 |
| `max_consecutive_tool_failures` | 连续失败这么多次就停 |
| `max_repeated_calls` | 同工具同参数连续出现这么多次视为死循环 |

示例：`/loop max_iterations 60` 把单次任务的轮次上限从默认 30 提到 60，适合明知道任务会很长的场景。

---

## `/context`

```
/context                    # 查看当前 ContextPolicy 副本
/context <字段> <值>         # 修改 ContextPolicy 的某个字段
```

目前只有一个字段：`max_tool_result_chars`——发给模型时单条工具结果最长多少字符
（`<= 0` 表示不截断）。默认与 `ToolLimits.max_file_read_chars` 对齐，避免模型看到的
比工具声称读到的还少；改这个不会动 `ToolLimits`，两者需要分别调。

---

## `/model`

```
/model                       # 查看当前模型
/model deepseek-reasoner     # 切换模型
```

直接改 `AgentSettings.llm.model`。`DeepSeekClient.model` 是只读 property，每次调用
实时读这个字段，所以切换立即生效，不需要重建客户端。不做模型名校验——这是目前唯一
在用的后端，改错名字会在下一次调用时由 API 报错。

---

## `/status`

```
/status
```

一屏汇总当前会话状态：

- 模型（`/model` 的当前值）
- 权限模式（`/mode` 的当前值）
- 流式输出开关（`/stream` 的当前值）
- 当前会话 id（前 8 位）与标题
- 上下文占用：`runtime.estimate_request_tokens()` / `compact_policy.max_context_tokens`，带百分比
- 调用统计：模型调用次数、工具调用次数、合计 token（同 `/usage`）

---

## `/todos` {#todos}

```
/todos             # 查看当前任务清单
/todos clear       # 手动清空
```

任务规划清单由模型自己判断何时用 `todo_write` 创建与更新（3 步以上、多文件、需要用户看到
进度的任务），**用户不能手动调用 `todo_write`**——`/todos` 只是只读查看入口（`clear` 是唯一
的写操作，直接清空，不需要经过模型）。清单是覆盖式的：模型每次调用 `todo_write` 都会传入
完整清单，不是增量修改；同一时刻最多一项 `in_progress`。清单跟随会话持久化（checkpoint +
sidecar `meta.json`），`/rewind restore` 分叉新会话时会恢复到锚点当时的清单状态。清单为空时
提示"当前没有任务清单"。

---

## `/changes` {#changes}

```
/changes           # 列出本次会话改动过的文件
```

数据来自 `session.workspace.modified_files`，由 `write_file`/`edit_file` 在成功落盘时标记
（`mark_modified`），路径按工作目录收成相对路径展示。`bash` 里执行 `rm`/重定向等改动不计入这份
清单——工具看不清目标文件到底是什么，宁可不追踪也不乱标。适合任务跑完之后快速确认"这次到底
改了哪些文件"，不需要跳出去手动 `git status`。

---

## `/history`

```
/history
```

按序号列出本会话完整日志里的用户消息（不含 `continue`/摘要），每条附事件 id 前 12 位
与文本预览。配合 `/rewind` 使用：先用 `/history` 找到序号或 id，再 `/rewind` 回退。

---

## `/rewind`

```
/rewind <序号或id>                          # 交互选择 restore / summarize-up / summarize-from
/rewind <序号或id> restore                  # 从该条用户消息分叉出一个新会话
/rewind <序号或id> summarize-up             # 把该条之前的历史压缩成摘要
/rewind <序号或id> summarize-from           # 把该条到末尾的历史压缩成摘要
```

- `restore`：`fork_at_user()` 拷贝锚点及之前的源事件到新 `<id>.jsonl`，父会话文件不改、
  不冻结；**不回滚工作区文件**，只是会话历史分叉。
- `summarize-up` / `summarize-from`：走跟 `/compact` 一样的压缩入口，只是切点由这条用户
  消息决定；如果这条消息已经被之前的压缩隐藏（不在当前上下文视图里），需要先 `restore`
  再压缩。

---

## `/sessions`

```
/sessions
```

列出本工作区（`{workspace}` 对应的 `~/.cyan/projects/<路径编码>/`）下已保存的会话：
当前会话标 `●`，其余标 `○`；`last`（`--continue` 会恢复的那个）额外标注；fork 出来的
会话显示父会话 id 前缀。只列出，不切换——切换用 `/resume`。

---

## `/resume` {#resume}

```
/resume                    # 列出可切换的会话（同 /sessions 的格式）
/resume <id 或前缀>          # 切换到该会话
```

别名 `/continue`。在 REPL 内部直接切会话，不需要退出进程重新用 `--resume <id>` 启动。

- 参数支持完整 id 或本工作区内唯一的前缀（`resolve_session_id()`）；前缀有歧义或匹配不到
  都会报错，不切换。
- 切到当前会话本身是空操作，只提示"已经是当前会话"。
- **权限模式沿用当前会话，不恢复目标会话磁盘上存的那个**——仿照 Claude Code 的
  `/resume` 行为，避免切完一个旧会话后权限模式莫名其妙变了。
- 底层复用 `session.branch.load_session()` + `App.attach_session()`，跟 `/new`、
  `/rewind restore` 是同一套装配逻辑。
- 切换成功后会把目标会话之前的对话整体回放一遍（用户消息、assistant 回复、工具
  调用与结果摘要），跟启动时 `--continue`/`--resume` 的回放是同一套渲染
  （`Renderer.render_transcript()`），先看到上下文再继续输入。

---

## `/new` / `/clear` {#new-clear}

```
/new
/clear
```

两个命令等价：开一个全新会话（新的 `<id>.jsonl`），旧会话的日志原样保留在磁盘上，
可以用 `/sessions` 找回、`/resume` 切回去。

---

## `/cwd`

```
/cwd
```

打印启动时 `-w/--workspace` 指定的工作区根目录（路径沙箱的边界，Agent 只能访问这个目录内的文件）。这不是 bash 工具当前的 `$PWD`：bash 在调用之间会延续 `cd`，越出工作区才会被拉回根；`/cwd` 始终显示工作区根。

---

## `/exit` {#exit}

```
/exit
/quit
```

退出 REPL。
