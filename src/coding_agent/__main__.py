"""命令行入口。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.console import Console

from .config import Config
from .errors import AgentError, ConfigError
from .logutil import get_logger, setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="coding-agent",
        description="一个自主读写文件、执行命令来完成编程任务的命令行智能体",
    )
    parser.add_argument("-p", "--prompt", help="直接执行单个任务后退出（非交互模式）")
    parser.add_argument("-w", "--workspace", type=Path, help="工作目录，默认为当前目录")
    parser.add_argument("-m", "--model", help="模型名称，默认 deepseek-chat")
    parser.add_argument("--api-key", help="API Key，默认读取环境变量 DEEPSEEK_API_KEY")
    parser.add_argument("--base-url", help="API 地址，默认 https://api.deepseek.com")
    parser.add_argument("--max-iterations", type=int, help="单个任务的最大轮次，默认 30")
    parser.add_argument(
        "--yolo",
        action="store_true",
        default=None,
        help="跳过写入与执行的逐次确认（敏感文件与危险命令仍会拦截）",
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    console = Console()
    logger = get_logger()

    try:
        config = Config.load(
            workspace=args.workspace,
            model=args.model,
            api_key=args.api_key,
            base_url=args.base_url,
            max_iterations=args.max_iterations,
            yolo=args.yolo,
            log_level=args.log_level,
            verbose=args.verbose,
        )
    except ConfigError as exc:
        console.print(f"[bold red]配置错误[/] {exc}")
        return 2

    log_path = setup_logging(config.log_dir, level=config.log_level, to_stderr=config.verbose)
    logger.info("启动 workspace=%s model=%s log=%s", config.workspace, config.model, log_path)

    from .cli.app import App

    try:
        app = App(config, console=console)
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
