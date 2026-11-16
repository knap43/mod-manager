import os
from pathlib import Path

from modmanager.core import proton
from modmanager.core.instance import Executable
from modmanager.core.manager import Manager


def fake_build(base: Path, name: str, ffmpeg: tuple[str, ...] = (), plugin: bool = True) -> Path:
    build = base / name
    lib = build / "files" / "lib" / "x86_64-linux-gnu"
    (lib / "gstreamer-1.0").mkdir(parents=True)
    (build / "proton").write_text("#!/usr/bin/env python3\n")
    if plugin:
        (lib / "gstreamer-1.0" / "libgstlibav.so").write_bytes(
            b"\x7fELF\0libavfilter.so.7\0libavcodec.so.58\0libgstreamer-1.0.so.0\0"
        )
    for name in ffmpeg:
        (lib / name).write_bytes(b"")
    return build


def test_missing_wma_libraries(tmp_path):
    bare = fake_build(tmp_path, "UMU-Proton-10.0-4")
    assert proton.missing_wma_libraries(bare) == ["libavcodec.so.58", "libavfilter.so.7"]
    full = fake_build(tmp_path, "GE-Proton10-34", ffmpeg=("libavfilter.so.7", "libavcodec.so.58"))
    assert proton.missing_wma_libraries(full) == []
    assert proton.missing_wma_libraries(fake_build(tmp_path, "Other", plugin=False)) == []


def test_resolve_build_picks_configured_or_newest(tmp_path):
    tools = tmp_path / "compatibilitytools.d"
    for name in ("UMU-Proton-9.0-3", "UMU-Proton-10.0-4", "GE-Proton9-27", "GE-Proton10-34"):
        fake_build(tools, name)
    assert proton.resolve_build({"proton_path": ""}, [tools]).name == "UMU-Proton-10.0-4"
    assert proton.resolve_build({"proton_path": "GE-Proton"}, [tools]).name == "GE-Proton10-34"
    chosen = tools / "GE-Proton9-27"
    assert proton.resolve_build({"proton_path": str(chosen)}, [tools]) == chosen
    assert proton.resolve_build({"proton_path": str(tmp_path / "gone")}, [tools]) is None


def test_wma_problem_reported_for_build_without_ffmpeg(env, tmp_path):
    env.proton["proton_path"] = str(fake_build(tmp_path, "UMU-Proton-10.0-4"))
    keys = [p for p in Manager(env).problems() if p.key.startswith("proton:wma:")]
    assert len(keys) == 1 and "music and voices" in keys[0].title
    env.proton["proton_path"] = str(fake_build(
        tmp_path, "GE-Proton10-34", ffmpeg=("libavfilter.so.7", "libavcodec.so.58")))
    assert not [p for p in Manager(env).problems() if p.key.startswith("proton:wma:")]


def test_steam_client_files_copied_once(tmp_path):
    steam_root = tmp_path / "Steam"
    (steam_root / "legacycompat").mkdir(parents=True)
    for name in ("steamclient.dll", "steamclient64.dll", "SteamService.exe", "Steam.dll"):
        (steam_root / "legacycompat" / name).write_bytes(name.encode())
    prefix = tmp_path / "prefix"
    (prefix / "drive_c").mkdir(parents=True)
    copied = proton.install_steam_client_files(prefix, steam_root)
    assert sorted(copied) == ["Steam.dll", "steam.exe", "steamclient.dll", "steamclient64.dll"]
    target = proton.steam_client_dir(prefix)
    assert (target / "steam.exe").read_bytes() == b"SteamService.exe"
    assert proton.install_steam_client_files(prefix, steam_root) == []  # already current


def test_needs_steam_client_only_for_steam_copies_under_proton(env):
    mgr = Manager(env)
    exe = Executable("Game", str(env.game_dir / "SkyrimSE.exe"))
    assert not mgr.needs_steam_client(exe)  # no steam_api64.dll: not a Steam copy
    (env.game_dir / "steam_api64.dll").write_bytes(b"MZ")
    assert mgr.needs_steam_client(exe)
    assert not mgr.needs_steam_client(Executable("Native", "/bin/true", use_proton=False))
