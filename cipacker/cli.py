"""命令行入口。

结构化事件流（``--structured``）契约完整保留，并**只做追加**：
新增 ``stage=verify`` / ``stage=doctor`` / ``level=finding``，
既有事件类型与字段语义不变，旧版 GUI 外壳可继续工作。

一处行为修正：``result=`` 事件由 :func:`main` 的单一出口输出，
保证每个进程生命周期内**恰好一次**。旧版在 ``except SystemExit``
分支里 emit 后又 ``raise``，会导致事件重复输出。
"""

from __future__ import annotations

import argparse
import sys

from . import __version__, logui, structured
from .errors import (
    EXIT_CANCELLED,
    EXIT_FAILURE,
    EXIT_OK,
    CancelledError,
    CIPackerError,
)

PROG = "cipacker"


# --------------------------------------------------------------------------- #
# 参数解析
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="CIPacker — ClassIsland 配置打包 / 解包 / 校验 / 体检工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_epilog(),
    )
    parser.add_argument(
        "--version", action="version", version=f"{PROG} {__version__}"
    )

    sub = parser.add_subparsers(dest="command", required=True, metavar="命令")

    _add_pack(sub)
    _add_unpack(sub)
    _add_list(sub)
    _add_verify(sub)
    _add_doctor(sub)
    _add_download(sub)

    return parser


def _epilog() -> str:
    from .layers import describe_layers

    return (
        "迁移层（--layers 可用值）:\n"
        + "\n".join(describe_layers())
        + "\n\n示例:\n"
        "  # 打包全部配置（确定性输出，附带包指纹）\n"
        "  cipacker pack D:\\\\ClassIsland -o backup.zip\n\n"
        "  # 只带走课表档案与应用设置\n"
        "  cipacker pack D:\\\\ClassIsland -o slim.zip --layers settings,profiles\n\n"
        "  # 打包前体检并脱敏敏感信息\n"
        "  cipacker pack D:\\\\ClassIsland --doctor --redact\n\n"
        "  # 校验包完整性\n"
        "  cipacker verify backup.zip --deep\n\n"
        "  # 检查一份配置的健康状况\n"
        "  cipacker doctor D:\\\\ClassIsland\n\n"
        "  # 解包到已有 ClassIsland 目录\n"
        "  cipacker unpack backup.zip -d D:\\\\ClassIsland\n"
    )


def _add_structured(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--structured",
        action="store_true",
        help="向 stderr 输出机器可读事件流（GUI 外壳集成用）",
    )


def _add_pack(sub) -> None:
    p = sub.add_parser("pack", help="打包 ClassIsland 配置", description="打包配置为迁移包")
    p.add_argument("dir", help="ClassIsland 数据目录（CI 2.x 通常是 …/data）")
    p.add_argument("-o", "--output", default="ci_config.zip", help="输出包路径（默认 ci_config.zip）")
    p.add_argument(
        "--layers",
        default=None,
        metavar="列表",
        help="要包含的迁移层，逗号分隔；all 表示全部（默认）",
    )
    p.add_argument("--include-backups", action="store_true", help="包含 Backups/ 目录")
    p.add_argument("--no-manifest", action="store_true", help="不写入包清单（导出为官方风格）")
    p.add_argument(
        "--cidata",
        action="store_true",
        help="使用 .cidata 扩展名（与 ClassIsland 官方档案格式保持一致）",
    )
    p.add_argument("--doctor", action="store_true", help="打包前先执行体检")
    p.add_argument("--redact", action="store_true", help="打包时脱敏敏感字段（写入包内，不改动源目录）")
    p.add_argument("--strict", action="store_true", help="严格模式：体检的警告视为错误并中止")
    p.add_argument(
        "--preserve-mtime",
        action="store_true",
        help="保留文件真实修改时间（将失去字节级可复现性）",
    )
    p.add_argument(
        "--json-report",
        metavar="路径",
        help="把体检报告写入指定 JSON 文件（不阻断打包；如需及时中止请配合 --doctor）",
    )
    _add_structured(p)
    p.set_defaults(handler=cmd_pack)


def _add_unpack(sub) -> None:
    p = sub.add_parser("unpack", help="解包配置到 ClassIsland 目录", description="把迁移包解包到目标安装")
    p.add_argument("input", help="迁移包路径 (.zip / .cidata)")
    p.add_argument("-d", "--dir", required=True, help="目标 ClassIsland 数据目录")
    p.add_argument("-y", "--yes", action="store_true", help="跳过所有确认")
    p.add_argument("--yes-download", action="store_true", help="允许自动下载缺失的 ClassIsland")
    p.add_argument("--no-backup", action="store_true", help="不备份将被覆盖的文件")
    p.add_argument(
        "--force",
        action="store_true",
        help="目标不是有效 ClassIsland 安装时也强制解包（用于预置配置/制作母盘）",
    )
    p.add_argument("--ci-version", default=None, help="缺失 ClassIsland 时下载的版本")
    p.add_argument("--source", default=None, help="指定下载数据源")
    p.add_argument("--spoof-ua", action="store_true", help="下载时使用浏览器 User-Agent")
    p.add_argument("--dry-run", action="store_true", help="只展示解包计划，不写入任何文件")
    _add_structured(p)
    p.set_defaults(handler=cmd_unpack)


def _add_list(sub) -> None:
    p = sub.add_parser("list", help="查看迁移包内容", description="列出包内文件与打包信息")
    p.add_argument("input", help="迁移包路径")
    p.add_argument("--hashes", action="store_true", help="显示每个文件的 SHA-256")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_structured(p)
    p.set_defaults(handler=cmd_list)


def _add_verify(sub) -> None:
    p = sub.add_parser("verify", help="校验迁移包完整性", description="校验包内容与清单一是否一致")
    p.add_argument("input", help="迁移包路径")
    p.add_argument("--deep", action="store_true", help="额外校验归档字节哈希（需旁挂 .sha256）")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_structured(p)
    p.set_defaults(handler=cmd_verify)


def _add_doctor(sub) -> None:
    p = sub.add_parser("doctor", help="体检配置健康状况", description="检查配置中的潜在问题")
    p.add_argument("target", help="ClassIsland 数据目录或迁移包路径")
    p.add_argument(
        "--strict",
        action="store_true",
        help="严格模式：警告视为错误，且发现错误时以非零退出码结束（默认只报告，总是返回 0）",
    )
    p.add_argument("--no-credentials", action="store_true", help="跳过敏感信息检查")
    p.add_argument("--no-paths", action="store_true", help="跳过绝对路径检查")
    p.add_argument("--no-plugins", action="store_true", help="跳过插件检查")
    p.add_argument("--redact", action="store_true", help="就地脱敏检出的敏感字段")
    p.add_argument("--target-version", default=None, help="用于插件兼容性对比的目标 ClassIsland 版本")
    p.add_argument("--json", action="store_true", dest="as_json", help="以 JSON 输出")
    _add_structured(p)
    p.set_defaults(handler=cmd_doctor)


def _add_download(sub) -> None:
    p = sub.add_parser("download", help="下载 ClassIsland 本体", description="下载并安装 ClassIsland")
    p.add_argument(
        "-d",
        "--dir",
        default=None,
        help="目标安装目录（--list-versions 时可省略）",
    )
    p.add_argument("--ci-version", default=None, help="指定版本，如 2.1.0.1 或 1.7.0.1")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--ci1x", action="store_true", help="强制使用 1.x 数据源")
    group.add_argument("--ci2x", action="store_true", help="强制使用 2.x 数据源")
    p.add_argument("--source", default=None, help="指定数据源 (distribution/github/disturb/disturb-net6)")
    p.add_argument("--list-versions", action="store_true", help="只列出可用版本后退出")
    p.add_argument("-y", "--yes", action="store_true", help="跳过所有确认")
    p.add_argument("--spoof-ua", action="store_true", help="下载时使用浏览器 User-Agent")
    p.add_argument("--no-verify", action="store_true", help="跳过哈希校验")
    _add_structured(p)
    p.set_defaults(handler=cmd_download)


# --------------------------------------------------------------------------- #
# 命令实现
# --------------------------------------------------------------------------- #


def cmd_pack(args: argparse.Namespace) -> int:
    from .commands import pack_command

    return pack_command(args)


def cmd_unpack(args: argparse.Namespace) -> int:
    from .commands import unpack_command

    return unpack_command(args)


def cmd_list(args: argparse.Namespace) -> int:
    from .commands import list_command

    return list_command(args)


def cmd_verify(args: argparse.Namespace) -> int:
    from .commands import verify_command

    return verify_command(args)


def cmd_doctor(args: argparse.Namespace) -> int:
    from .commands import doctor_command

    return doctor_command(args)


def cmd_download(args: argparse.Namespace) -> int:
    from .commands import download_command

    return download_command(args)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """CLI 入口。``result=`` 事件在此**唯一**输出。"""
    parser = build_parser()
    args = parser.parse_args(argv)

    structured.set_structured(getattr(args, "structured", False))
    logui.set_quiet(False)

    exit_code = EXIT_OK
    result_kind = "success"
    message: str | None = None

    try:
        code = args.handler(args)
        exit_code = int(code or EXIT_OK)
        if exit_code != EXIT_OK:
            result_kind = "failed"

    except KeyboardInterrupt:
        logui.err("已中断")
        exit_code, result_kind = EXIT_CANCELLED, "cancelled"

    except CancelledError as exc:
        logui.out(f"已取消。{exc.message}")
        exit_code, result_kind, message = exc.exit_code, "cancelled", exc.message

    except CIPackerError as exc:
        logui.err(exc.message)
        if exc.hint:
            logui.out(f"提示: {exc.hint}")
        exit_code, result_kind, message = exc.exit_code, "failed", exc.message

    except SystemExit as exc:  # 由 argparse 或命令内部触发
        code = exc.code if isinstance(exc.code, int) else EXIT_FAILURE
        exit_code = code
        if code != EXIT_OK:
            result_kind = "failed"
        # 不再 emit：交给下面的统一出口

    except Exception as exc:  # noqa: BLE001 - 兜底，保证事件恰好一次
        logui.err(f"未预期的错误: {exc}")
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        exit_code, result_kind, message = EXIT_FAILURE, "failed", str(exc)

    structured.result(result_kind, exit_code, message)
    return exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
