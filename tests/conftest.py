import struct
from pathlib import Path

import pytest

from modmanager.core.instance import Instance


def make_plugin(path: Path, masters=(), master=False, light=False, records=(), description="") -> Path:
    """Write a TES4 plugin: header, then one GRUP holding empty records with the given FormIDs."""
    sub = b""
    for m in masters:
        data = m.encode("cp1252") + b"\0"
        sub += b"MAST" + struct.pack("<H", len(data)) + data
        sub += b"DATA" + struct.pack("<H", 8) + b"\0" * 8
    if description:
        data = description.encode("cp1252") + b"\0"
        sub += b"SNAM" + struct.pack("<H", len(data)) + data
    flags = (0x1 if master else 0) | (0x200 if light else 0)
    header = b"TES4" + struct.pack("<IIIIHH", len(sub), flags, 0, 0, 44, 0)
    body = b"".join(b"WEAP" + struct.pack("<IIIIHH", 0, 0, fid, 0, 44, 0) for fid in records)
    group = b"GRUP" + struct.pack("<I", 24 + len(body)) + b"WEAP" + struct.pack("<iHHI", 0, 0, 0, 0) + body if records else b""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + sub + group)
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    game = tmp_path / "game"
    data = game / "Data"
    data.mkdir(parents=True)
    (game / "SkyrimSE.exe").write_bytes(b"MZ")
    for name in ("Skyrim.esm", "Update.esm", "Dawnguard.esm"):
        make_plugin(data / name, master=True)
    (data / "Textures").mkdir()
    (data / "Textures" / "vanilla.dds").write_text("vanilla")
    inst = Instance.create(tmp_path / "instance", "Test", "skyrimse", game)
    return inst


def add_mod(inst: Instance, name: str, files: dict[str, str]) -> Path:
    root = inst.mods_dir / name
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return root
