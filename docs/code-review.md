# Coding Agent 全项目代码评审

> 2026-08-30：循环协议 / 重复检测 / 读文件截断与已读标记，见 [docs/p0-fixes.md](p0-fixes.md)。
>
> 后续结构整理（2026-08-29）：已删除 `constants/` 平行目录树（工具 schema 内联到 `tools/defs/`，权限文案迁入 `security/`）；`security_rules.py` 改名为 `rules.py`，`utils.py` 改名为 `readonly.py`；根目录调试脚本 `test_deepseek.py` / `test_env.py` 已删除。下文仍保留当时的问题记录。
>
> 评审范围：`src/coding_agent` 全部源码、`docs/architecture.md`、`README.md`、`需求.md`，并运行过 `tests/smoke.py`（79 项全部通过）。
>
> 整体工程素养不错——分层清晰、事件流解耦、异常体系完整——但深挖之后发现了几处**文档承诺与代码实现脱节**的问题，这些恰恰是最容易被扣分的点，因为它们不是"没写完"，而是"写了但没接上"。下面按矛盾 / 错误 / 职责划分 / 数据初始化 / 重叠冗余五个维度展开，最后给 10 个可扩展方向。

## 一、矛盾：文档承诺 ≠ 代码实现（已修复 ✅）

### 1.1 README/architecture.md 承诺的"三道防线"里，黑名单与敏感文件强制确认根本不存在

**原问题**：权限判定只看 capability / 模式 / 工具名白名单，`force` 永远是 `False`。用户对 `bash` 选一次 `a` 之后，`rm -rf /`、`sudo` 都会被直接执行；对 `write_file` 选一次 `a` 之后，写 `.env` / `.git/config` 也会被放行。文档在承诺一套不存在的安全模型。

**修复**：落地 [`security_rules.py`](src/coding_agent/security/security_rules.py) + [`PermissionManager`](src/coding_agent/security/permissions.py) 四级判定链：

1. **Blocked**：`sudo` / `rm -rf /` / `mkfs` / `curl | sh` 等永远 DENY，连 Bypass 也不能绕过（`DenyReason.POLICY_BLOCKED`）。
2. **Restricted**：`git push --force`、写入 `.git/` 永远 DENY，不出审批 UI（`DenyReason.RESTRICTED`）。
3. **Sensitive**：`.env`、私钥、`pip install`、`git push` 走 `NEED_APPROVAL` 且 `force=True`，不受「始终允许」/ AcceptEdits / Bypass 影响。
4. **Normal**：维持原有按模式 / 白名单的逻辑。

工具 `run()` 里对 Blocked / Restricted 再拦一次（`BlockedCommandError` / `SecurityError`），避免有人绕过 `PermissionManager` 直接 `execute`。`always_allowed` 按同类操作匹配：写入记 `write:{目录}`（根目录文件为 `write:.`，只放行根下其它文件；`write:pkg` 放行 `pkg/` 及其子目录，`write_file`/`edit_file` 共用），执行记 `exec:{命令名}`。敏感路径 / CRITICAL 仍走 force，不受白名单影响。见七、1。

### 1.2 Plan 模式的"只读命令放行"是空实现，导致该模式下 `bash` 形同虚设

**原问题**：`is_readonly_command()` 永远返回 `False`，Plan 模式下任何 bash 调用都被拒绝；smoke test 还把这个错误行为锁成了回归基线。

**修复**：[`security/utils.py`](src/coding_agent/security/utils.py) 实现了保守启发式：管道/列表里每一段都得是只读，命令替换、写重定向、后台任务一律拒绝。放行 `ls`/`git status`/`pytest`/`python -m pytest` 等，拒绝 `git push`/`touch`/`find -delete`。smoke test 改为断言放行只读、拒绝非只读，而不再把 bug 锁死。

### 1.3 docs/architecture.md 描述的目录结构与当前代码已经不一致

**原问题**：文档里的 `core/agent.py`、`core/session/`、`security/policy.py`、`context/manager.py` 等路径已经不存在，安全模型还在写 Ask/Agent/YOLO 和「`risk` 就是 READ/WRITE/EXEC」。

**修复**：按当前代码重写了目录树、数据流图、Tool 契约、Session 字段和安全模型一节（两轴 + 四级规则 + 实际的 `PermissionMode` 名称），README 的分层树和工具表（类型 / 风险拆开）一并同步。

## 二、明确的 Bug（已修复 ✅）

> 2025-08-28 更新：以下三处均已修复，`tests/smoke.py` 79 项全部通过。保留原始描述作为问题记录，修复说明见每小节末尾。

### 2.1 审批预览（`describe`）与真实执行（`run`）使用两套不同来源的参数默认值

`edit_file`/`write_file` 的 `describe()` 直接用 `args.get(...)`（工具调用的**原始** JSON 参数，尚未经过 `Tool.validate()` 的 schema 默认值填充），而 `run()` 收到的是 `registry.execute()` 里 `tool.validate(args)` **规范化之后**的参数：

```37:41:src/coding_agent/tools/registry.py
        try:
            tool = self.get(name)
            normalized = tool.validate(args)
            return tool.run(ctx, **normalized)
```

对比 `edit_file.py` 的两处：

```22:35:src/coding_agent/tools/defs/edit_file.py
    def describe(self, args: dict[str, Any], workspace: Path) -> tuple[str, str | None, str]:
        ...
        count = -1 if args.get("replace_all") else 1
```

目前恰好因为 `replace_all` 的 schema 默认值就是 `False`，`args.get("replace_all")` 缺省为 `None`（falsy）与规范化后的 `False` 效果一致，所以现在看不出问题——但这是"凑巧对齐"，不是设计保证。以后任何工具的某个参数默认值改成 `True`，或者新增一个有默认值的参数，`describe()` 展示给用户看的审批预览就会和真正执行的行为**悄悄不一致**（用户看到的 diff/摘要与实际发生的不是一回事，这在一个强调"审批前必须看到真实 diff"的安全模型里是相当严重的隐患）。

**建议**：`describe()` 和 `run()` 应该共用同一份 `tool.validate(args)` 结果，而不是各自从原始 args 里 `.get()`。

**修复**：把 `tool.validate(args)` 的调用点从 `ToolRegistry.execute()` 内部提前到 `AgentLoop._run_single_call()`——解析出原始 JSON 参数后，先拿到 `tool` 实例并立即规范化一次，规范化失败按原有的 `InvalidToolArgumentsError` 处理流程回喂模型；规范化成功后，这份**同一份**参数依次用于重复调用指纹（`record_call_fingerprint`）、权限判定与审批预览（`_resolve_permission` → `PermissionManager.evaluate` → `tool.describe`）、以及最终执行（`tool_executor.execute`）。`ToolRegistry.execute()` 内部仍会对（已规范化的）参数再跑一次 `validate()`，这是幂等操作（不会改变已经填好默认值、类型正确的字典），保留它是为了不破坏测试与直接调用 `registry.execute()` 的既有用法。已用脚本验证：`edit_file` 在不传 `replace_all` 时，`describe()` 现在拿到的是规范化后的 `False`，与 `run()` 完全一致。见 `core/runtime/loop.py` 的 `_run_single_call`。

### 2.2 `duration` 参数被计算、传递，但从未被使用——是一段"看起来在用，实际没用"的死代码

```219:250:src/coding_agent/core/runtime/loop.py
    def _respond(self, call: ToolCallBlock, responded: set[str], result: ToolRunResult, *, duration: float = 0.0) -> None:
        self._respond_text(
            call,
            result.to_model_text(),
            ok=result.ok,
            duration=duration,
            error=result.error,
        )
        responded.add(call.id)

    def _respond_text(
        self,
        call: ToolCallBlock,
        text: str,
        ok: bool,
        *,
        error: str | None = None,
        duration: float = 0.0,
    ) -> None:
        if self.session.tool_history.get(call.id) is None:
            self.session.start_tool_execution(...)
        self.session.finish_tool_execution(
            call_id=call.id,
            ok=ok,
            content=text,
            error=error,
        )
        self.session.add(ToolMessage.of(call.id))
```

`_run_single_call` 里用 `time.monotonic()` 精确算出的耗时，一路传到 `_respond_text` 后**从未被读取**；真正落在 `ToolExecution.duration` 上的值，是 `Session.finish_tool_execution` 内部重新用 `time.time()` 算的 `now - execution.started_at`（`session/session.py:135-136`）。两套计时各算各的，前者纯属摆设。虽然目前数值上大体相近（因为两次计时窗口几乎重叠），但这是典型的"接口参数名义上存在、实际链路早已断开"的坏味道，容易在后续维护中误导人（以为改这个参数有用）。

**修复**：`Session.finish_tool_execution()` 新增 `duration: float | None = None` 形参，若调用方传入则直接采用（`AgentLoop` 现在始终会传：真正执行过的调用传 `time.monotonic()` 算出的精确耗时，权限拒绝/参数校验失败/重复调用等"根本没执行"的分支传 `0.0`，语义比之前"即使没执行也会有一个几毫秒的墙钟耗时"更准确）；只有未来出现不经过 `AgentLoop`（比如测试）直接调用 `finish_tool_execution` 且不传 `duration` 的情况，才会退化为按时间戳估算，保证向后兼容。已用脚本验证：`ToolFinished` 事件里的 `duration` 和 `session.tool_history` 里落盘的 `ToolExecution.duration` 现在是同一个数值（不再是两套独立计时）。见 `core/runtime/loop.py` 的 `_respond_text` 与 `session/session.py` 的 `finish_tool_execution`。

### 2.3 `mark_modified()` / `SessionWorkspace.modified_files` 定义了却从未被调用

```193:201:src/coding_agent/session/session.py
    def mark_modified(self, path: Path) -> None:
        """
        标记文件已经被修改。
        """
        resolved_path = path.resolve()
        self.workspace.modified_files.add(resolved_path)
        self.metadata.touch()
```

`write_file`/`edit_file` 修改文件后只调用了 `mark_read`，没有调用 `mark_modified`。如果未来想做"任务结束时列出改动过的文件"这种摘要（Claude Code 有这个功能），这个字段现在是空的、永远不会被填充——是典型的"接口定义了但没接线"。

**修复**：在 `WriteFileTool.run()` 和 `EditFileTool.run()` 里，写完文件、调用 `mark_read()` 之后紧接着调用 `ctx.session.mark_modified(target)`。已用脚本验证：写文件、编辑文件之后 `session.workspace.modified_files` 里都正确出现了对应路径；`Session.clear()` 原本就会清空这个集合，生命周期不用额外处理。见 `tools/defs/write_file.py`、`tools/defs/edit_file.py`。注意：目前只是把数据**写入**补上了，还没有任何地方**读取**它（比如任务结束摘要展示"改动了哪些文件"），这是七、10 号可扩展方向里可以顺手补上的小功能，不在本次"修 bug"范围内。

## 三、职责划分问题（3.2/3.3/3.4 已修复 ✅，3.1 暂缓）

> 2025-08-28 更新：3.2（`ToolContext` 收窄）、3.3（`Runtime` 行为方法收敛）、3.4（`CommandRegistry`）均已修复，`tests/smoke.py` 79 项全部通过，并额外写脚本验证过。3.1 按讨论意见暂不改动——`ToolExecutor` 这层转发在当前规模下确实没有实际职责，但未来（埋点/熔断/hooks）肯定会用到，直接删掉再加回来没有意义，先保留，处理建议放进了七、10 号可扩展方向。

### 3.1 `ToolExecutor` 是一个没有任何职责的转发层（暂缓，留作扩展点）

```9:16:src/coding_agent/core/tool_executor.py
class ToolExecutor:


    def __init__(self, registry: ToolRegistry):
        self._registry = registry

    def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolRunResult:
        return self._registry.execute(name, args, ctx)
```

它不做重试、不做超时控制、不做埋点、不做钩子——纯粹把调用转给 `registry.execute`。现在 `AgentLoop` 同时持有 `self.runtime.tool_executor` 和 `self.runtime.registry`（还直接用 `self.registry.has(...)`、`self.registry.get(...)`），说明这层抽象**没有真正统一入口**，反而是"两条路都能到 registry"。

**建议**：要么让 `ToolExecutor` 承担点实际职责（比如未来的执行埋点、超时熔断、并发调度都放在这里，`AgentLoop` 只认 `ToolExecutor` 不直接碰 `registry`），要么直接删掉这层，让 `Runtime` 直接持有 `registry`。现在这种"因为架构图上画了一层就留一层"的状态，是为了分层而分层。

**结论（暂不改动）**：讨论后决定先不删这层——`ToolExecutor` 是七、10 号"Hooks 系统"方向的天然落点（工具执行前后的钩子、未来的埋点/超时熔断都应该加在这里），现在删掉、以后又要加回来没有意义。当前 `AgentLoop` 已经通过 3.3 的修复统一走 `Runtime.execute_tool()` 一条路径，不会再出现"两条路都能到 registry"的问题，`ToolExecutor` 目前"空转"是可以接受的过渡状态，具体职责规划见七、10。

### 3.2 `ToolContext` 直接把整个 `Session` 塞给每个工具——代码自己也承认这是问题

```70:79:src/coding_agent/tools/base.py
@dataclass
class ToolContext:
    """工具执行时可访问的运行环境。

    TODO: 不要传入整个 Session，改为 workspace 视图 + 受控 mutator，避免工具层穿透数据边界。
    """

    workspace: Path
    tool_config: ToolConfig
    session: Session
```

结果是 `write_file`/`edit_file`/`bash` 里出现了 `ctx.session.has_read(...)`、`ctx.session.mark_read(...)`、`ctx.session.bash_cwd = ...` 这种工具直接改会话状态的写法——工具本应只是"输入 → 输出"的纯执行单元，现在却对 `Session` 有隐式的读写权限，任何新工具作者都可以绕过安全/权限层直接改会话状态（比如伪造 `always_allowed`、清空 `tool_history`）。

这是"职责边界"里最值得在评审里主动指出并修复的一点——按 TODO 里说的做一个只读的 `WorkspaceView` + 明确的 mutator 方法集合（`mark_read`/`mark_modified`/`get_bash_cwd`/`set_bash_cwd`），而不是整个 `Session`。

**修复**：新增 `session/workspace_access.py`，`WorkspaceAccess` 包住一个 `Session`，只转发 `has_read`/`mark_read`/`mark_modified`/`bash_cwd`（getter+setter）这几个工具真正需要的方法，不透出消息历史、权限白名单、token 用量、`tool_history` 等其它 Session 状态。`ToolContext.session: Session` 改成 `ToolContext.workspace_access: WorkspaceAccess`，`AgentLoop.tool_ctx` 与全部工具实现（`read_file`/`write_file`/`edit_file`/`bash`）里的 `ctx.session.*` 全部换成 `ctx.workspace_access.*`；`tests/smoke.py` 里手动构造 `ToolContext` 的三处也同步改掉。已用脚本验证：工具依然能正常读写文件、bash 依然能正确延续/重置工作目录，79 项 smoke test 全部通过。见 `session/workspace_access.py`、`tools/base.py`。

### 3.3 `Runtime` 的封装被 `AgentLoop` 大量穿透

`AgentLoop` 里 `self.runtime.llm.chat(...)`、`self.runtime.tool_executor.execute(...)`、`self.runtime.permissions.evaluate(...)`、`self.runtime.registry`（经由 `self.registry` 属性透传）都是直接掏 `Runtime` 内部组件用，`Runtime` 自己只提供了 `run()`/`schemas_for_mode()`/`messages_for_request()` 三个"高层行为方法"，却没有阻止 `AgentLoop` 绕过它们直接拿内部对象。`Runtime` 目前更像一个"组件容器"而不是真正的"行为封装层"。

这不是致命问题，但如果评审问到"分层解耦体现在哪"，这里会站不住脚——建议把 `AgentLoop` 需要的行为都收敛成 `Runtime` 的方法（如 `Runtime.call_llm()`、`Runtime.execute_tool()`、`Runtime.check_permission()`），`AgentLoop` 不再直接引用 `runtime.llm`/`runtime.permissions` 等属性。

**修复**：给 `Runtime` 加了 `call_llm()`、`has_tool()`、`get_tool()`、`tool_names()`、`execute_tool()`、`check_permission()`、`apply_permission_decision()` 等行为方法；`AgentLoop` 里所有 `self.runtime.llm.chat(...)`、`self.runtime.tool_executor.execute(...)`、`self.runtime.permissions.evaluate(...)`、`self.registry.has/get(...)` 全部换成对应的 `self.runtime.xxx(...)` 调用，并删掉了 `AgentLoop.registry` 这个透传属性——现在 `AgentLoop` 只认 `Runtime` 暴露的行为方法，不再直接触达 `llm`/`tool_executor`/`permissions`/`registry` 这些内部组件。`PermissionManager.user_denied_message` 仍是无状态文案辅助。见 `core/runtime/__init__.py`、`core/runtime/loop.py`。

### 3.4 斜杠命令是一条不可扩展的 if/elif 链，与文档强调的"工具即插件"理念相悖

`cli/app.py._handle_command` 里 `/help`/`/tools`/`/mode`/`/usage`/`/clear`/`/cwd`/`/exit` 全部写在一个方法体的 if/elif 里。工具系统精心设计了"继承 `Tool` + 注册表自动导出 schema"的插件化模式，但命令系统完全没有对齐这个理念——新增一个 `/model`、`/history`、`/save` 都要在这个函数里加一段 elif。

**建议**：给 CLI 命令也做一个类似 `ToolRegistry` 的 `CommandRegistry`（`name -> handler`），保持两边设计哲学一致。

**修复**：新增 `cli/commands.py`，`SlashCommand`（name/usage/description/handler/aliases）+ `CommandRegistry`（`register`/`get`/`__iter__`），`build_default_commands()` 里一行注册一个命令，写法和 `tools/registry.py` 的 `build_default_registry()` 对齐。`/help` 的帮助文本改成从 `CommandRegistry` 动态生成（`build_help_text`），不再需要手写一份和 if/elif 分开维护的 `HELP_TEXT` 常量。`App._handle_command` 现在只是 `parts[0].lower()` 查表分发，新增命令只需要在 `commands.py` 写一个 handler 函数并注册一行，不用再碰 `app.py`。已用脚本手动跑过 `/help`/`/tools`/`/mode plan`/`/mode bogus`/`/usage`/`/cwd`/`/clear`/未知命令/`/exit` 全部命令，行为与修复前一致；`tests/smoke.py` 79 项全部通过（该测试不覆盖 CLI 层，属于额外的人工验证）。见 `cli/commands.py`、`cli/app.py`。

## 四、数据初始化方式的问题（已修复 ✅）

### 4.1 `SessionState`/`SessionWorkspace` 里塞了一堆"预留给未来阶段"的字段，当前完全没人读写

**原问题**：`plan`/`current_step`/`variables`（任务规划）、`SessionWorkspace.environment`（环境变量持久化）全局搜索均为**零引用**——每创建一个 `Session` 就要初始化这些永远空着的容器，字段的 shape 已经定好，但语义、写入时机、失效策略都还没有设计。`opened_files`/`modified_files`/`recent_calls` 等字段上也挂着 `# TODO 需要想想` 这类"未决设计下沉到数据结构里"的占位注释。

**修复**：零引用的字段直接注释掉（YAGNI），不再假装它们已经生效，并在注释里写清楚接入条件（哪个 Phase、谁写入、谁读取）：

```83:88:src/coding_agent/session/types.py
    # plan: list[str] = field(default_factory=list)
    # current_step: int = 0
    # variables: dict[str, Any] = field(default_factory=dict)
    # ↑ 任务规划字段：接入 Phase 4 的 todo_write 工具（见 docs/code-review.md 七、3）之前，
    # 没有任何代码读写它们。先注释掉而不是留着占位，避免误导——真正接入时再一起设计好
    # 写入时机（模型调用 todo_write 更新）和读取时机（system prompt / ContextBuilder 注入）。
```

`SessionWorkspace.environment` 同样注释掉并写明接入条件。而 `opened_files`/`modified_files`/`recent_calls`/`updated_at` 这几个**确实在用**的字段（配合本文档二、2.3 的 `mark_modified` 修复），把含糊的 `# TODO 需要想想` 替换成了准确描述"谁写、谁读、干什么用"的注释。

### 4.2 配置系统的"已知字段名单"与 dataclass 字段是手工重复维护的，容易漂移

**修复**：用 `dataclasses.fields()` 反射派生字段名集合，替换手工维护的 frozenset：

```30:44:src/coding_agent/config/loader.py
def _field_names(cls: type) -> frozenset[str]:
    """从 dataclass 反射出字段名集合，而不是手工抄一份容易漂移的名单——
    往 LLMConfig/LoopConfig/AppConfig/ToolConfig 加字段时，这里自动跟上，不用同步改。
    """
    return frozenset(f.name for f in dataclass_fields(cls))


_LLM_FIELDS = _field_names(LLMConfig)
_LOOP_FIELDS = _field_names(LoopConfig)
_APP_FIELDS = _field_names(AppConfig)
_TOOL_FIELDS = _field_names(ToolConfig)
```

以后往任何一个 Config dataclass 加字段，`load_config`/`_assemble` 自动认得它，不会再出现"改了 dataclass、忘了改 loader.py 的名单"这类漂移。

### 4.3 `.coding_agent/tmp/` 目录是遗留产物，当前代码没有任何逻辑写入它

**修复**：直接删除了这个空目录（本地运行态产物，不在版本控制里）。凡是运行时会自动创建的目录/文件，都应该有对应代码路径去解释它为什么存在——目前只有 `logs/` 被 `logutil.py` 使用，是合理的。

### 4.4 工具的 JSON Schema 默认值与 Python 函数签名默认值是两份手工同步的数据

**修复**：把每个默认值提成一个命名常量，JSON Schema 和 Python 函数签名共享同一个来源，而不是各写一份字面量：

```27:34:src/coding_agent/tools/defs/bash.py
    def run(self, ctx: ToolContext, command: str, timeout_ms: int = BASH_DEFAULT_TIMEOUT_MS) -> ToolRunResult:
```

`BASH_DEFAULT_TIMEOUT_MS`（bash）、`EDIT_FILE_DEFAULT_REPLACE_ALL`（edit_file）、`LIST_DIR_DEFAULT_PATH`/`LIST_DIR_DEFAULT_DEPTH`（list_dir）、`READ_FILE_DEFAULT_OFFSET`（read_file）均定义在对应的 `constants/tools/defs/*.py` 里，`PARAMETERS["default"]` 与函数签名默认值都引用它，改一处即可同步生效。

## 五、重叠 / 冗余（5.2 / 5.3 / 5.4 已修复 ✅；5.1 保留原结构）

### 5.1 `constants/tools/defs/*.py` 与 `tools/defs/*.py` 是两棵完全镜像的目录树

`tools/defs/bash.py` 定义 `BashTool` 类，`constants/tools/defs/bash.py` 定义它的 name/description/parameters 常量，一一对应、文件名相同。这种"数据"与"行为"强制拆到两个平行目录里的做法，好处是常量可以独立被其他模块引用（比如测试），坏处是新增/修改一个工具永远要同时打开两个文件、两层 `__init__.py`（`tools/defs/__init__.py`、`constants/tools/defs/__init__.py`、`constants/tools/__init__.py`、`constants/__init__.py` 层层转发），对一个目前只有 5 个工具的项目来说，这层间接性的收益还没有体现出来，更多是"为了看起来工程化"而增加的文件数量和跳转成本。

**建议**：至少把常量作为 `Tool` 子类的 `ClassVar` 直接内联（`RiskLevel`/`ToolCapability` 已经是这么做的），减少一层目录。

**处理**：这是原有的配置方式，不拆掉。4.4 已经把 schema 默认值和 Python 签名默认值收成同一份常量（仍放在 `constants/tools/defs/`），工具类继续从那里引用 `NAME` / `DESCRIPTION` / `PARAMETERS`。平行目录本身留下，作为"数据与行为分离"的既有约定。

### 5.2 `errors.py` 里定义的部分异常从未被抛出

`BlockedCommandError`、`ApprovalDeniedError`、`DenyReason.RESTRICTED`、`DenyReason.POLICY_BLOCKED`、`DenyReason.USER_DENIED` 全部是"预先声明、无人使用"的死代码，和第一节的安全模型缺口是同一个根因——它们本该是黑名单/敏感文件机制的产物，但那套机制没写，这些类型就只剩声明。

**修复**：`DenyReason.POLICY_BLOCKED` / `RESTRICTED` 已由 `PermissionManager` 真正返回；`BlockedCommandError` 在 `bash.run()` 里作为执行层二次拦截抛出。`ApprovalDeniedError` 和 `DenyReason.USER_DENIED` 仍是原错误体系的一部分，当前用户拒绝走的是 `ToolRunResult.failure` 回喂模型，不经过这两条类型——类型本身保留，不删。

### 5.3 `RiskLevel` 字段全项目定义、赋值，但对权限判定零作用

每个工具都写了 `risk = RiskLevel.LOW/MEDIUM/HIGH`，但权限判定（`PermissionManager.evaluate`）只看 `capability`（READ/WRITE/EXEC），`risk` 仅用于 CLI 审批面板的文字展示（`_RISK_LABEL`）。也就是说，`risk` 目前是纯"化妆品"字段——`tools/base.py` 里也自己写了 `"""TODO risklevel尚未参与权限系统"""`。这和 README 里"风险等级"这一列的说法（实际展示的是 capability 而不是 risk）也有术语上的混用，容易让人以为风险分级已经影响了安全策略。

**建议**：要么让 `risk` 真正参与决策（比如 CRITICAL 风险的工具即使 `always_allowed` 也要强制二次确认），要么先把它从"权限"相关的语境里摘出来，只当展示用的元数据说清楚。

**修复**：`capability` 和 `risk` 保持两轴，不互相替代。`RiskLevel.CRITICAL` 在 `PermissionManager._forced_confirmation_reason` 里强制 `force=True`，不受 Bypass / AcceptEdits / 本会话始终允许影响；MINIMAL–HIGH 继续用于审批面板展示。README 工具表拆成「类型 / 风险」两列，避免和 capability 混用。现有五个工具的 risk 赋值没改（list_dir=MINIMAL、read_file=LOW、write/edit=MEDIUM、bash=HIGH）。

### 5.4 `write_file`/`edit_file` 的 `describe()` 与 `run()` 重复实现了同一段 diff 计算逻辑

除了 4.2 提到的参数来源不一致问题，这两个方法本身的"读旧内容 → 算 diff"逻辑几乎是复制粘贴（`edit_file.py:22-35` 与 `edit_file.py:37-76`，`write_file.py` 同理），文件被读了两次、diff 算了两次。

**建议**：可以抽一个内部方法 `_plan_change(ctx_or_workspace, path, ...) -> (original, updated, diff)`，`describe()` 和 `run()` 都调它，从根源上避免两条路径分叉。

**修复**：在原工具文件里抽共用辅助，不改对外契约：`write_file` 用 `_snapshot()` 读旧内容，`edit_file` 用 `_planned_edit()` / `_apply_replace()` 算替换结果，`describe()` 和 `run()` 都走同一套规则再各自生成 diff。`run()` 里的 has_read / 唯一匹配等策略检查仍只在执行路径上。

## 六、其它工程化细节（顺手提一下）

- `uv.lock` 在 `.gitignore` 里被忽略，但 `uv.lock` 恰恰是保证多人/多机构建一致性的文件，通常应该提交而不是忽略。
- 根目录的 `test_deepseek.py`、`test_env.py` 是两个手写调试脚本，已被 git 跟踪，和 `tests/smoke.py` 的正式测试方式不统一，且 `test_env.py` 会把 API Key 前 8 位打印到标准输出——建议删除或移进 `scripts/`，不要打印任何密钥片段。
- `pyproject.toml` 没有 `pytest` 之类的 dev 依赖声明，和 `docs/architecture.md` Phase 5 的规划（"把 smoke.py 改写成 pytest"）对应不上，如果近期要做，建议提前把 `[dependency-groups] dev = ["pytest"]` 之类的骨架加上。

## 七、10 个可扩展维度

结合 `docs/architecture.md` 里已经排的 Phase 2-5 待办，以及当前架构本身预留的扩展点，给出 10 个方向，前 5 个是把现有"承诺但未完成"的地方做实，后 5 个是差异化的创新点：

1. **白名单粒度细化**（已落地）：`always_allowed` 不再按整个工具名记录。写入记 `write:{目录}`（根目录文件为 `write:.`，只放行根下其它文件，不含子目录；`write:pkg` 放行 `pkg/` 及其子目录，`write_file` 与 `edit_file` 共用），执行记 `exec:{命令名}`（`exec:pytest` 放行 `pytest -q`，不放行 `touch`；`python -m pytest` 记为 `exec:python -m pytest`）。审批面板的 `a` 选项会提示具体范围。敏感路径 / CRITICAL / Blocked / Restricted 仍在白名单之前判定。
2. **上下文压缩与长期 Memory**：实现 `CompressionManager`，按 token 预算把老的 `ToolExecution.content` 换成 `summary` + `ref`（架构已经为此预留了 `ToolResult.render(mode)`），再加 `AGENTS.md` 项目记忆注入与 `.coding_agent/sessions/*.json` 会话持久化 + `--continue` 恢复，补上 `ContextConfig.max_context_tokens` 现在完全没被使用的这个字段。
3. **任务规划工具 `todo_write`**：重新启用 `SessionState` 里已注释掉的 `plan`/`current_step`（见 4.1），让模型显式维护一份任务清单并在事件流里 `yield` 一个 `PlanUpdated` 事件，CLI 渲染成 Claude Code 那种带勾选框的任务面板。
4. **搜索工具 `grep`/`glob`**：补齐纯文本/正则检索能力（可以直接封装 `ripgrep` 子进程，复用 `_process.run_process`），让 Agent 不必靠 `bash` 里手写 `grep` 命令，也方便统一走安全/审批/输出截断策略。
5. **把 `tests/smoke.py` 迁移成 pytest + CI**：79 个手写断言拆成带 fixture 的 pytest 用例，配合 GitHub Actions 跑单测 + `ruff`/`mypy`，顺带把"配置字段手工同步"（4.2）这种隐患用 mypy/测试摆到台面上。
6. **子任务委派（Sub-agent）机制**（创新点）：在 `Runtime`/`AgentLoop` 现有的事件流架构上加一个 `dispatch_task` 工具，允许主 Agent 派生一个只读/受限工具集的子 `Runtime` 去做独立子任务（比如"先只读探索这个模块的用法，总结给我"），复用现有的 generator + 事件流模型而不引入新框架，是对"Agent Loop 通过 yield/send 解耦"这个既有设计的自然延伸，也是 Claude Code 的 Task 工具的核心能力。
7. **可插拔多 LLM Provider + 失败降级链**（创新点）：`LLMClient` 已经是抽象接口，当前只有 `DeepSeekClient` 一个实现。加一个 Provider 注册表（类似 `ToolRegistry`），支持 OpenAI/Anthropic/本地 Ollama，并在 `DeepSeekClient` 的重试逻辑之上加"同一层级多个 Provider 轮转降级"（比如主模型限流时自动切到备用模型），同时验证依赖倒置的设计是否真的做到"换实现不改 Agent Loop"。
8. **结构化可观测性与会话回放**（创新点）：把 `AgentEvent` 流（已经是结构化 dataclass）额外落一份 JSONL trace 到磁盘（区别于 `logutil.py` 面向人类阅读的日志），再写一个 `coding-agent --replay <trace.jsonl>` 命令用假 LLM/假审批把整个会话重放一遍——这既是调试工具，也天然是一个"确定性回归测试"的数据源，顺便解决 2.2 提到的 duration 数据链路断裂问题（回放时可以校验真实耗时）。
9. **Benchmark/评测框架**（创新点）：写一组"任务描述 + 断言脚本"（类似 mini SWE-bench），批量跑 `App.run_once`，统计任务完成率、平均轮次、平均 token 消耗、审批触发次数，给这个课程作业提供量化的"工程质量证据"，比空口说"设计得好"更有说服力，也能顺带发现 3.x/4.x 里提到的隐藏 bug（比如审批预览与执行不一致的场景）。
10. **Hooks 系统 + 项目类型自适应**：一是加一个工具执行前后的 hook 点（如"写文件后自动跑 `ruff format`"、"commit 前提示 git status"），落点就是 `core/tool_executor.py` 的 `ToolExecutor.execute()`——在调用 `registry.execute()` 前后插 `before_hooks`/`after_hooks` 两个列表，`AgentLoop` 完全不用改（它已经只认 `Runtime.execute_tool()` 这一个入口，见三、3.3 的修复），这正是 3.1 里说"先留着、以后有用"的落地方式；二是让 `build_system_prompt` 根据工作目录里的 `pyproject.toml`/`package.json`/`go.mod` 自动识别项目类型，调整"初期只支持 Python"这条限制，往"方便扩展"的需求方向再走一步。

## 八、建议的优先级

如果时间有限，建议优先做：

1. 第七节方向 2-3（上下文压缩、任务规划）——白名单粒度（方向 1）已落地；剩下的是 Phase 3-4 里价值最高、也最容易演示的部分。
2. 挑 1-2 个创新点（建议 6 号子任务委派或 9 号 Benchmark 框架）作为差异化亮点。
