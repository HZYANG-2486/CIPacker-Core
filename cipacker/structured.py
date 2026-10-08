"""结构化事件流输出层（GUI 集成契约）。

所有结构化事件以 ``[STRUCTURED] `` 前缀输出到 **stderr**，每行一条，
格式为空格分隔的 ``key=value`` 对。

本模块是 GUI_INTEGRATION.md 中描述的对外契约的**唯一实现点**：
- 人类模式（STRUCTURED_MODE=False）：emit() 静默，不产生任何输出。
- 结构化模式（STRUCTURED_MODE=True）：emit() 写 stderr。

新增事件类型只允许**追加**，不得修改既有事件的字段语义，
以保证旧版 GUI 外壳（忽略未知字段）仍然可用。
"""

from __future__ import annotations

import sys
from typing import Any

PREFIX = "[STRUCTURED] "

#: 是否为结构化模式。由 CLI 入口统一设置。
STRUCTURED_MODE: bool = False


def _format_value(value: Any) -> str:
    """把值渲染成可嵌入 ``key=value`` 的字符串。

    含空格、引号、换行等会破坏「空格分隔」约定的值，统一用双引号包裹，
    内部的双引号与反斜杠转义（与 :func:`parse_line` 对称）。
    这样 GUI 外壳才能安全地按空白切分。
    """
    text = str(value)
    if text == "" or any(ch in text for ch in ' \t\r\n"\\'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"')
        escaped = escaped.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        return f'"{escaped}"'
    return text


def set_structured(enabled: bool) -> None:
    """启用或关闭结构化输出模式。"""
    global STRUCTURED_MODE
    STRUCTURED_MODE = bool(enabled)


def is_structured() -> bool:
    return STRUCTURED_MODE


def emit(*pairs: Any, **kwargs: Any) -> None:
    """输出一条结构化事件。

    用法::

        emit("stage=download")
        emit("stage=download", "progress=0.0")
        emit(stage="download", progress=0.0)
        emit("level=warning", message="文件不存在")

    值为 ``None`` 的关键字参数会被跳过（便于可选字段）。
    """
    if not STRUCTURED_MODE:
        return

    parts = [str(p) for p in pairs]
    for key, value in kwargs.items():
        if value is None:
            continue
        parts.append(f"{key}={_format_value(value)}")

    if not parts:
        return

    print(PREFIX + " ".join(parts), file=sys.stderr, flush=True)


def stage(name: str, **extra: Any) -> None:
    """阶段切换事件。"""
    emit(f"stage={name}", **extra)


def info(**fields: Any) -> None:
    emit(level="info", **fields)


def warning(message: str, **fields: Any) -> None:
    emit(level="warning", message=message, **fields)


def error(message: str, **fields: Any) -> None:
    emit(level="error", message=message, **fields)


def success(**fields: Any) -> None:
    emit(level="success", **fields)


def finding(rule_id: str, severity: str, message: str, location: str = "") -> None:
    """体检发现事件（v3 新增，旧外壳忽略即可）。"""
    emit(
        level="finding",
        rule=rule_id,
        severity=severity,
        message=message,
        location=location or None,
    )


def option_list(count: int, stage_name: str) -> None:
    """标记一个选项列表开始。"""
    emit(level="options", stage=stage_name, count=count)


def option(index: int, value: str, **fields: Any) -> None:
    """输出列表中的单个选项。"""
    emit(level="option", index=index, value=value, **fields)


def progress(percent: float, current: int | None = None, total: int | None = None) -> None:
    """下载进度事件。"""
    emit(progress=f"{percent:.1f}", current=current, total=total)


def result(kind: str, exit_code: int, message: str | None = None) -> None:
    """最终结果事件。必须在整个进程生命周期内**恰好输出一次**。"""
    emit(f"result={kind}", f"exit_code={exit_code}", message=message)


def ask(prompt: str, default: str | None = None, choices: list[str] | None = None) -> str:
    """询问包装函数。

    人类模式：直接调用 ``input()``。
    结构化模式：先 emit ``level=ask`` 事件，再调用 ``input()``。
    GUI 外壳可通过 stdin 注入回答，或由 CLI 提供自动应答。

    在实际 CLI 流程中，建议优先使用 :class:`cipacker.logui.UserPrompt`，
    它同时负责人类可读文案与结构化事件，并在无交互环境下自动选择默认值。
    """
    if STRUCTURED_MODE:
        fields: dict[str, Any] = {"level": "ask", "message": prompt, "default": default}
        if choices:
            fields["choices"] = ",".join(choices)
        emit(**fields)

    try:
        return input(prompt)
    except EOFError:
        return default or ""


def parse_line(line: str) -> dict[str, str]:
    """解析一行结构化事件为字段字典（供测试与 GUI 外壳参考实现）。

    与 :func:`emit` 对称：支持双引号包裹的值，并还原转义序列。
    """
    if not line.startswith(PREFIX):
        return {}
    payload = line[len(PREFIX) :].strip()

    fields: dict[str, str] = {}
    i, n = 0, len(payload)
    while i < n:
        # 跳过空白
        while i < n and payload[i] in " \t":
            i += 1
        if i >= n:
            break

        # 读取 key
        key_start = i
        while i < n and payload[i] not in "= \t":
            i += 1
        key = payload[key_start:i]
        if i >= n or payload[i] != "=":
            continue
        i += 1  # 跳过 '='

        # 读取 value（可能带引号）
        if i < n and payload[i] == '"':
            i += 1
            buf: list[str] = []
            while i < n:
                ch = payload[i]
                if ch == "\\" and i + 1 < n:
                    nxt = payload[i + 1]
                    buf.append(
                        {"n": "\n", "r": "\r", "t": "\t"}.get(nxt, nxt)
                    )
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                buf.append(ch)
                i += 1
            value = "".join(buf)
        else:
            val_start = i
            while i < n and payload[i] not in " \t":
                i += 1
            value = payload[val_start:i]

        if key:
            fields[key] = value

    return fields
