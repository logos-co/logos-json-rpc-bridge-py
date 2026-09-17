"""Deterministic text: Python literals, docstrings, and Doxygen code blocks.

Nothing here depends on the running Python's ``repr`` or Unicode database, so
Python 3.10 and 3.13 write byte-identical files.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any, Final

# Characters escaped in string literals even though they are not ASCII controls:
# C1 controls, invisible formatting, line/paragraph separators, BOM, surrogates.
_ESCAPED_RANGES: Final = (
    (0x0000, 0x001F), (0x007F, 0x009F), (0x00AD, 0x00AD), (0x200B, 0x200F), (0x2028, 0x202E),
    (0x2060, 0x206F), (0xFEFF, 0xFEFF), (0xD800, 0xDFFF), (0xFFF9, 0xFFFB),
)
_SHORT_ESCAPES: Final = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
_CODE_START: Final = re.compile(r"^\s*@code(?:\{\.?([A-Za-z0-9_+-]*)\})?\s*$")
_CODE_END: Final = re.compile(r"^\s*@endcode\s*$")


def _needs_escape(char: str) -> bool:
    point = ord(char)
    return any(lo <= point <= hi for lo, hi in _ESCAPED_RANGES)


def _escape_char(char: str) -> str:
    if char in _SHORT_ESCAPES:
        return _SHORT_ESCAPES[char]
    point = ord(char)
    if point <= 0xFF:
        return f"\\x{point:02x}"
    if point <= 0xFFFF:
        return f"\\u{point:04x}"
    return f"\\U{point:08x}"


def py_str(text: str) -> str:
    """A double-quoted Python string literal."""
    out = ['"']
    for char in text:
        if char == '"':
            out.append('\\"')
        elif char in _SHORT_ESCAPES or _needs_escape(char):
            out.append(_escape_char(char))
        else:
            out.append(char)
    out.append('"')
    return "".join(out)


class RawCode(str):
    """Emitted verbatim by :func:`py_literal` (a name, not a string)."""


def _scalar(value: Any) -> str:
    if isinstance(value, RawCode):
        return str.__str__(value)
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return py_str(value)
    raise TypeError(f"no literal for {type(value).__name__}")


def _inline(value: Any) -> str:
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{py_str(k)}: {_inline(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_inline(v) for v in value) + "]"
    return _scalar(value)


def py_literal(value: Any, indent: int = 0, column: int | None = None, width: int = 110) -> str:
    """A Python literal for JSON-like data, keys in the given order.

    ``indent`` is the enclosing block's indentation and ``column`` where the literal
    starts; a value that does not fit on that line gets one entry per line.
    """
    flat = _inline(value)
    start = indent if column is None else column
    if start + len(flat) <= width or not isinstance(value, (Mapping, list, tuple)) or not value:
        return flat
    pad = " " * (indent + 4)
    if isinstance(value, Mapping):
        items = []
        for key, item in value.items():
            head = f"{pad}{py_str(key)}: "
            items.append(head + py_literal(item, indent + 4, len(head), width - 1) + ",")
        return "{\n" + "\n".join(items) + "\n" + " " * indent + "}"
    items = [pad + py_literal(item, indent + 4, indent + 4, width - 1) + "," for item in value]
    return "[\n" + "\n".join(items) + "\n" + " " * indent + "]"


def split_code_blocks(text: str) -> Iterator[tuple[str, str | None, str]]:
    """``(kind, language, body)`` chunks: ``text`` and Doxygen ``@code``/``@endcode`` blocks."""
    prose: list[str] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        start = _CODE_START.match(lines[index])
        if start is None:
            prose.append(lines[index])
            index += 1
            continue
        end = next((j for j in range(index + 1, len(lines)) if _CODE_END.match(lines[j])), None)
        if end is None:  # an unterminated block stays prose
            prose.append(lines[index])
            index += 1
            continue
        if prose:
            yield "text", None, "\n".join(prose)
            prose = []
        yield "code", start.group(1) or None, "\n".join(lines[index + 1:end])
        index = end + 1
    if prose:
        yield "text", None, "\n".join(prose)


def _trim(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip("\n").split("\n")).strip("\n")


def summary_and_body(description: str) -> tuple[str, str]:
    """The first line (the summary) and the rest."""
    text = description.strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    return first.strip(), rest.strip("\n")


def rest_text(description: str) -> str:
    """Doxygen-flavoured text as reStructuredText: ``@code`` blocks become literal blocks."""
    parts: list[str] = []
    for kind, _language, body in split_code_blocks(description):
        if kind == "text":
            trimmed = _trim(body)
            if trimmed:
                parts.append(trimmed)
        else:
            code = "\n".join(("    " + line) if line.strip() else "" for line in body.split("\n"))
            parts.append("::\n\n" + code.rstrip())
    return "\n\n".join(parts)


def markdown_text(description: str) -> str:
    """The same text for Markdown: ``@code{.json}`` becomes a fenced block."""
    parts: list[str] = []
    for kind, language, body in split_code_blocks(description):
        if kind == "text":
            trimmed = _trim(body)
            if trimmed:
                parts.append(trimmed)
        else:
            fence = "~~~~" if "```" in body else "```"
            parts.append(f"{fence}{language or ''}\n{body}\n{fence}")
    return "\n\n".join(parts)


def wrap_line(line: str, width: int) -> list[str]:
    """Split an over-long prose line at spaces (indented lines are left alone)."""
    if len(line) <= width or line[:1].isspace():
        return [line]
    out: list[str] = []
    current = ""
    for word in line.split(" "):
        if current and len(current) + 1 + len(word) > width:
            out.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    out.append(current)
    return out


def docstring(text: str, indent: int, width: int = 100) -> str:
    """A triple-quoted docstring body for ``text``, indented for its block."""
    text = "\n".join(part for line in text.split("\n") for part in wrap_line(line, width - indent))
    pad = " " * indent
    escaped = []
    for char in text:
        if char == "\\":
            escaped.append("\\\\")
        elif char == "\n":
            escaped.append("\n")
        elif char == "\t" or _needs_escape(char):
            escaped.append(_escape_char(char))
        else:
            escaped.append(char)
    body = "".join(escaped).replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    lines = body.split("\n")
    rendered = [lines[0]] + [(pad + line) if line else "" for line in lines[1:]]
    if len(lines) == 1:
        return f'{pad}"""{rendered[0]}"""'
    return f'{pad}"""' + "\n".join(rendered) + f'\n{pad}"""'


def md_code(text: str) -> str:
    """Inline Markdown code, safe for backticks and table pipes (GFM ``\\|``)."""
    fence = "``" if "`" in text else "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text.replace('|', chr(92) + '|')}{pad}{fence}"
