"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from .errors import AgentError, ConfigError
from .logutil import get_logger, setup_logging
from .security.types import PermissionMode
from .settings import AgentSettings


def build_parser() -> argparse.ArgumentParser:
    """声明命令行参数；真正的默认值在 ``AgentSettings`` / 环境变量里。"""
    parser = argparse.ArgumentParser(
        prog="cyan",
        description="Cyan：自主读写文件、执行命令来完成编程任务的命令行智能体",
    )
    parser.add_argument("-p", "--prompt", help="直接执行单个任务后退出（非交互模式）")
    parser.add_argument("-w", "--workspace", type=Path, help="工作目录，默认为当前目录")
    parser.add_argument("-m", "--model", help="模型名称，默认 deepseek-chat")
    parser.add_argument("--api-key", help="API Key，默认读取环境变量 DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", help="API 地址，默认 https://api.deepseek.com")
    parser.add_argument("--max-iterations", type=int, help="单个任务的最大轮次，默认 30")
    parser.add_argument(
        "--log-level",
        dest="log_level",
        default=None,
        help="日志级别（DEBUG/INFO/WARNING/ERROR），默认 INFO；文件始终额外保留 DEBUG 细节",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=None,
        help="同时把日志打到 stderr（默认只写文件，避免和 rich 界面叠在一起）",
    )
    parser.add_argument(
        "--mode",
        choices=["plan", "default", "accept_edits", "bypass"],
        help="权限模式：plan=只读规划 / default=默认 / accept_edits=自动批准编辑 / bypass=跳过普通审批（黑名单与敏感操作仍生效），默认 default",
    )
    parser.add_argument(
        "-c",
        "--continue",
        dest="continue_last",
        action="store_true",
        help="恢复本工作区最近一次会话",
    )
    parser.add_argument(
        "--resume",
        nargs="?",
        const="",
        default=None,
        help="恢复指定会话；不带 id 时列出本工作区会话供选择",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """加载配置、初始化日志，然后进入一次性任务或交互 REPL。"""
    args = build_parser().parse_args(argv)
    console = Console()
    logger = get_logger()

    try:
        settings = AgentSettings.load(
            workspace=args.workspace,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            max_iterations=args.max_iterations,
            log_level=args.log_level,
            verbose=args.verbose,
            permission_mode=PermissionMode(args.mode) if args.mode else None,
        )
    except ConfigError as exc:
        console.print(f"[bold red]配置错误[/] {exc}")
        return 2

    log_path = setup_logging(settings.log_dir, level=settings.cli.log_level, to_stderr=settings.cli.verbose)
    logger.info("启动 workspace=%s model=%s log=%s", settings.workspace, settings.llm.model, log_path)

    from .cli.app import App

    resume_id = args.resume
    if resume_id == "":
        picked = _pick_session(console, settings.workspace)
        if picked is None:
            return 1
        resume_id = picked

    try:
        app = App(
            settings,
            console=console,
            resume=resume_id,
            continue_last=bool(args.continue_last),
            permission_mode_override=PermissionMode(args.mode) if args.mode else None,
        )
        if args.prompt:
            return app.run_once(args.prompt)
        return app.run_interactive()
    except AgentError as exc:
        console.print(f"[bold red]错误[/] {exc}")
        logger.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]已退出[/]")
        logger.info("已退出")
        return 130


if __name__ == "__main__":
    sys.exit(main())


def _pick_session(console: Console, workspace: Path) -> str | None:
    from .session.store import list_sessions

    items = list_sessions(workspace)
    if not items:
        console.print("[yellow]这个工作区还没有已保存的会话[/]")
        return None
    console.print("本工作区会话：")
    for index, item in enumerate(items, start=1):
        title = item.title or "(无标题)"
        console.print(f"  {index}. {item.session_id[:8]}  {title}")
    raw = console.input("选择序号或会话 id：").strip()
    if not raw:
        return None
    if raw.isdigit():
        number = int(raw)
        if 1 <= number <= len(items):
            return items[number - 1].session_id
        console.print("[yellow]序号超出范围[/]")
        return None
    from .session.store import resolve_session_id

    session_id = resolve_session_id(workspace, raw)
    if session_id is None:
        console.print(f"[yellow]找不到会话 {raw}[/]")
    return session_id
