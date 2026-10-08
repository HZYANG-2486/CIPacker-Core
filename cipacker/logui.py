"""人类可读输出与用户交互层。

与 :mod:`cipacker.structured` 正交：
- 本模块负责 stdout 上给人看的文案；
- 结构化事件由 structured 模块负责。

:class:`UserPrompt` 是二者的统一入口，保证「同一处交互」既产生人类文案，
也产生结构化事件，且在无 `--yes` 的无交互环境下按默认值自动决策而不挂起。
"""

from __future__ import annotations

import sys
from typing import Any

from .structured import emit, is_structured

# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #

_QUIET = False


def set_quiet(quiet: bool) -> None:
    global _QUIET
    _QUIET = bool(quiet)


def is_quiet() -> bool:
    return _QUIET


def out(message: str = "") -> None:
    """标准输出（人类可读）。"""
    if not _QUIET:
        print(message)


def raw(message: str) -> None:
    """不换行的标准输出，用于进度行。"""
    if not _QUIET:
        print(message, end="", flush=True)


def warn(message: str) -> None:
    """警告信息：走 stderr，正常输出流不受污染。"""
    if not _QUIET:
        print(f"警告: {message}", file=sys.stderr)


def err(message: str) -> None:
    """错误信息：走 stderr。"""
    print(f"错误: {message}", file=sys.stderr)


def section(title: str) -> None:
    """分节标题。"""
    if not _QUIET:
        print(f"\n=== {title} ===")


def human_size(num_bytes: float) -> str:
    """把字节数格式化为人类可读字符串。"""
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


# --------------------------------------------------------------------------- #
# 交互
# --------------------------------------------------------------------------- #


class UserPrompt:
    """交互式提问的统一入口。

    :param assume_yes: 为 True 时所有确认题自动回答「是」，跳过交互。
    :param interactive: 为 False 时（无 TTY / GUI 未注入 stdin）按默认值决策。
    """

    def __init__(self, assume_yes: bool = False, interactive: bool | None = None) -> None:
        self.assume_yes = assume_yes
        if interactive is None:
            interactive = sys.stdin is not None and sys.stdin.isatty()
        self.interactive = interactive

    # -- 确认题 ------------------------------------------------------------ #

    def confirm(self, question: str, default: bool = False) -> bool:
        """询问是/否。返回用户选择。"""
        default_token = "Y/n" if default else "y/N"
        prompt_text = f"{question} ({default_token}) "

        if self.assume_yes:
            emit(
                level="info",
                auto="true",
                message=question,
                selected="yes",
            )
            out(f"{question} -> 是（--yes 自动确认）")
            return True

        if not self.interactive:
            emit(level="info", auto="true", message=question, selected="no")
            out(f"{question} -> {'是' if default else '否'}（无交互输入，采用默认值）")
            return default

        answer = self._read(
            prompt_text, message=question, default="y" if default else "n"
        )
        return answer.strip().lower() in ("y", "yes")

    # -- 单选题 ------------------------------------------------------------ #

    def choose(
        self,
        question: str,
        options: list[tuple[str, str]],
        default_index: int = 0,
    ) -> str | None:
        """从候选中单选。

        :param options: ``[(value, label), ...]``
        :param default_index: 无交互时的默认下标（0 表示第一项，-1 表示取消）
        :return: 选中的 value；用户取消时返回 None。
        """
        if not options:
            return None

        values = [value for value, _ in options]

        if self.assume_yes or not self.interactive:
            if default_index < 0 or default_index >= len(options):
                emit(
                    level="info",
                    auto="true",
                    message=question,
                    selected="cancel",
                )
                return None
            chosen = options[default_index][0]
            emit(
                level="info",
                auto="true",
                message=question,
                selected=chosen,
            )
            out(f"自动选择: {options[default_index][1]}")
            return chosen

        from .structured import option as emit_option
        from .structured import option_list

        option_list(len(options), "choose")
        for index, (value, label) in enumerate(options, 1):
            emit_option(index, value, label=label)

        out("")
        for index, (_, label) in enumerate(options, 1):
            out(f"  {index}. {label}")

        while True:
            answer = self._read(
                f"{question} (1-{len(options)}): ",
                message=question,
                default=str(default_index + 1) if default_index >= 0 else "0",
                choices=values,
            ).strip()
            if answer in ("", "0"):
                return None
            try:
                index = int(answer)
            except ValueError:
                out("无效输入，请重试。")
                continue
            if 1 <= index <= len(options):
                return options[index - 1][0]
            out("无效输入，请重试。")

    # -- 内部 -------------------------------------------------------------- #

    def _read(
        self,
        prompt_text: str,
        default: str | None = None,
        message: str | None = None,
        choices: list[str] | None = None,
    ) -> str:
        fields: dict[str, Any] = {
            "level": "ask",
            "message": message or prompt_text,
            "default": default,
        }
        if choices:
            fields["choices"] = ",".join(choices)
        if is_structured():
            emit(**fields)

        try:
            return input(prompt_text)
        except EOFError:
            return default or ""
        except KeyboardInterrupt:
            raise
