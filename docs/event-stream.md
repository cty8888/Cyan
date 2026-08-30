# 事件流：当前设计（先不要改）

> 记录日期：2026-08-29。这是对**现状**的说明，不是改造清单。
> 先把这套协议吃透再动；现在改形态会把「内核零 I/O + 审批卡循环」搅乱，影响对 Agent Loop 的理解。

相关代码：[`src/cyan/core/types.py`](../src/cyan/core/types.py)、[`src/cyan/core/loop.py`](../src/cyan/core/loop.py)、[`src/cyan/cli/app.py`](../src/cyan/cli/app.py)。

## 现在用的是什么

不是第三方事件总线，不是 asyncio，不是观察者模式。

内核是 **同步 Python generator**：

```python
AgentStream = Generator[AgentEvent, ApprovalDecision | None, None]
```

- 向外 `yield` 的是自建 dataclass `AgentEvent`
- 外界 `send()` 回去的只能是 `ApprovalDecision` 或 `None`
- 循环结束靠生成器 `return` / `StopIteration`，结束原因在最后一条 `TaskFinished.reason`

`Runtime.run(task)` → `AgentLoop.run()` 是这条流的唯一入口。CLI 用 `stream.send(reply)` 拉下一条；测试里的 `drive()` 走同一协议。内核自己不 `print`、不 `input()`。

选生成器而不是 EventEmitter 的原因（和 Claude Code 同一类）：消费者调用 `.next()` / `send()` 才往下跑，天然背压；结束原因是类型化的返回值，不用再订一个 `"end"` 事件。

## 两条轨道，不要混

| 轨道 | 类型 | 给谁看 | 存哪 |
| --- | --- | --- | --- |
| 对话 | `Message` + `Block` | 模型 | `session.messages` |
| 工具事实 | `ToolHistory` / `ToolExecution` | 压缩与展示策略 | `session.tool_history` |
| UI 事件 | `AgentEvent` | CLI / 测试桩 | **不落盘**，只在生成器里活一次 |

发给模型的内容和发给终端的内容是分开的。`AssistantReply` 不是 `AssistantMessage` 的别名；`ToolStarted` 也不是 API 里的 `tool_calls`。CLI 用 `isinstance` 把事件翻译成 rich 输出。

## 八种事件

| 事件 | 何时 yield | `send` 回去什么 |
| --- | --- | --- |
| `TaskStarted` | 任务开头 | `None` |
| `Thinking` | 每一轮 `call_llm` 之前 | `None` |
| `AssistantReply` | 模型有可见文本（不含 tool_call） | `None` |
| `ApprovalRequired` | 权限层要人确认 | **`ApprovalDecision`（唯一双向点）** |
| `ToolStarted` | 真正执行工具前 | `None` |
| `ToolFinished` | 执行结束（含 duration） | `None` |
| `Notice` | 重试、拒绝、重复调用等提示 | `None` |
| `TaskFinished` | 循环结束 | `None`（之后生成器停） |

审批在 Loop 里长这样：

```python
decision = yield ApprovalRequired(request=outcome.request)
```

CLI 碰到 `ApprovalRequired` 时调用 `ask_approval()`，把 `y` / `n` / `a` 变成 `ApprovalDecision`，下一轮 `send` 进去。其余事件 `_render` 返回 `None`，只是把生成器往前推。

中断：CLI `stream.throw(KeyboardInterrupt())`，Loop 给尚未响应的 tool_call 补一条失败的 `ToolMessage`，保证上下文完整。

## 当前限制（知道即可，先不修）

- 同步阻塞：`call_llm` 和工具执行会卡住整个生成器，没有 token 级增量。
- 单消费者：同一时刻只有 CLI（或测试）在拉流；日志是另写文件，不是事件的第二路订阅。
- 事件不可回放：dataclass 不序列化。
- 分发靠 if/elif：`App._render` 加新事件就要改 CLI。

这些是实现偏朴素，不是抽象选错。

## 和 Claude Code 的对照（后面若改，从这里看）

Claude Code 公开形态：TypeScript **异步生成器** `query()`；Agent SDK 是 `async for message in query(...)`；CLI `--output-format stream-json` 是同一条流的 NDJSON。REPL / SDK / 子 Agent / `claude -p` 都走这一条。

同构的部分：都是「生成器当 Agent Loop 对外接口」，不是总线、不是队列。

不同的部分：

| | 本项目（现状，保持） | Claude Code |
| --- | --- | --- |
| 同步性 | 同步 `Generator` | `async function*` / `AsyncIterator` |
| 载荷 | 自建 UI 事件 `AgentEvent` | 会话 `Message`（`assistant` / `user`+`tool_result` / `result`） |
| 审批 | `yield` + `send(ApprovalDecision)` | options 里的 `can_use_tool` 回调，生成器基本单向 |
| 流式 | 整段 `AssistantReply` | 可选 `stream_event` token 增量 |
| 子 Agent | 无 | 内层 `query()` 用 `yield*` 把消息透传上来 |

它把「发给模型的 message」几乎原样 yield 给 UI；TUI 自己从 `tool_use` 画卡片。审批是 await 一个函数，不是往生成器里 send。

若以后要靠拢，也是 **在现有 generator 协议上长**，而不是先换成 EventBus / asyncio 队列：

1. 仍由 Loop yield，不要让工具或 LLM 客户端直接打 UI。
2. 审批可以继续 `send`，也可以改成注入 `can_use_tool`；不要两种同时存在。
3. 若改成 yield 对话 Message，需要想清楚 `Thinking` / `Notice` / `ApprovalRequired` 放哪——它们不是模型协议里的东西。
4. 异步和流式是下一层（`LLMClient` 先能 stream），不是先改事件类型就能得到的。

## 明确不在这次做的

- 不改 `AgentEvent` 集合，不改 `AgentStream` 类型。
- 不把 Loop 改成 async。
- 不引入事件总线、钩子总线、或把审批改成回调。
- 不把 `Message` 和 `AgentEvent` 合成一套。

先把「用户任务 → yield 事件 → 审批 send 回来 → 工具结果进 ToolHistory → 再调模型」这条同步闭环搞熟。改事件形态会动 `loop.py`、`app.py`、全部测试里的 `drive()`，会打断对这一层的理解。
