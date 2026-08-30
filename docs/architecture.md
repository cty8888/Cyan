# Coding Agent 架构设计与开发规划

从零设计一个不依赖任何 Agent 框架的 CLI 编程智能体：分层模块化架构（LLM 层 / 工具层 / 安全层 / 上下文层 / Agent Loop / CLI 层）。先交付最小可用闭环，架构接口一次性按完整版切好，后续分阶段填充流式渲染、上下文压缩、Memory 与任务规划。

## 1. 设计原则

- 分层解耦：`CLI` 只负责交互与渲染，`core` 只负责 Agent 逻辑，二者通过**事件流**通信，未来换 TUI/Web 不动内核。
- 依赖倒置：`llm`、`tools`、`context`、`security` 全部先定义抽象接口，MVP 给最简实现，后续换实现不改 Agent Loop。
- 工具即插件：新增工具 = 新增一个类 + 一行注册，JSON Schema 由类自动导出。
- 错误不上抛：工具的可预期失败（文件不存在、命令超时、参数非法）统一转成结构化 `ToolResult` 回喂模型，由模型自主恢复；只有不可恢复错误才中断循环。

## 2. 目录结构

```
src/coding_agent/
  __main__.py            # 入口，参数解析
  errors.py              # 异常体系
  logutil.py             # logging 配置（默认只写文件，不抢 rich 界面）
  settings/              # 按职责拆开的运行时设置（CLI 参数 > 环境变量 > 默认值）
    agent.py             # AgentSettings：一次运行的入口（workspace + 各域）
    loader.py            # load_settings()
    llm.py               # LLMSettings：模型、地址、超时与重试
    loop.py              # LoopLimits：轮次 / 失败 / 重复上限
    tools.py             # ToolLimits：输出与读取截断
    cli.py               # CliSettings：日志、权限模式、状态目录
    compact.py           # CompactPolicy：阈值、预留、保留轮数（启动默认值）
  cli/
    app.py               # REPL 主循环、斜杠命令分发
    commands.py          # CommandRegistry：可扩展的斜杠命令
    renderer.py          # rich 渲染与界面文案（模式标签、终止原因、审批面板）
  core/
    types.py             # AgentEvent / StopReason / AgentStream
    runtime.py           # Runtime 组装 LLM / 工具 / 权限 / 上下文
    loop.py              # AgentLoop 驱动任务循环
    tool_executor.py     # ToolExecutor：实际执行工具（预留 hook 点）
    prompts.py           # system prompt
  session/               # Session 数据层（Loop 只通过 Runtime 读写）
    types.py             # 会话字段、工具执行历史
    session.py           # Session 门面
    compact.py           # 对话压缩（区间 → 额外 chat → SummaryMessage）
    workspace_access.py  # 工具能触达的受控工作区视图
  context/
    types.py             # ContextPolicy（装配期工具结果截断）
    builder.py           # ContextBuilder：装配 wire 格式，工具正文按上限截尾
  llm/
    types.py             # Role / Block(...) / Message 继承体系 / LLMResponse
    base.py              # LLMClient 抽象
    deepseek.py          # OpenAI 兼容实现（DeepSeek）
    parser.py            # 模型输出解析：tool_call 参数 JSON 容错
  tools/
    types.py             # ToolCapability / RiskLevel / ToolRunResult / ToolContext
    base.py              # Tool 抽象基类 + 参数校验
    registry.py          # 注册表：schema 导出 + 名称分发 + 执行封装
    diff.py              # write_file / edit_file 共用的 diff 生成
    process.py           # bash 工具共用的子进程执行
    builtin/             # 每个内置工具一个文件：schema 常量 + 行为实现
      list_dir.py
      read_file.py
      write_file.py
      edit_file.py
      bash.py
  security/
    types.py             # PermissionMode / 审批协议 / PermissionOutcome
    permissions.py       # PermissionManager：判定链入口
    messages.py          # 回喂模型的权限文案（Plan 拒绝、用户拒绝等）
    paths.py             # 路径沙箱 + 写目标展示路径
    shell.py             # Plan 模式只读命令判定、执行头提取（白名单共用）
    allowlist.py         # 本会话「始终允许」：write:{目录} / exec:{命令}
    rules.py             # Blocked / Restricted / Sensitive 路径与命令规则
    readonly.py          # 兼容旧导入，转调 shell.py
```

每个领域包对齐同一骨架：`types.py` 放 enum / dataclass；行为按职责单独成文件；共用函数用具体名字（`paths` / `diff` / `process` / `shell`），不设 `utils.py`。`settings/` 本身就是按域拆开的 dataclass，不再套一层 types；`cli/` 没有独立数据契约。

## 3. Agent Loop 与数据流

```mermaid
flowchart TD
    UserInput[用户输入任务] --> Ctx[ContextBuilder 装配消息]
    Ctx --> LLM[LLMClient.chat + tools schema]
    LLM --> Parse[parser 解析文本与 tool_calls]
    Parse --> HasTool{有 tool_calls?}
    HasTool -->|否| Done[输出最终回复, 本轮结束]
    HasTool -->|是| Perm[PermissionManager 判断是否需要审批]
    Perm --> NeedAsk{需要用户确认?}
    NeedAsk -->|否| Exec[Runtime.execute_tool 执行]
    NeedAsk -->|是| Approve[CLI 审批 y/n/a]
    Approve -->|拒绝| Denied[生成 denied 结果回喂模型]
    Approve -->|同意| Exec
    Exec --> Result[ToolRunResult 转 tool 消息]
    Denied --> Result
    Result --> Ctx
```

`runtime.run(task)` 是一个 **generator**，`yield` 事件给 CLI，审批通过 `send()` 回传结果，从而让内核完全不依赖 `input()`。

事件类型、与对话 Message 的双轨划分、以及和 Claude Code（异步 `query()` / 单向 message 流 / `can_use_tool` 回调）的对照，见 [docs/event-stream.md](event-stream.md)。**先不要改这套协议**——改形态会动 Loop / CLI / 测试里的 `drive()`，干扰对当前闭环的理解。

**终止条件**（必须全部实现，缺一会死循环）：

- 模型返回无 `tool_calls` 的完整回复
- 达到 `max_iterations`（默认 30）
- 用户 Ctrl-C 中断
- 连续 N 次工具失败（默认 3）或检测到「同工具 + 同参数」**连续**重复且中间无成功进展

token 预算触发压缩后任务继续，压缩失败不中断循环。

2026-08-30 对循环协议、重复检测、读文件已读标记的修复见 [docs/p0-fixes.md](p0-fixes.md)。

### Message 继承体系 + Block 内容模型 + ToolHistory 事实记录

三个互相独立、各司其职的结构：`Block` 承载「消息里的信息」，`Message` 只是「role + 一组 blocks」的容器，`ToolHistory` 承载「Agent 执行工具的事实记录」——三者不互相继承，`ToolHistory` 也不属于 `Message`。

```
Message (ABC dataclass：role: ClassVar[Role] + blocks: list[Block])
├── SystemMessage      # 只放一个 TextBlock
├── UserMessage        # TextBlock，未来可以混入 FileBlock / CodeBlock
├── SummaryMessage     # 压缩后的区间摘要；to_api 仍是 user
├── AssistantMessage   # TextBlock + 若干 ToolCallBlock，覆写 to_api() 处理 tool_calls
└── ToolMessage        # 只放一个 ToolResultBlock（只有 call id）

Block (ABC：type: ClassVar[BlockType])
├── TextBlock        # 文本
├── ToolCallBlock    # 一次工具调用（id / name / arguments）
├── ToolResultBlock  # 对一次工具调用结果的引用，只有 tool_call_id
├── FileBlock        # 文件引用（path / start_line / end_line），不携带文件内容
└── CodeBlock        # 独立代码片段（language / code）

session.tool_history（与 Message 完全解耦，挂在 Session 上，只负责保存与查询）
├── ToolResult      # content —— 工具输出原文
├── ToolExecution   # id / tool_name / arguments / status / result / started_at / finished_at / duration / error
└── ToolHistory     # dict[call_id, ToolExecution]，record() / get()

context.builder.ContextBuilder（装配层，由 Runtime 持有）
└── build_messages()  # 反查 ToolHistory，取出 content，按 max_tool_result_chars 截尾
```

### Session 与 Runtime

```
Runtime（执行层，不保存长期状态）
 ├── LLMClient
 ├── ContextBuilder / ContextPolicy   # 只装配 messages + tool_history
 ├── CompactPolicy                    # 何时压、留几轮
 ├── ToolRegistry
 ├── ToolExecutor
 ├── PermissionManager
 └── Session（数据层）

Session
 ├── metadata      # id / created_at / updated_at / title
 ├── messages        # list[Message]
 ├── tool_history    # ToolHistory
 ├── state           # current_task / consecutive_tool_failures / last_call_fingerprint / consecutive_identical_calls
 ├── workspace       # root / cwd / opened_files / modified_files
 ├── permissions     # permission_mode / always_allowed（write:{目录} / exec:{命令名}）
 └── usage           # input_tokens / output_tokens / total_tokens / llm_calls / tool_calls

ContextPolicy（装配，不属于 Session）
 └── max_tool_result_chars

CompactPolicy（压缩：默认值在 ``AgentSettings.compact``，App 拷一份注入 Runtime）
 └── max_context_tokens / reserve_tokens / trigger_ratio / keep_recent_turns
```

**运行时策略与斜杠命令（回头再做，先不改代码）**

配置分三类，不要一律 `replace`：

- **启动身份**（workspace、api_key、log）：留在 `AgentSettings` / App，进程内基本不改；换模型要重建客户端。
- **会话状态**（`permission_mode`、`always_allowed`）：继续挂在 Session；`/mode` 已是这个模式。
- **本会话行为策略**（compact / loop / tools / context）：默认值只住 `settings/`，App 拷一份注入 Runtime，Loop 只读 Runtime 上的副本。会话中途用斜杠命令改副本，不写回 `AgentSettings`。

Compact 已经按第三类做了。`LoopLimits` / `ToolLimits` 还在读 `runtime.settings.*`，`ContextPolicy` 还在 `Runtime.create` 里直接 `ContextPolicy()`，回头一起收齐。同时丰富 `/` 命令：除现有 `/compact`、`/mode` 外，补查看与修改阈值、保留轮数、轮次上限、工具结果截断等（改 `runtime.compact_policy` / 将来的 `loop` / `tools` / `context`）。

模型参数（`model` / `temperature`）留在 ``LLMSettings``，不属于 Session。

核心原则：Session 保存 Agent 的「过去和当前状态」，Runtime 负责 Agent 的「下一步行动」。Session 是数据，Runtime 是行为。

关键约束：**`Message` 子类自己不额外开业务字段，也不直接持有工具执行的真实内容**。`ToolMessage` 只知道「这条消息对应哪个 call id」（`ToolResultBlock.tool_call_id`），一次工具调用真正的输出内容、成功与否、执行耗时，属于 Agent 执行工具的事实记录，跟 Session 一起存在于 `tool_history` 里，不属于 Message。对话压缩（[`session/compact.py`](src/coding_agent/session/compact.py)）会把较早的消息区间额外 `chat` 一次收成 `SummaryMessage` 写回列表，并删除该区间对应的 `tool_history` 条目；失败则两者都不改。

`ToolHistory` 只提供 `record()` / `get()` / `remove()`，不承担展示职责。`ToolResult` 只保存 ``content``。发给模型时由 ``ContextBuilder`` 按 ``max_tool_result_chars`` 截尾，不写回 Session。

`Runtime.messages_for_request()` 委托给 `ContextBuilder.build_messages()`——它读取 Session 的 `messages` 和 `tool_history`，把两者拼成 wire 并按上限截工具正文；`LLMClient.chat()` 因此直接接收装配好的 `list[dict]`，不需要认识 `Message` 这个内部类型。

## 4. 工具系统

统一契约（`tools/base.py`）：

```python
class Tool(ABC):
    name: str
    description: str
    capability: ToolCapability  # READ / WRITE / EXEC —— 决定模式怎么分流
    risk: RiskLevel             # MINIMAL … CRITICAL；CRITICAL 强制逐次确认
    parameters: dict            # JSON Schema，直接喂给 tool calling
    def describe(self, args, workspace) -> tuple[str, str | None, str]: ...
    def run(self, ctx: ToolContext, **kwargs) -> ToolRunResult: ...
```

`ToolResult(ok, content, error, metadata)`，`content` 是给模型看的纯文本，`metadata` 给 CLI 渲染用（如 diff）。

MVP 工具集：

- `list_dir`：目录树，支持 depth，自动跳过 `.git`/`node_modules`/`.venv`
- `read_file`：带行号返回，支持 offset/limit，大文件截断并提示
- `write_file`：整文件写入/新建
- `edit_file`：精确字符串替换（old_string 必须唯一，否则报错让模型补上下文）—— 比整文件重写省 token，是 Claude Code 的关键设计
- `bash`：唯一的 shell 执行入口，测试、构建、git、脚本都走它。学习 Claude Code 的 Bash 工具设计：
  - 接口只有 `command`（必填）与 `timeout_ms`（默认 120000）
  - 每条命令都在独立新进程里跑，没有持久 shell；不保留环境变量或别名，`export` 不影响下一次调用
  - 工作目录会在调用之间延续：给命令追加一段 trailer 脚本，执行结束后打印 `$PWD`，
    解析出来后写回会话状态（`Session.bash_cwd`），下一次调用从那里继续；越出工作目录会被拉回工作目录根
  - stdout/stderr 合并成一路输出，超过 `max_tool_output_chars`（默认 30000）时截掉超出的尾部、保留开头，并加 `...[truncated]`
  - system prompt 里写明本机 `sys.executable` 的绝对路径，避免模型在命令里假设存在 `python` 这个命令（很多环境只有 `python3`）
  - 先不做：后台任务、shell 别名加载、环境变量持久化、输出落盘、权限沙箱——等基础跑稳再加

## 5. 安全模型

两轴 + 四级规则，由 [`PermissionManager.evaluate()`](src/coding_agent/security/permissions.py) 一次判定：

**两轴**（写在每个 `Tool` 上，正交）：

| 轴 | 回答的问题 | 参与判定的方式 |
| --- | --- | --- |
| `ToolCapability`（READ / WRITE / EXEC） | 这是读、写还是执行？ | Plan 拒写；AcceptEdits 放行普通写；Bypass 放行普通写/执行 |
| `RiskLevel`（MINIMAL … CRITICAL） | 这次操作有多危险？ | **CRITICAL 强制逐次确认**，不受模式 / 「始终允许」影响；其余级别用于审批面板展示 |

**四级规则**（[`rules.py`](src/coding_agent/security/rules.py)，write 与 exec 统一；工具 `run()` 里对 Blocked / Restricted 再拦一次，防止有人绕过权限层直接 `execute`）：

| 级别 | 处置 | 例子 |
|------|------|------|
| **Blocked 黑名单** | 永远 DENY，连 Bypass 也不能绕过 | `sudo`、`rm -rf /`、`mkfs`、`curl \| sh` |
| **Restricted 强硬限制** | 永远 DENY，不出审批 UI | `git push --force`、写入 `.git/` |
| **Sensitive 敏感** | NEED_APPROVAL + `force=True`，不受「始终允许」/ AcceptEdits / Bypass 影响 | `.env`、私钥、`pip install`、`git push` |
| **Normal 普通** | 由 `PermissionMode` 决定 | 普通源码写入、`pytest` |

**PermissionMode**（Normal 级别）：

| 模式 | 只读 | 普通写入 | 普通执行 |
|------|------|----------|----------|
| Plan | 放行 | DENY | 仅放行只读命令（`git status`、`pytest`、`ls` 等，见 `security/shell.py`） |
| Default | 放行 | 需审批 | 需审批 |
| AcceptEdits | 放行 | 直接放行 | 需审批 |
| Bypass | 放行 | 直接放行 | 直接放行 |

审批选项：`y` 本次允许 / `n` 拒绝 / `a` 本会话始终允许同类操作（仅非 force）。写入按目录前缀（根目录文件记 `write:.`，只放行根下其它文件；`write:pkg` 放行 `pkg/` 及其子目录，`write_file` 与 `edit_file` 共用），执行按命令名（`exec:pytest` 只放行 `pytest …`，不放行 `touch`）。用户拒绝后回喂模型换方案。路径沙箱（`security/paths.py`）独立于这套规则，任何工具读写文件时都生效。

组件职责：

1. **`rules.py`**：Blocked / Restricted / Sensitive 规则表
2. **`shell.py`**：Plan 只读命令判定与执行头提取
3. **`allowlist.py`**：本会话始终允许的范围键
4. **`PermissionManager`**：编排判定链，产出 `PermissionOutcome`
5. **工具 `run()`**：对 Blocked / Restricted 再拦一次（`BlockedCommandError` / `SecurityError`）

## 6. 上下文管理与 Memory

- Token 记账：压缩触发看「即将发出的整包」——组窗后的 wire 用 JSON 字符数 / 4 粗估；上一轮 API 的 `usage.prompt_tokens`（`last_prompt_tokens`）只作补充，那次已经超阈值则这轮出门前先压
- 压缩策略默认值在 ``settings.compact``（``CompactPolicy``），启动时 App 拷一份注入 ``Runtime.compact_policy``。会话中途改 Runtime 上的副本（后续斜杠命令 / 配置）不影响 ``AgentSettings``。超过阈值 `(max_context_tokens - reserve_tokens) * trigger_ratio` 时，在下一轮任务 `call_llm` 之前压；保留最近 `keep_recent_turns` 轮原文。被压缩段另一次不带 tools 的 `chat` 收成 `SummaryMessage`，成功后再删该段 `tool_history`。发给模型的上下文仍只来自 Session 的 messages + tool_history，由 ContextBuilder 装配。REPL `/compact` 走同一入口
- 不做滑动窗口：对话变瘦只靠 compact。组窗仍送出当前全部 messages；``ContextBuilder`` 对每条工具正文按 ``max_tool_result_chars``（默认 30000）截尾，不写回 Session。压缩那次 chat 仍用 history 全文。
- Memory 两层（未做）：项目级 `AGENTS.md` + 会话级持久化与 `--continue`

## 7. 开发排期

MVP（Phase 1）交付后即可端到端跑通「用户任务 → 分析 → 调工具 → 完成」，Phase 2-5 在不改内核接口的前提下增量补齐。

### Phase 1：MVP 闭环（已完成）

- [x] 搭建目录骨架、`settings/`（按域拆分的三级覆盖）、`errors.py` 异常体系
- [x] llm 层：types/base 抽象 + deepseek OpenAI 兼容实现（非流式）+ parser 的 tool_call 参数 JSON 容错解析
- [x] tools 层：Tool 基类 + ToolRunResult + registry 自动导出 schema，实现 `list_dir`/`read_file`/`write_file`/`edit_file`
- [x] execution 工具：`bash`（超时/输出截断/跨调用工作目录延续），对齐 Claude Code 的 Bash 工具设计，取代早期的 `run_command`+`run_code` 双工具方案
- [x] security 层：路径沙箱、命令黑名单 / 强硬限制 / 敏感资源、权限管理与审批协议（y/n/a）、Plan 模式只读命令判定（`shell.py`）
- [x] `core/runtime` Agent Loop（generator + 事件流）、session 状态、全部终止条件与错误恢复策略
- [x] 基础 CLI REPL：消费事件流、处理审批交互，跑通端到端最小闭环
- [x] 提前补做：审批 diff 预览、Ctrl-C 中断、离线测试 `tests/`（pytest）
- [x] 标准库 logging：事件写入 `.coding_agent/logs/agent.log`（默认不打 stderr）

### Phase 2：交互体验

- [ ] 流式输出（`stream=True` + tool_call 分片拼接）
- [x] rich 富渲染：工具卡片、diff 预览、执行输出摘要
- [x] Ctrl-C 中断（中断时补齐未响应的 tool_call，保证上下文完整）
- [ ] 丰富斜杠命令：会话中改 Runtime 上的策略副本（compact / loop / tools / context），不写回 `AgentSettings`；先把 loop/tools/context 收成与 compact 相同的「settings 默认 → App 拷贝注入」

### Phase 3：上下文与记忆

- [x] 上下文 token 预算与历史摘要压缩（改写 Session.messages 为 SummaryMessage，不落盘）
- [ ] Memory：`AGENTS.md` 注入、会话持久化与 `--continue` 恢复

### Phase 4：规划与检索

- [ ] 任务规划工具 `todo_write`
- [ ] `grep` / `glob` 搜索工具

### Phase 5：工程收尾

- [x] 把离线测试改写为按模块拆分的 pytest 用例
- [x] README
- [x] 最小 GitHub Actions：push / PR 跑 pytest
- [ ] 整体打磨

## 8. 依赖

现有 `openai` / `python-dotenv` / `rich` 已足够，开发依赖为 `pytest`。无需引入任何 Agent 框架。
