"""Which records each plugin contains, for LOOT's overlap rules."""

from __future__ import annotations

import mmap
import os
import struct
from dataclasses import dataclass
from pathlib import Path

from modmanager.core.plugins import read_header

# Record keys are (file id << 24) | object id, with file ids interned per session
# so keys from different plugins can be compared.
_file_ids: dict[str, int] = {}
_cache: dict[str, tuple[int, int, "PluginRecords"]] = {}


def file_id(name: str) -> int:
    return _file_ids.setdefault(name.lower(), len(_file_ids) + 1)


@dataclass
class PluginRecords:
    keys: frozenset[int]        # Every record the plugin contains (new and overridden)
    overrides: frozenset[int]   # Records it overrides from its masters
    total: int

    @property
    def override_count(self) -> int:
        return len(self.overrides)


def scan(path: Path) -> PluginRecords:
    """Read record headers (24 bytes each, Skyrim/Fallout 4 format), skipping record data."""
    st = path.stat()
    cached = _cache.get(str(path))
    if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
        return cached[2]
    header = read_header(path)
    masters = header.masters if header else []
    master_ids = [file_id(m) for m in masters]
    own = file_id(path.name)
    n_masters = len(masters)
    keys: set[int] = set()
    overrides: set[int] = set()
    unpack = struct.Struct("<4sII").unpack_from
    fid_at = struct.Struct("<I").unpack_from
    total = 0
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        if size < 24:
            return PluginRecords(frozenset(), frozenset(), 0)
        with mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ) as mm:
            # Skip the TES4 header record.
            pos = 24 + struct.unpack_from("<I", mm, 4)[0]
            while pos + 24 <= size:
                sig, length, _flags = unpack(mm, pos)
                if sig == b"GRUP":
                    pos += 24  # Groups nest; their contents follow the header directly.
                    continue
                fid = fid_at(mm, pos + 12)[0]
                index, obj = fid >> 24, fid & 0xFFFFFF
                if index < n_masters:
                    key = (master_ids[index] << 24) | obj
                    overrides.add(key)
                else:
                    key = (own << 24) | obj
                keys.add(key)
                total += 1
                pos += 24 + length
    result = PluginRecords(frozenset(keys), frozenset(overrides), total)
    _cache[str(path)] = (st.st_size, st.st_mtime_ns, result)
    return result
