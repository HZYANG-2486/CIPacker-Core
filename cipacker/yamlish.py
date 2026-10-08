"""零依赖的 YAML 子集解析器。

ClassIsland 的插件元数据 ``manifest.yml`` 使用 YamlDotNet 反序列化，
结构上只用到 YAML 的一个很小的子集：

- 映射（``key: value``）
- 缩进序列（``- item`` 与 ``- key: value``）
- 内联序列（``[a, b, c]``）
- 引号字符串（单引号 / 双引号）
- 块标量（``|``、``>``）
- 布尔 / 整数 / null 字面量
- 注释（``#``）

本模块实现该子集的解析器。若环境中存在 PyYAML，则优先使用它
（与 tqdm 同样的「可选依赖优雅降级」策略），保证结果一致的同时
在不装 PyYAML 的机器上也能正确解析插件依赖——这正是旧版
``parse_yaml_simple``（仅按单个 ``key: value`` 行切分）无法做到的地方。
"""

from __future__ import annotations

import re
from typing import Any

try:  # pragma: no cover - 取决于运行环境
    import yaml as _pyyaml

    PYYAML_AVAILABLE = True
except ImportError:  # pragma: no cover
    _pyyaml = None  # type: ignore[assignment]
    PYYAML_AVAILABLE = False


class YamlSubsetError(ValueError):
    """YAML 子集解析失败。"""


_NULL_TOKENS = {"~", "null", "Null", "NULL", ""}
_TRUE_TOKENS = {"true", "True", "TRUE", "yes", "Yes", "on", "On"}
_FALSE_TOKENS = {"false", "False", "FALSE", "no", "No", "off", "Off"}

_INT_RE = re.compile(r"^[+-]?\d+$")
_FLOAT_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")


# --------------------------------------------------------------------------- #
# 标量解析
# --------------------------------------------------------------------------- #


def _strip_comment(text: str) -> str:
    """去掉行尾注释（尊重引号内的 ``#``）。"""
    out_chars: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote:
            out_chars.append(char)
            if char == "\\" and quote == '"' and index + 1 < len(text):
                out_chars.append(text[index + 1])
                index += 2
                continue
            if char == quote:
                quote = None
        else:
            if char in ("'", '"'):
                quote = char
                out_chars.append(char)
            elif char == "#" and (not out_chars or out_chars[-1] in (" ", "\t")):
                break
            else:
                out_chars.append(char)
        index += 1
    return "".join(out_chars)


def _unquote(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == "'":
            return inner.replace("''", "'")
        return (
            inner.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )
    return text


def _parse_scalar(text: str) -> Any:
    """把标量文本转换为 Python 值。"""
    raw = text.strip()
    if raw == "":
        return None

    # 引号包裹 → 纯字符串
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        return _unquote(raw)

    if raw in _NULL_TOKENS:
        return None
    if raw in _TRUE_TOKENS:
        return True
    if raw in _FALSE_TOKENS:
        return False

    # 内联序列 [a, b, c]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in _split_inline(inner)]

    # 内联映射 {a: 1, b: 2}
    if raw.startswith("{") and raw.endswith("}"):
        inner = raw[1:-1].strip()
        if not inner:
            return {}
        result: dict[str, Any] = {}
        for item in _split_inline(inner):
            if ":" in item:
                key, value = item.split(":", 1)
                result[_unquote(key.strip())] = _parse_scalar(value)
            else:
                result[_unquote(item.strip())] = None
        return result

    if _INT_RE.match(raw):
        try:
            return int(raw)
        except ValueError:  # pragma: no cover
            return raw

    if _FLOAT_RE.match(raw) and any(c in raw for c in ".eE"):
        try:
            return float(raw)
        except ValueError:  # pragma: no cover
            return raw

    return raw


def _split_inline(text: str) -> list[str]:
    """按逗号切分内联序列（尊重引号与嵌套括号）。"""
    items: list[str] = []
    depth = 0
    quote: str | None = None
    current: list[str] = []

    for char in text:
        if quote:
            current.append(char)
            if char == quote:
                quote = None
            continue
        if char in ("'", '"'):
            quote = char
            current.append(char)
        elif char in "[{":
            depth += 1
            current.append(char)
        elif char in "]}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0:
            items.append("".join(current).strip())
            current = []
        else:
            current.append(char)

    tail = "".join(current).strip()
    if tail:
        items.append(tail)
    return items


# --------------------------------------------------------------------------- #
# 缩进解析器
# --------------------------------------------------------------------------- #


class _Line:
    __slots__ = ("indent", "content", "raw", "number")

    def __init__(self, indent: int, content: str, raw: str, number: int) -> None:
        self.indent = indent
        self.content = content
        self.raw = raw
        self.number = number

    def __repr__(self) -> str:  # pragma: no cover
        return f"_Line(indent={self.indent}, content={self.content!r})"


def _preprocess(text: str) -> list[_Line]:
    """把原始文本切成带缩进信息的逻辑行，处理块标量。"""
    lines: list[_Line] = []
    raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    index = 0

    while index < len(raw_lines):
        raw = raw_lines[index]
        index += 1

        if not raw.strip() or raw.lstrip().startswith("#"):
            continue

        stripped_comment = _strip_comment(raw)
        if not stripped_comment.strip():
            continue

        expanded = stripped_comment.replace("\t", "    ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        content = expanded.strip()
        line = _Line(indent, content, raw, index)

        # 块标量：`key: |` 或 `key: >`
        block_match = re.match(r"^(.*?):\s*([|>])([-+]?\d*)\s*$", content)
        if block_match:
            key_part = block_match.group(1).strip()
            style = block_match.group(2)
            body: list[str] = []
            body_indent: int | None = None
            while index < len(raw_lines):
                candidate = raw_lines[index]
                if not candidate.strip():
                    body.append("")
                    index += 1
                    continue
                cand_indent = len(candidate.replace("\t", "    ")) - len(
                    candidate.replace("\t", "    ").lstrip(" ")
                )
                if cand_indent <= indent:
                    break
                if body_indent is None:
                    body_indent = cand_indent
                body.append(candidate[body_indent:] if len(candidate) > body_indent else "")
                index += 1

            while body and not body[-1].strip():
                body.pop()

            value = "\n".join(body) if style == "|" else " ".join(
                part.strip() for part in body if part.strip()
            )
            folded = f"{key_part}: {value!r}" if value else f"{key_part}:"
            lines.append(_Line(indent, folded, raw, line.number))
            continue

        lines.append(line)

    return lines


class _Parser:
    """基于缩进栈的递归下降解析器。"""

    def __init__(self, lines: list[_Line]) -> None:
        self.lines = lines
        self.pos = 0

    def parse(self) -> Any:
        if not self.lines:
            return {}
        value = self._parse_block(self.lines[0].indent)
        return value

    # -- 块 ---------------------------------------------------------------- #

    def _parse_block(self, indent: int) -> Any:
        if self.pos >= len(self.lines):
            return None

        line = self.lines[self.pos]

        if line.content.startswith("- "):
            return self._parse_sequence(indent)
        if line.content == "-":
            return self._parse_sequence(indent)
        if self._is_mapping_line(line.content):
            return self._parse_mapping(indent)

        # 裸标量
        self.pos += 1
        return _parse_scalar(line.content)

    def _is_mapping_line(self, content: str) -> bool:
        if content.startswith("- "):
            return False
        match = re.match(r"^(\"[^\"]*\"|'[^']*'|[^:\s][^:]*?)\s*:(?:\s|$)", content)
        return match is not None

    def _split_key_value(self, content: str) -> tuple[str, str]:
        """在第一个「键冒号」处切分（跳过引号内的冒号）。"""
        quote: str | None = None
        for index, char in enumerate(content):
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char == ":":
                next_char = content[index + 1 : index + 2]
                if next_char in ("", " ", "\t"):
                    key = content[:index].strip()
                    value = content[index + 1 :].strip()
                    return _unquote(key), value
        raise YamlSubsetError(f"无法解析的映射行: {content!r}")

    def _parse_mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}

        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent:
                break
            if line.indent > indent:
                # 属于上一个键的嵌套块，但未被消费——跳过以免死循环
                self.pos += 1
                continue
            if not self._is_mapping_line(line.content):
                break

            key, value_text = self._split_key_value(line.content)
            self.pos += 1

            if value_text == "":
                # 嵌套块（映射或序列）
                nested = self._parse_child(indent)
                result[key] = nested
            else:
                result[key] = _parse_scalar(value_text)

        return result

    def _parse_child(self, parent_indent: int) -> Any:
        """解析父键下方缩进更深的子块。"""
        if self.pos >= len(self.lines):
            return None
        line = self.lines[self.pos]
        if line.indent <= parent_indent:
            return None
        return self._parse_block(line.indent)

    def _parse_sequence(self, indent: int) -> list[Any]:
        items: list[Any] = []

        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.indent < indent:
                break
            if line.indent > indent:
                self.pos += 1
                continue
            if not (line.content == "-" or line.content.startswith("- ")):
                break

            body = line.content[1:].strip()
            self.pos += 1

            if not body:
                # `-` 之后换行，子块在下一层缩进
                items.append(self._parse_child(indent))
                continue

            if self._is_mapping_line(body):
                # `- key: value` 形式的序列项（可跨多行）
                item = self._parse_inline_mapping_item(body, indent)
                items.append(item)
            else:
                items.append(_parse_scalar(body))

        return items

    def _parse_inline_mapping_item(self, first_body: str, seq_indent: int) -> dict[str, Any]:
        """解析 ``- key: value`` 起始的映射项，合并后续同缩进的键。"""
        item: dict[str, Any] = {}
        key, value_text = self._split_key_value(first_body)

        if value_text == "":
            # 形如 `- id:` 后跟更深缩进
            child = self._parse_child(seq_indent + 1)
            item[key] = child
        else:
            item[key] = _parse_scalar(value_text)

        # 后续键相对序列标记缩进更深，但相对本项是同级
        if self.pos < len(self.lines):
            next_line = self.lines[self.pos]
            item_indent = next_line.indent
            if item_indent > seq_indent:
                extra = self._parse_mapping(item_indent)
                if isinstance(extra, dict):
                    item.update(extra)

        return item


# --------------------------------------------------------------------------- #
# 公开接口
# --------------------------------------------------------------------------- #


def load(text: str, *, use_pyyaml: bool | None = None) -> Any:
    """解析 YAML 文本。

    :param text: YAML 源文本。
    :param use_pyyaml: 强制指定是否使用 PyYAML（``None`` = 自动）。
    :raises YamlSubsetError: 解析失败。
    """
    if use_pyyaml is None:
        use_pyyaml = PYYAML_AVAILABLE

    if use_pyyaml and _pyyaml is not None:
        try:
            return _pyyaml.safe_load(text) or {}
        except Exception as exc:  # noqa: BLE001 - 回退到内置解析器
            # 不直接失败：退到内置实现，尽量拿到结果
            try:
                return _Parser(_preprocess(text)).parse()
            except Exception:
                raise YamlSubsetError(f"YAML 解析失败: {exc}") from exc

    try:
        return _Parser(_preprocess(text)).parse()
    except YamlSubsetError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise YamlSubsetError(f"YAML 解析失败: {exc}") from exc


def load_mapping(text: str, *, use_pyyaml: bool | None = None) -> dict[str, Any]:
    """解析 YAML 并确保结果是映射。"""
    data = load(text, use_pyyaml=use_pyyaml)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise YamlSubsetError(f"期望映射，实际得到 {type(data).__name__}")
    return data


def dumps_simple(data: dict[str, Any], indent: int = 0) -> str:
    """把简单的嵌套 dict/list 序列化为 YAML 文本（用于测试与调试）。"""
    lines: list[str] = []
    pad = "  " * indent

    for key, value in data.items():
        if isinstance(value, dict) and value:
            lines.append(f"{pad}{key}:")
            lines.append(dumps_simple(value, indent + 1))
        elif isinstance(value, list) and value:
            lines.append(f"{pad}{key}:")
            for item in value:
                if isinstance(item, dict):
                    inner = dumps_simple(item, indent + 2).splitlines()
                    if inner:
                        lines.append(f"{pad}  - {inner[0].strip()}")
                        lines.extend(inner[1:])
                    else:  # pragma: no cover
                        lines.append(f"{pad}  -")
                else:
                    lines.append(f"{pad}  - {_format_scalar(item)}")
        else:
            lines.append(f"{pad}{key}: {_format_scalar(value)}")

    return "\n".join(lines)


def _format_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "" or any(c in text for c in ":#[]{}\n'\""):
        escaped = text.replace("'", "''")
        return f"'{escaped}'"
    return text
