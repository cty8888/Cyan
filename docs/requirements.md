我要设计并实现一个编程智能体（Cyan），能够自主地读写文件、执行命令，完成用户交给它的编程任务，类似一个简化版 Claude Code、Codex、OpenCode、DeepSeek Harness 等。

要求：

1. 不允许在现有 Agent 产品上封装界面，不得使用任何 Agent 框架或 SDK，包括但不限于：
- LangChain
- LlamaIndex
- OpenAI Agents SDK
- Claude Agent SDK
- AutoGen
- CrewAI

2. 允许使用：
- 模型厂商 API 客户端库
- OpenAI 兼容 API
- 模型原生 Tool Calling 接口

3. 不允许依赖 API 服务端托管的代码执行或文件工具，例如：
- Code Interpreter
- Files API

4. 重要逻辑必须自行实现，包括：
- Agent Loop
- Tool 系统定义与执行
- 任务规划
- 上下文管理
- Memory
- 模型输出解析
- 循环终止条件
- 错误处理与恢复


主要实现：

- 一个类似 Claude Code 的 CLI 编程智能体
- 支持自主读取目录、读取文件、修改文件、执行命令
- 初期只支持 Python 文件执行，但工具设计需要方便扩展
- 实现完整 Agent 工作流程：
  用户输入任务 → 模型分析 → 调用工具 → 获取结果 → 继续执行 → 完成任务
- 考虑文件权限、命令执行安全、错误提示和异常恢复


工程要求：

- Python 实现
- 模块化设计
- 结构清晰
- 易扩展
- 工程化实现

当前环境：
- Windows + WSL
- Python 环境已配置
- uv 项目管理
- DeepSeek API 已配置完成

请先进行整体架构设计和开发规划，不要直接生成代码。