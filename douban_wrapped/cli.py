"""命令行入口：`douban-wrapped ...`（pip 安装后）或 `python -m douban_wrapped ...`。"""

import sys

from .export import run


def main() -> None:
    sys.exit(run())
