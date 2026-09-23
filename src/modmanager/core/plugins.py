"""Bethesda plugin (.esp/.esm/.esl) headers and load order bookkeeping."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

PLUGIN_EXTS = (".esp", ".esm", ".esl")
FLAG_MASTER = 0x1
FLAG_LIGHT = 0x200


def is_plugin_name(name: str) -> bool:
    return name.lower().endswith(PLUGIN_EXTS)


@dataclass
class PluginHeader:
    master_flag: bool = False
    light_flag: bool = False
    masters: list[str] = field(default_factory=list)
    author: str = ""
    description: str = ""


def read_header(path: Path) -> PluginHeader | None:
    """Read the TES4 record at the start of a plugin. Returns None if it isn't one."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(24)
            if len(head) < 24 or head[:4] != b"TES4":
                return None
            size, flags = struct.unpack_from("<II", head, 4)
            body = fh.read(min(size, 1 << 20))
    except OSError:
        return None

    header = PluginHeader(master_flag=bool(flags & FLAG_MASTER), light_flag=bool(flags & FLAG_LIGHT))
    pos = 0
    big_size: int | None = None
    while pos + 6 <= len(body):
        sub_type = body[pos : pos + 4]
        sub_size = struct.unpack_from("<H", body, pos + 4)[0]
        pos += 6
        if big_size is not None:
            sub_size, big_size = big_size, None
        data = body[pos : pos + sub_size]
        pos += sub_size
        if sub_type == b"XXXX" and len(data) >= 4:
            big_size = struct.unpack_from("<I", data)[0]
        elif sub_type == b"MAST":
            header.masters.append(_zstring(data))
        elif sub_type == b"CNAM":
            header.author = _zstring(data)
        elif sub_type == b"SNAM":
            header.description = _zstring(data)
    return header


def _zstring(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("cp1252", errors="replace")


@dataclass
class PluginEntry:
    name: str
    enabled: bool = True
    origin: str = ""          # Mod that provides the file, or "" for the base game Data folder.
    path: Path | None = None
    implicit: bool = False    # Always loaded by the engine; cannot be moved or disabled.
    is_master: bool = False   # Sorted into the master block (ESM flag, .esm or .esl).
    is_light: bool = False
    masters: list[str] = field(default_factory=list)
    missing_masters: list[str] = field(default_factory=list)
    late_masters: list[str] = field(default_factory=list)
    load_index: str = ""

    @property
    def kind(self) -> str:
        ext = Path(self.name).suffix.lower()
        if self.is_light:
            return "ESL" if ext == ".esl" else ("ESM+ESL" if self.is_master and ext == ".esm" else "ESPFE")
        if ext == ".esm":
            return "ESM"
        return "ESP (master)" if self.is_master else "ESP"


def read_order_file(path: Path) -> list[tuple[str, bool]]:
    """Read a profile ``plugins.txt`` in asterisk format: ``*Name`` active, ``Name`` inactive."""
    entries: list[tuple[str, bool]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return entries
    seen: set[str] = set()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        enabled = line.startswith("*")
        name = line.lstrip("*").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            entries.append((name, enabled))
    return entries


def format_order_file(entries: list[tuple[str, bool]]) -> str:
    lines = ["# Load order managed by Mod Manager. '*' marks active plugins."]
    lines += [("*" if enabled else "") + name for name, enabled in entries]
    return "\n".join(lines) + "\n"


def merge_saved_order(saved: list[tuple[str, bool]], present: list[str]) -> list[tuple[str, bool]]:
    """Order ``present`` plugin names by ``saved``; unknown ones are appended and enabled."""
    by_lower = {name.lower(): name for name in present}
    result: list[tuple[str, bool]] = []
    used: set[str] = set()
    for name, enabled in saved:
        key = name.lower()
        if key in by_lower and key not in used:
            used.add(key)
            result.append((by_lower[key], enabled))
    for name in present:
        if name.lower() not in used:
            used.add(name.lower())
            result.append((name, True))
    return result


def update_saved_order(
    saved: list[tuple[str, bool]], visible: list[tuple[str, bool]]
) -> list[tuple[str, bool]]:
    """Fold a reordered visible list back into the full saved list.

    Plugins that are currently absent (their mod is disabled) keep their slots so
    that re-enabling the mod restores their old position.
    """
    visible_keys = {name.lower() for name, _ in visible}
    queue = iter(visible)
    result: list[tuple[str, bool]] = []
    for name, enabled in saved:
        if name.lower() in visible_keys:
            nxt = next(queue, None)
            if nxt is not None:
                result.append(nxt)
        else:
            result.append((name, enabled))
    result.extend(queue)
    return result


def partition_masters(entries: list[PluginEntry]) -> list[PluginEntry]:
    """The engine always loads implicit plugins, then masters, then the rest."""
    implicit = [e for e in entries if e.implicit]
    masters = [e for e in entries if not e.implicit and e.is_master]
    others = [e for e in entries if not e.implicit and not e.is_master]
    return implicit + masters + others


def assign_load_indices(entries: list[PluginEntry], light_supported: bool) -> None:
    """Fill ``load_index``/``missing_masters``/``late_masters`` for the given order."""
    full = 0
    light = 0
    loaded: set[str] = set()
    known = {e.name.lower() for e in entries if e.enabled}
    for e in entries:
        e.missing_masters = [m for m in e.masters if m.lower() not in known] if e.enabled else []
        e.late_masters = (
            [m for m in e.masters if m.lower() in known and m.lower() not in loaded] if e.enabled else []
        )
        if not e.enabled:
            e.load_index = ""
            continue
        loaded.add(e.name.lower())
        if light_supported and e.is_light:
            e.load_index = f"FE:{light:03X}"
            light += 1
        else:
            e.load_index = f"{full:02X}"
            full += 1
