"""LOOT metadata conditions, e.g. ``active("A.esp") and not file("SKSE/Plugins/x.dll")``.

Grammar (from LOOT's documentation)::

    condition := compound ("or" compound)*
    compound  := function ("and" function)*
    function  := ["not"] (call | "(" condition ")")

``evaluate`` returns None for conditions using functions it can't evaluate, so
callers can skip that metadata instead of guessing.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from modmanager.core.fomod import pe_file_version
from modmanager.core.plugins import is_plugin_name, read_header

# Unquoted arguments are CRC checksums in hex, e.g. checksum("A.esp", 3D8CB8A2).
TOKEN = re.compile(r'\s*(?:(?P<str>"[^"]*")|(?P<op>==|!=|<=|>=|<|>)|(?P<punct>[(),])'
                   r'|(?P<word>[A-Za-z_][A-Za-z0-9_]*)|(?P<num>[0-9][0-9A-Fa-f.]*))')
REGEX_CHARS = set(":\\*?|")
_CRC_CACHE: dict[str, int] = {}


class Unsupported(Exception):
    pass


@dataclass
class Context:
    """What conditions can ask about the current setup."""

    data_dir: Path
    game_dir: Path
    present: set[str]                  # Lower-cased Data-relative paths that exist (deployed or planned)
    active: set[str]                   # Lower-cased active plugin names
    masters: set[str] = field(default_factory=set)   # Lower-cased plugins with the master flag
    resolve: Callable[[str], Path | None] = lambda _rel: None  # Data-relative path -> real file

    # LOOT paths always use "/" separators; a backslash is a regex escape.
    def exists(self, rel: str) -> bool:
        if rel.startswith("../"):
            return (self.game_dir / rel[3:]).exists() or _ci_exists(self.game_dir, rel[3:])
        name = rel.rsplit("/", 1)[-1]
        if REGEX_CHARS & set(name):
            folder = rel.rsplit("/", 1)[0].lower() + "/" if "/" in rel else ""
            pattern = re.compile(name, re.I)
            return any(k.startswith(folder) and "/" not in k[len(folder):] and pattern.fullmatch(k[len(folder):])
                       for k in self.present)
        return rel.lower() in self.present

    def path(self, rel: str) -> Path | None:
        if rel.startswith("../"):
            p = self.game_dir / rel[3:]
            return p if p.exists() else None
        return self.resolve(rel)

    def crc(self, rel: str) -> int | None:
        path = self.path(rel)
        if path is None or not path.is_file():
            return None
        st = path.stat()
        key = f"{path}:{st.st_size}:{st.st_mtime_ns}"
        if key not in _CRC_CACHE:
            value = 0
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    value = zlib.crc32(chunk, value)
            _CRC_CACHE[key] = value
        return _CRC_CACHE[key]

    def version(self, rel: str) -> tuple[int, ...] | None:
        path = self.path(rel)
        if path is None or not path.is_file():
            return None
        if is_plugin_name(path.name):
            header = read_header(path)
            return parse_version(header.description) if header else None
        return pe_file_version(path)


def _ci_exists(root: Path, rel: str) -> bool:
    from modmanager.core.fomod import resolve_ci

    return resolve_ci(root, rel) is not None


def parse_version(text: str) -> tuple[int, ...] | None:
    """Version from a plugin description, as LOOT does (e.g. "Version: 4.2.5")."""
    if not text:
        return None
    m = re.search(r"(?:version|ver\.?|v)\s*:?\s*(\d+(?:\.\d+)+|\d+)", text, re.I) or re.search(r"\d+(?:\.\d+)+", text)
    if not m:
        return None
    return tuple(int(x) for x in re.findall(r"\d+", m.group(m.lastindex or 0)))


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)) or (0,)


def _compare(actual: tuple[int, ...] | None, wanted: tuple[int, ...], op: str) -> bool:
    # A missing file counts as having the lowest possible version.
    a = actual or ()
    width = max(len(a), len(wanted))
    a = a + (0,) * (width - len(a))
    w = wanted + (0,) * (width - len(wanted))
    return {"==": a == w, "!=": a != w, "<": a < w, ">": a > w, "<=": a <= w, ">=": a >= w}[op]


class _Parser:
    def __init__(self, text: str, ctx: Context):
        self.tokens = self._tokenize(text)
        self.i = 0
        self.ctx = ctx

    @staticmethod
    def _tokenize(text: str) -> list[tuple[str, str]]:
        tokens, pos = [], 0
        text = text.strip()
        while pos < len(text):
            m = TOKEN.match(text, pos)
            if not m or m.end() == pos:
                raise Unsupported(f"cannot parse condition near: {text[pos:pos + 20]}")
            kind = m.lastgroup
            tokens.append((kind, m.group(kind)))
            pos = m.end()
        return tokens

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.i] if self.i < len(self.tokens) else None

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if tok is None or (value is not None and tok[1] != value):
            raise Unsupported(f"expected {value!r}")
        self.i += 1
        return tok

    def condition(self) -> bool:
        result = self.compound()
        while self.peek() and self.peek()[1] == "or":
            self.take("or")
            rhs = self.compound()
            result = result or rhs
        return result

    def compound(self) -> bool:
        result = self.function()
        while self.peek() and self.peek()[1] == "and":
            self.take("and")
            rhs = self.function()
            result = result and rhs
        return result

    def function(self) -> bool:
        negate = False
        if self.peek() and self.peek()[1] == "not":
            self.take("not")
            negate = True
        if self.peek() and self.peek()[1] == "(":
            self.take("(")
            value = self.condition()
            self.take(")")
        else:
            value = self.call()
        return not value if negate else value

    def call(self) -> bool:
        kind, name = self.take()
        if kind != "word":
            raise Unsupported(f"unexpected {name!r}")
        self.take("(")
        args: list[str] = []
        while self.peek() and self.peek()[1] != ")":
            k, v = self.take()
            if k == "punct" and v == ",":
                continue
            args.append(v[1:-1] if k == "str" else v)
        self.take(")")
        return self.apply(name, args)

    def apply(self, name: str, args: list[str]) -> bool:
        ctx = self.ctx
        if name in ("file", "readable") and len(args) == 1:
            return ctx.exists(args[0])
        if name == "active" and len(args) == 1:
            return _matches_any(args[0], ctx.active) > 0
        if name == "many" and len(args) == 1:
            return _count_files(ctx, args[0]) > 1
        if name == "many_active" and len(args) == 1:
            return _matches_any(args[0], ctx.active) > 1
        if name == "is_master" and len(args) == 1:
            return args[0].lower() in ctx.masters
        if name == "checksum" and len(args) == 2:
            crc = ctx.crc(args[0])
            return crc is not None and crc == int(args[1], 16)
        if name in ("version", "product_version") and len(args) == 3:
            return _compare(ctx.version(args[0]), _version_tuple(args[1]), args[2])
        raise Unsupported(f"{name}()")


def _matches_any(pattern: str, names: set[str]) -> int:
    if REGEX_CHARS & set(pattern):
        rx = re.compile(pattern, re.I)
        return sum(1 for n in names if rx.fullmatch(n))
    return 1 if pattern.lower() in names else 0


def _count_files(ctx: Context, rel: str) -> int:
    folder, _, name = rel.rpartition("/")
    prefix = folder.lower() + "/" if folder else ""
    rx = re.compile(name, re.I)
    return sum(1 for k in ctx.present
               if k.startswith(prefix) and "/" not in k[len(prefix):] and rx.fullmatch(k[len(prefix):]))


def evaluate(condition: str | None, ctx: Context) -> bool | None:
    """True/False, or None when the condition uses something unsupported."""
    if not condition:
        return True
    try:
        parser = _Parser(condition, ctx)
        result = parser.condition()
        if parser.peek() is not None:
            raise Unsupported("trailing input")
        return result
    except (Unsupported, re.error, ValueError, KeyError):
        return None
