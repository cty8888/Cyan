"""临时冒烟测试：用假 LLM 驱动完整 Agent Loop，不访问网络。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from coding_agent.config import Config, ToolConfig
from coding_agent.core.agent import Agent
from coding_agent.core.events import ApprovalRequired, StopReason, TaskFinished, ToolFinished
from coding_agent.core.prompts import build_system_prompt
from coding_agent.core.session import Session
from coding_agent.errors import PathOutsideWorkspaceError
from coding_agent.llm.base import LLMClient
from coding_agent.llm.parser import parse_tool_arguments
from coding_agent.llm.types import AssistantMessage, LLMResponse, ToolCallBlock, ToolMessage, Usage
from coding_agent.logutil import get_logger, setup_logging
from coding_agent.security.approval import ApprovalDecision
from coding_agent.security.modes import PermissionMode
from coding_agent.security.paths import resolve_path
from coding_agent.security.permissions import PermissionManager
from coding_agent.tools.base import ToolContext
from coding_agent.tools._process import run_process
from coding_agent.tools.registry import build_default_registry

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(label)


class FakeLLM(LLMClient):
    """按预设脚本依次返回响应。"""

    def __init__(self, script):
        self.model = "fake"
        self.script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        item = self.script.pop(0) if self.script else AssistantMessage.of("done")
        return LLMResponse(message=item, usage=Usage(10, 5, 15))


def make_env(tmp: Path):
    config = Config(api_key="test", workspace=tmp)
    permissions = PermissionManager(tmp)
    registry = build_default_registry()
    session = Session.create(workspace=tmp, system_prompt="")
    ctx = ToolContext(workspace=tmp, tool_config=config.tool, session=session)
    return config, permissions, registry, ctx


def drive(agent, task, decision=ApprovalDecision.ALLOW_ONCE):
    """消费事件流，返回 (事件列表, 终止原因)。"""
    events, reply, reason = [], None, None
    stream = agent.run(task)
    while True:
        try:
            event = stream.send(reply)
        except StopIteration:
            break
        events.append(event)
        reply = decision if isinstance(event, ApprovalRequired) else None
        if isinstance(event, TaskFinished):
            reason = event.reason
    return events, reason


def eval_perm(permissions, tool, args, mode=PermissionMode.DEFAULT, always_allowed=None):
    outcome = permissions.evaluate(
        tool,
        args,
        mode=mode,
        always_allowed=always_allowed if always_allowed is not None else set(),
    )
    return outcome


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="ca_smoke_"))
    try:
        config, permissions, registry, ctx = make_env(tmp)

        # ---------------------------------------------------------- 工具层
        (tmp / "pkg").mkdir()
        (tmp / "pkg" / "mod.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

        r = registry.execute("list_dir", {"path": "."}, ctx)
        check("list_dir 列出目录", r.ok and "pkg/" in r.content, r.content)

        r = registry.execute("read_file", {"path": "pkg/mod.py"}, ctx)
        check("read_file 带行号", r.ok and "1 | def add" in r.content, r.content)

        r = registry.execute("edit_file", {"path": "pkg/mod.py", "old_string": "a - b", "new_string": "a + b"}, ctx)
        check("edit_file 精确替换", r.ok and "a + b" in (tmp / "pkg" / "mod.py").read_text(), r.error or "")
        check("edit_file 产出 diff", "diff" in r.metadata and "+" in r.metadata["diff"])

        r = registry.execute("edit_file", {"path": "pkg/mod.py", "old_string": "不存在", "new_string": "x"}, ctx)
        check("edit_file 找不到原文时返回失败", not r.ok and "找不到" in (r.error or ""), r.error or "")

        (tmp / "dup.txt").write_text("x\nx\n", encoding="utf-8")
        registry.execute("read_file", {"path": "dup.txt"}, ctx)
        r = registry.execute("edit_file", {"path": "dup.txt", "old_string": "x", "new_string": "y"}, ctx)
        check("edit_file 非唯一匹配时拒绝", not r.ok and "不唯一" in (r.error or ""), r.error or "")

        r = registry.execute("write_file", {"path": "sub/new.py", "content": "print('hi')\n"}, ctx)
        check("write_file 自动建父目录", r.ok and (tmp / "sub" / "new.py").is_file(), r.error or "")

        # ------------------------------------------------- Read/Write/Edit 前置读取语义
        _, _, ro_registry, ro_ctx = make_env(tmp)

        (tmp / "guarded.py").write_text("x = 1\n", encoding="utf-8")
        r = ro_registry.execute(
            "write_file", {"path": "guarded.py", "content": "x = 2\n"}, ro_ctx
        )
        check("覆写已存在但未读过的文件被拒绝", not r.ok and "还没有读取过" in (r.error or ""), r.error or "")

        r = ro_registry.execute(
            "edit_file", {"path": "guarded.py", "old_string": "x = 1", "new_string": "x = 2"}, ro_ctx
        )
        check("编辑未读过的文件被拒绝", not r.ok and "必须先用 read_file" in (r.error or ""), r.error or "")

        r = ro_registry.execute("write_file", {"path": "brand_new.py", "content": "y = 1\n"}, ro_ctx)
        check("新建文件无需先读", r.ok, r.error or "")

        ro_registry.execute("read_file", {"path": "guarded.py"}, ro_ctx)
        r = ro_registry.execute(
            "write_file", {"path": "guarded.py", "content": "x = 2\n"}, ro_ctx
        )
        check("读过之后允许覆写", r.ok, r.error or "")
        r = ro_registry.execute(
            "edit_file", {"path": "guarded.py", "old_string": "x = 2", "new_string": "x = 3"}, ro_ctx
        )
        check("写入后可以直接编辑而不用重新读", r.ok, r.error or "")

        big = "\n".join(f"line {i}" for i in range(2000))
        (tmp / "big.txt").write_text(big, encoding="utf-8")
        tiny_config = Config(api_key="test", workspace=tmp, tool=ToolConfig(max_file_read_chars=200))
        tiny_ctx = ToolContext(
            workspace=tmp, tool_config=tiny_config.tool, session=Session.create(workspace=tmp, system_prompt="")
        )
        r = ro_registry.execute("read_file", {"path": "big.txt"}, tiny_ctx)
        check("超预算的整篇读取返回 PARTIAL 视图", r.ok and "[PARTIAL VIEW]" in r.content, r.content)
        check("PARTIAL 视图不满足读取前置条件", not tiny_ctx.session.has_read((tmp / "big.txt").resolve()))

        r = ro_registry.execute(
            "read_file", {"path": "big.txt", "offset": 1, "limit": 500}, tiny_ctx
        )
        check(
            "显式 limit 超预算直接报错而不是静默截断",
            not r.ok and "调小 limit" in (r.error or ""),
            r.error or "",
        )

        r = ro_registry.execute("read_file", {"path": "big.txt", "offset": 1, "limit": 3}, tiny_ctx)
        check("显式小 limit 正常读取且不触发 PARTIAL", r.ok and "PARTIAL" not in r.content, r.content)
        check("显式区间读取满足读取前置条件", tiny_ctx.session.has_read((tmp / "big.txt").resolve()))

        (tmp / "empty.txt").write_text("", encoding="utf-8")
        r = registry.execute("read_file", {"path": "empty.txt"}, ctx)
        check("空文件返回明确提示", r.ok and "内容为空" in r.content, r.content)

        r = registry.execute("read_file", {"path": "pkg/mod.py", "offset": 999}, ctx)
        check("越界 offset 返回行数提示", r.ok and "共" in r.content and "没有内容" in r.content, r.content)

        # ------------------------------------------------------------- bash 工具
        r = registry.execute("bash", {"command": "echo hello"}, ctx)
        check("bash 正常执行", r.ok and "hello" in r.content, r.content)

        r = registry.execute("bash", {"command": "echo out; echo err >&2"}, ctx)
        check("bash 合并 stdout/stderr", r.ok and "out" in r.content and "err" in r.content, r.content)

        r = registry.execute("bash", {"command": "exit 3"}, ctx)
        check("bash 非零退出码算失败", not r.ok and "退出码：3" in (r.error or ""), r.error or "")

        r = registry.execute("bash", {"command": "sleep 5", "timeout_ms": 200}, ctx)
        check("bash 超时终止", not r.ok and "超时" in (r.error or ""), r.error or "")

        run_add = {"command": f'{sys.executable} -c "from pkg.mod import add; print(add(1, 2))"'}
        r = registry.execute("bash", run_add, ctx)
        check("bash 可以直接调用项目的 Python 解释器", r.ok and "3" in r.content, r.content)

        # cwd 在两次调用之间延续：这次 cd 进 pkg，下一次不带 cwd 参数也该停在那
        r = registry.execute("bash", {"command": "cd pkg && pwd"}, ctx)
        check("bash cd 后目录信息正确", r.ok and str((tmp / "pkg").resolve()) in r.content, r.content)
        r = registry.execute("bash", {"command": "pwd"}, ctx)
        check("bash 下一次调用延续上一次的目录", r.ok and str((tmp / "pkg").resolve()) in r.content, r.content)

        # 越出工作目录要被拉回来
        r = registry.execute("bash", {"command": "cd / && pwd"}, ctx)
        check("越出工作目录会被重置回根目录", r.ok and "已重置回工作目录根" in r.content, r.content)
        r = registry.execute("bash", {"command": "pwd"}, ctx)
        check("重置后下一次调用回到工作目录根", r.ok and str(tmp.resolve()) in r.content, r.content)

        # 输出超过上限时按 spec 截断（尾部加 ...[truncated]，不是老版本的头尾各留一半）
        tiny_out_config = Config(api_key="test", workspace=tmp, tool=ToolConfig(max_tool_output_chars=50))
        tiny_out_ctx = ToolContext(
            workspace=tmp, tool_config=tiny_out_config.tool, session=Session.create(workspace=tmp, system_prompt="")
        )
        big_echo = {"command": f'{sys.executable} -c "print(\'x\' * 500)"'}
        r = registry.execute("bash", big_echo, tiny_out_ctx)
        check("bash 输出超限时尾部截断", r.ok and r.content.rstrip().endswith("...[truncated]"), r.content)

        r = registry.execute("read_file", {"path": "nope.py"}, ctx)
        check("read_file 文件不存在返回失败", not r.ok, r.content)

        r = registry.execute("read_file", {}, ctx)
        check("缺少必填参数被拦截", not r.ok and "缺少必填参数" in (r.error or ""), r.error or "")

        r = registry.execute("read_file", {"path": "pkg/mod.py", "offset": "2"}, ctx)
        check("字符串数字被宽松转换", r.ok and "2 |" in r.content, r.content)

        r = registry.execute("no_such_tool", {}, ctx)
        check("未知工具返回失败", not r.ok and "不存在名为" in (r.error or ""), r.error or "")

        # ---------------------------------------------------------- 路径沙箱
        try:
            resolve_path(tmp, "../../etc/passwd")
            check("路径逃逸被拦截", False)
        except PathOutsideWorkspaceError:
            check("路径逃逸被拦截", True)

        (tmp / "link").symlink_to("/etc")
        try:
            resolve_path(tmp, "link/passwd")
            check("符号链接逃逸被拦截", False)
        except PathOutsideWorkspaceError:
            check("符号链接逃逸被拦截", True)

        # -------------------------------------------------------- 参数解析
        check("解析普通 JSON", parse_tool_arguments('{"path": "a.py"}') == {"path": "a.py"})
        check("解析空参数", parse_tool_arguments("") == {})
        check("剥离 markdown 围栏", parse_tool_arguments('```json\n{"a": 1}\n```') == {"a": 1})
        check("修复尾随逗号", parse_tool_arguments('{"a": 1,}') == {"a": 1})
        check("忽略前后杂文本", parse_tool_arguments('好的 {"a": 1} 完成') == {"a": 1})

        # -------------------------------------------------------- Agent Loop
        def call(name, args_json, cid="c1"):
            return AssistantMessage.of(tool_calls=[ToolCallBlock(id=cid, name=name, arguments=args_json)])

        # 正常闭环：调工具 -> 拿结果 -> 收尾
        run_loop = json.dumps({"command": f"{sys.executable} loop.py"})
        llm = FakeLLM([
            call("write_file", '{"path": "loop.py", "content": "print(41+1)"}'),
            call("bash", run_loop, "c2"),
            AssistantMessage.of("已创建并验证 loop.py，输出 42。"),
        ])
        agent = Agent(config, llm, registry, permissions)
        events, reason = drive(agent, "写个脚本")
        check("Agent 正常完成", reason is StopReason.COMPLETED, str(reason))
        check("Agent 执行了两次工具", sum(isinstance(e, ToolFinished) for e in events) == 2)
        check("产物文件已生成", (tmp / "loop.py").is_file())

        # 达到轮次上限
        cfg_small = Config(api_key="t", workspace=tmp, max_iterations=3)
        # 每轮参数不同，避免先被重复调用检测拦下
        llm = FakeLLM([call("list_dir", '{"path": ".", "depth": %d}' % (i + 1), f"c{i}") for i in range(10)])
        agent = Agent(cfg_small, llm, registry, permissions)
        _, reason = drive(agent, "循环")
        check("达到轮次上限会终止", reason is StopReason.MAX_ITERATIONS, str(reason))

        # 重复调用检测（相同工具+相同参数）
        llm = FakeLLM([call("read_file", '{"path": "nope.py"}', f"r{i}") for i in range(10)])
        agent = Agent(config, llm, registry, permissions)
        _, reason = drive(agent, "重复")
        check("重复调用被打断", reason is StopReason.REPEATED_CALLS, str(reason))

        # 连续工具失败
        llm = FakeLLM([call("read_file", '{"path": "miss%d.py"}' % i, f"f{i}") for i in range(10)])
        agent = Agent(config, llm, registry, permissions)
        _, reason = drive(agent, "连续失败")
        check("连续失败会终止", reason is StopReason.TOOL_FAILURES, str(reason))

        # 参数非法：模型有机会自我纠正
        llm = FakeLLM([
            call("read_file", "{不是JSON"),
            AssistantMessage.of("参数写错了，已放弃。"),
        ])
        agent = Agent(config, llm, registry, permissions)
        _, reason = drive(agent, "坏参数")
        check("参数非法不会崩溃", reason is StopReason.COMPLETED, str(reason))
        tool_msgs = [m for m in agent.session.messages if isinstance(m, ToolMessage)]
        block = tool_msgs[0].tool_result
        execution = agent.session.tool_history.get(block.tool_call_id) if block else None
        check(
            "参数非法也回喂了 tool 消息",
            len(tool_msgs) == 1 and execution is not None and "不是合法 JSON" in (execution.result.content or ""),
        )

        # 审批：Agent 模式下普通 exec 需确认，拒绝后不执行
        llm = FakeLLM([
            call("bash", '{"command": "touch denied_by_bash.txt"}'),
            AssistantMessage.of("好的，已放弃执行。"),
        ])
        agent = Agent(config, llm, registry, permissions)
        events, _ = drive(agent, "touch", decision=ApprovalDecision.DENY)
        check("普通 exec 触发审批", any(isinstance(e, ApprovalRequired) for e in events))
        check("拒绝后 bash 未执行", not (tmp / "denied_by_bash.txt").exists())

        # write 需确认，审批面板带 diff
        llm = FakeLLM([
            call("write_file", '{"path": ".env", "content": "K=1"}'),
            AssistantMessage.of("好的。"),
        ])
        agent = Agent(config, llm, registry, permissions)
        events, _ = drive(agent, "写 env", decision=ApprovalDecision.ALLOW_ONCE)
        approval = next(e.request for e in events if isinstance(e, ApprovalRequired))
        check("write 触发审批", approval is not None)
        check("审批面板带 diff", approval.detail_format == "diff" and "+K=1" in (approval.detail or ""))

        # 只读工具不触发审批
        llm = FakeLLM([call("list_dir", '{"path": "."}'), AssistantMessage.of("看完了")])
        agent = Agent(config, llm, registry, permissions)
        events, _ = drive(agent, "看目录")
        check("只读工具不审批", not any(isinstance(e, ApprovalRequired) for e in events))

        # write 需用户确认
        llm = FakeLLM([call("write_file", '{"path": ".env", "content": "K=1"}'), AssistantMessage.of("好")])
        agent = Agent(config, llm, registry, permissions)
        events, _ = drive(agent, "改 env", decision=ApprovalDecision.DENY)
        requests = [e.request for e in events if isinstance(e, ApprovalRequired)]
        check("write 需确认", len(requests) == 1, str(requests))

        # 上下文完整性：每个 tool_call 都有对应响应
        assistant_calls = sum(len(m.tool_calls) for m in agent.session.messages if isinstance(m, AssistantMessage))
        tool_replies = sum(1 for m in agent.session.messages if isinstance(m, ToolMessage))
        check("每个 tool_call 都有 tool 响应", assistant_calls == tool_replies, f"{assistant_calls} vs {tool_replies}")

        # ------------------------------------------------- 回归：三个已验证的缺陷
        # 缺陷 1：Ctrl-C 时子进程在独立进程组里收不到 SIGINT，必须显式清理
        marker = "ca_regress_marker_7731"

        def interrupt_soon():
            time.sleep(0.8)
            os.kill(os.getpid(), signal.SIGINT)

        threading.Thread(target=interrupt_soon, daemon=True).start()
        try:
            run_process(f"sleep 60 # {marker}", tmp, timeout=30, shell=True)
            interrupted = False
        except KeyboardInterrupt:
            interrupted = True
        time.sleep(0.4)
        survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout.split()
        check("中断时终止子进程，不留孤儿", interrupted and not survivors, f"残留 {len(survivors)} 个")
        subprocess.run(["pkill", "-f", marker], capture_output=True)

        # 缺陷 2：A-B-A-B 交替循环此前完全检测不到
        s = Session.create(workspace=tmp, system_prompt="")
        alternating = []
        for _ in range(3):
            alternating.append(s.record_call_fingerprint("read_file", {"path": "a.py"}))
            alternating.append(s.record_call_fingerprint("read_file", {"path": "b.py"}))
        check("交替循环能被检测", max(alternating) >= 3, str(alternating))

        # 但「改文件 -> 重跑测试」的正常迭代不该被误判
        s = Session.create(workspace=tmp, system_prompt="")
        iterating = []
        for _ in range(4):
            iterating.append(s.record_call_fingerprint("bash", {"command": "pytest"}))
            s.record_call_fingerprint("edit_file", {"path": "x.py"})
            s.record_progress()
        check("有实质进展的重复不误判", max(iterating) == 1, str(iterating))

        # -------------------------------------------------------- Permission Mode
        write_tool = registry.get("write_file")
        bash_tool = registry.get("bash")
        read_tool = registry.get("read_file")

        plan_write = eval_perm(
            permissions, write_tool, {"path": "ok.py", "content": "x"}, mode=PermissionMode.PLAN
        )
        check("Plan 模式拒绝 write", plan_write.kind == "deny", plan_write.kind)

        plan_read = eval_perm(permissions, read_tool, {"path": "a.py"}, mode=PermissionMode.PLAN)
        check("Plan 模式允许 read", plan_read.kind == "allow", plan_read.kind)

        plan_pytest = eval_perm(
            permissions, bash_tool, {"command": "pytest -q"}, mode=PermissionMode.PLAN
        )
        check("Plan 模式 exec 待实现只读判定", plan_pytest.kind == "deny", plan_pytest.kind)

        plan_touch = eval_perm(
            permissions, bash_tool, {"command": "touch x.txt"}, mode=PermissionMode.PLAN
        )
        check("Plan 模式拒绝非只读 exec", plan_touch.kind == "deny", plan_touch.kind)

        default_write = eval_perm(permissions, write_tool, {"path": "ok.py", "content": "x"})
        check("Default 模式 write 首次需审批", default_write.kind == "need_approval", default_write.kind)

        default_env = eval_perm(permissions, write_tool, {"path": ".env", "content": "K=1"})
        check("Default 模式 write 需审批", default_env.kind == "need_approval", default_env.kind)

        default_pytest = eval_perm(permissions, bash_tool, {"command": "pytest -q"})
        check("Default 模式 exec 首次需审批", default_pytest.kind == "need_approval", default_pytest.kind)

        accept_write = eval_perm(
            permissions, write_tool, {"path": "ok.py", "content": "x"}, mode=PermissionMode.ACCEPT_EDITS
        )
        check("AcceptEdits 模式 write 直接放行", accept_write.kind == "allow", accept_write.kind)

        accept_pytest = eval_perm(
            permissions, bash_tool, {"command": "pytest -q"}, mode=PermissionMode.ACCEPT_EDITS
        )
        check("AcceptEdits 模式 exec 首次需审批", accept_pytest.kind == "need_approval", accept_pytest.kind)

        bypass_rm = eval_perm(permissions, bash_tool, {"command": "rm -rf /"}, mode=PermissionMode.BYPASS)
        check("Bypass 直接放行", bypass_rm.kind == "allow", bypass_rm.kind)

        bypass_write = eval_perm(
            permissions, write_tool, {"path": ".git/config", "content": "x"}, mode=PermissionMode.BYPASS
        )
        check("Bypass write 直接放行", bypass_write.kind == "allow", bypass_write.kind)

        whitelisted = eval_perm(
            permissions,
            bash_tool,
            {"command": "pytest -q"},
            always_allowed={"bash"},
        )
        check("白名单后 bash 普通命令放行", whitelisted.kind == "allow", whitelisted.kind)

        whitelisted_write = eval_perm(
            permissions,
            write_tool,
            {"path": "ok.py", "content": "x"},
            always_allowed={"write_file"},
        )
        check("白名单后 write 直接放行", whitelisted_write.kind == "allow", whitelisted_write.kind)

        req_git = eval_perm(permissions, bash_tool, {"command": "cat .git/config"})
        check("bash exec 首次需审批", req_git.kind == "need_approval", req_git.kind)

        req_safe = eval_perm(permissions, bash_tool, {"command": "git status && pytest -q"})
        check("普通 exec 需审批", req_safe.kind == "need_approval", req_safe.kind)

        check(
            "只读工具无需审批",
            eval_perm(permissions, read_tool, {"path": "a.py"}).kind == "allow",
        )

        # Plan 模式 schema 过滤
        plan_cfg = Config(api_key="test", workspace=tmp, permission_mode=PermissionMode.PLAN)
        plan_session = Session.create(workspace=tmp, system_prompt="", permission_mode=PermissionMode.PLAN)
        plan_agent = Agent(plan_cfg, FakeLLM([AssistantMessage.of("ok")]), registry, permissions, session=plan_session)
        plan_schemas = plan_agent._schemas_for_mode()
        plan_names = {s["function"]["name"] for s in plan_schemas}
        check("Plan 模式暴露 read + bash", plan_names == {"list_dir", "read_file", "bash"}, str(plan_names))

        # 缺陷 3：bash 此前能绕过敏感文件的强制确认
        prompt = build_system_prompt(tmp)
        check("system prompt 写明 Python 解释器路径", sys.executable in prompt)

        log_path = setup_logging(config.log_dir, level="INFO", to_stderr=False)
        get_logger("cli").info("smoke-marker-42")
        logged = log_path.read_text(encoding="utf-8")
        check("日志写入文件", log_path.is_file() and "smoke-marker-42" in logged, str(log_path))
        check("默认不往 stderr 打日志", sum(h.__class__.__name__ == "StreamHandler" for h in get_logger().handlers) == 0)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} 项失败：")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
