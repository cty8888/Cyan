# 会话存储

磁盘上的会话是完整事件日志；发给模型的上下文是投影。compact 只往日志里追加 overlay，不删原文。

## 目录

根目录默认 `~/.cyan`（环境变量 `CYAN_HOME` 可覆盖）。工作区 `.cyan/` 只放 `logs/agent.log`。

```
~/.cyan/
├── projects/
│   └── <项目路径编码>/
│       ├── last                 # 一行 session-id，供 --continue
│       ├── <session-id>.jsonl   # 事件日志，只追加
│       └── <session-id>/        # 附属：meta.json、snapshots/
└── settings.json                # schema 版本；不含 API Key、不含 last
```

路径编码：先把路径里的 ``-`` 写成 ``--``，再把 ``:``、``/``、``\`` 换成 ``-``，避免 ``/foo/bar`` 与 ``/foo-bar`` 撞名。

- `/home/cty/projects/foo` → `-home-cty-projects-foo`
- `/home/cty/foo-bar` → `-home-cty-foo--bar`
- `/home/cty/foo/bar` → `-home-cty-foo-bar`
- `C:\Users\lenovo` → `C--Users-lenovo`

同一仓库在 WSL 路径和 Windows 盘符下是两棵树，不合并。

## 事件

每行一个 JSON 对象：`v` / `id` / `type` / `ts` / `parent_id` / `payload`。

| type | 作用 |
| --- | --- |
| `session_started` | 系统提示 |
| `user` / `continue` / `assistant` / `tool_result` | 对话与工具 |
| `summary` + `compact` | 压缩 overlay（`hidden_event_ids` + 插入点 `start_event_id`）。超大粘贴时 `summary.payload` 带 `preserved_user_text`，resume 时插回截断副本。 |
| `checkpoint` | 某条 user 当时的 cwd / 已读 / 白名单 |
| `file_op` | write_file / edit_file 索引 |
| `branch_forked` | 新分支开头 |

`tool_result.content` 落盘不截断。组窗截尾只发生在 ContextBuilder。

## 重放

1. 按时间应用每条 `compact`，得到 hidden 集合，以及 `start_event_id → summary`。
2. 扫描源事件：走到插入点时先放出 Summary（若有 `preserved_user_text` 再插截断 user），再跳过 hidden。
3. 当前任务若被切进压缩段且不是超大粘贴，该条 `user` 不进 hidden。
4. continue / resume 用 sidecar `meta.json` 的 head 状态（cwd、已读、白名单、用量）。rewind 才套该条 user **提交时**写下的 checkpoint（紧跟在 user 事件后面）。
5. `--continue` 跳过还没有用户消息的空会话；`last` 只在第一条真实用户消息、resume、fork 时更新。

## 分支

`/rewind <n> restore` 把锚点及之前的源事件（含该条 user 的 checkpoint）拷到新的 `<id>.jsonl`。父文件不改、不冻结。不回滚工作区文件。`tool_call` id 保持原值。

## CLI

- `cyan --continue` / `-c`
- `cyan --resume` / `--resume <id>`
- `/history` `/rewind` `/sessions` `/resume [<id 或前缀>]`（别名 `/continue`，REPL 内切会话，权限模式沿用当前会话，不恢复目标会话存的）`/new`（`/clear` 等同 `/new`）
