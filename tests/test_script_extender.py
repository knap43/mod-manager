import struct

from conftest import add_mod

from modmanager.core import script_extender
from modmanager.core.games import get_game
from modmanager.core.manager import Manager


def fake_exe(path, version):
    ms = (version[0] << 16) | version[1]
    ls = (version[2] << 16) | version[3]
    blob = b"MZ" + b"\0" * 64 + "VS_VERSION_INFO".encode("utf-16-le") + b"\0" * 6
    blob += b"\xbd\x04\xef\xfe" + struct.pack("<III", 0x10000, ms, ls)
    path.write_bytes(blob)


def test_not_installed(tmp_path):
    fake_exe(tmp_path / "SkyrimSE.exe", (1, 6, 1170, 0))
    status = script_extender.check(get_game("skyrimse"), tmp_path)
    assert not status.installed and status.summary() == "SKSE64 not installed"
    assert status.game_version == (1, 6, 1170, 0)
    assert script_extender.check(get_game("generic"), tmp_path) is None


def test_matching_and_mismatched_runtime(tmp_path):
    fake_exe(tmp_path / "SkyrimSE.exe", (1, 6, 1170, 0))
    fake_exe(tmp_path / "skse64_loader.exe", (0, 2, 2, 6))
    (tmp_path / "skse64_1_6_1170.dll").write_bytes(b"")
    (tmp_path / "skse64_steam_loader.dll").write_bytes(b"")
    status = script_extender.check(get_game("skyrimse"), tmp_path)
    assert status.installed and status.compatible
    assert status.runtimes == [(1, 6, 1170)] and status.summary() == "SKSE64 2.2.6"

    (tmp_path / "skse64_1_6_1170.dll").unlink()
    (tmp_path / "skse64_1_6_640.dll").write_bytes(b"")
    status = script_extender.check(get_game("skyrimse"), tmp_path)
    assert status.compatible is False and "wrong game version" in status.summary()


def test_address_library_names():
    assert script_extender.address_library_file((1, 6, 1170, 0)) == "versionlib-1-6-1170-0.bin"
    assert script_extender.address_library_file((1, 5, 97, 0)) == "version-1-5-97-0.bin"


def test_executable_added_once_and_plugins_found(env):
    fake_exe(env.game_dir / "skse64_loader.exe", (0, 2, 2, 6))
    add_mod(env, "Engine Fixes", {"SKSE/Plugins/EngineFixes.dll": "x", "SKSE/Plugins/EngineFixes.toml": "y"})
    mgr = Manager(env)
    assert mgr.ensure_script_extender_executable()
    assert env.executables[0].name == "SKSE64" and env.config["selected_executable"] == 0
    assert not mgr.ensure_script_extender_executable()
    env.executables = env.executables[1:]          # user removes it
    assert not mgr.ensure_script_extender_executable()
    assert mgr.enabled_script_extender_plugins() == []
    mgr.modlist.set_enabled(["Engine Fixes"], True)
    assert mgr.enabled_script_extender_plugins() == ["skse/plugins/enginefixes.dll"]


def test_game_version_prefers_product_version_string(tmp_path):
    # Skyrim SE 1.5.97 says 1.0.0.0 in VS_FIXEDFILEINFO; SKSE goes by the ProductVersion string.
    game = tmp_path
    fake_exe(game / "SkyrimSE.exe", (1, 0, 0, 0))
    with open(game / "SkyrimSE.exe", "ab") as fh:
        fh.write("ProductVersion\0".encode("utf-16-le") + b"\0\0" + "1.5.97.0\0".encode("utf-16-le"))
    fake_exe(game / "skse64_loader.exe", (0, 2, 0, 20))
    (game / "skse64_1_5_97.dll").write_bytes(b"MZ")
    st = script_extender.check(get_game("skyrimse"), game)
    assert st.game_version == (1, 5, 97, 0) and st.compatible
