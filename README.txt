Cyan —— 命令行编程智能体

Git 仓库：https://github.com/cty8888/Cyan

一、如何运行

环境要求：Python 3.10+、包管理器 uv、DeepSeek API Key。内容搜索工具 grep 依赖本机已安装 ripgrep。

  uv sync
  echo "DEEPSEEK_API_KEY=sk-..." > .env
  uv run cyan                 # 在当前目录启动交互 REPL

Agent 默认以启动目录为工作区，且只能访问该目录内的文件。常用参数：

  -w, --workspace   指定工作目录
  -m, --model       模型名称，默认 deepseek-chat
  -c, --continue    恢复本工作区最近一次会话（会先回放历史）
  --resume [id]     列出或指定会话 id 恢复
  --no-stream       关闭流式输出，整段回复一次显示
  --mode            权限模式：plan / default / accept_edits

进入 REPL 后直接输入自然语言即开始任务。敲 / 弹出斜杠命令（/help 查看全部），敲 @ 引用工作区内文件。任务执行中按 Ctrl-C 中断。开发测试：

  uv sync --group dev && uv run pytest

二、特色功能

1. 全自研 Agent 内核。不依赖 LangChain、LlamaIndex、OpenAI Agents SDK 等任何 Agent 框架或托管代码解释器，仅使用模型厂商的 OpenAI 兼容 API 与原生 Tool Calling。Agent Loop、工具系统、输出解析、安全策略、终止条件与错误恢复全部自行实现。CLI 与内核通过事件流解耦：内核是 generator，向外 yield 事件，审批决策由 send() 回传，因此换成 TUI 或测试桩不必改循环。

2. 完整编程工具集。list_dir / read_file / glob / grep 负责探索；edit_file 做唯一字符串替换（比整文件重写省 token），write_file 负责新建或整体改写；bash 是唯一的命令入口，测试、构建、git 都走它。bash 每次独立新进程（环境变量不跨调用），但工作目录会延续，越出工作区自动拉回。多步任务由模型调用 todo_write 维护清单。

3. 分层安全模型。判定顺序固定：工作区路径沙箱 → deny 规则 → 关键路径删除强制询问 → ask 规则 → 只读命令与 allow 规则 → Plan / Default / AcceptEdits 三种模式及会话白名单。内置拒绝 sudo 等危险命令，读写 .env、私钥、.git 需确认。写操作在确认前展示完整 diff；用户可选 y（本次）/ n（拒绝）/ a（本会话同类始终允许）。

4. 可恢复的会话与上下文压缩。对话以只追加的 jsonl 持久化到 ~/.cyan/projects/，支持 --continue / --resume 与 /rewind 分叉。上下文接近模型窗口时自动把较早历史收成摘要，事件日志中的原文不删除。任务里的 @path 会在提交时拍成文件内容快照，随历史一起重放和压缩。

5. 指令层、Skills 与自动记忆。cyan.md 提供个人级与项目级持久规则；Skills 以目录加 SKILL.md 的形式按需注入工作方法；项目级 Auto Memory 在任务成功结束后沉淀笔记。它们在组窗时叠进 system 角色，不写入会话日志。

6. 终端体验。rich 渲染 Markdown、diff 与审批面板；助手回复默认按 SSE 流式打字显示；斜杠命令与文件路径随打字实时过滤候选。

三、其它说明

模块按 cli / core / session / context / prompt / llm / tools / security 分层。Session 保存状态，Runtime 负责下一步行动。工具的可预期失败（文件不存在、命令超时、参数非法、权限拒绝）不会中断循环，而是转成结构化结果回喂模型，由模型自行换方案。终止条件包括：模型给出无工具调用的完整回复、达到轮次上限、连续失败、重复无效调用、用户中断。

新增工具只需继承 Tool、填写 schema 并实现 run()，在注册表加一行即可，JSON Schema 自动导出给模型。推送到 main 或提交 PR 时，GitHub Actions 会跑 ruff、vulture 与 pytest。

更完整的设计见 README.md 与 docs/architecture.md。
