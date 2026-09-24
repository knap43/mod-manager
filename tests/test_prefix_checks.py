from pathlib import Path

from conftest import add_mod

from modmanager.core import proton
from modmanager.core.manager import Manager

WINE_DLL = b"MZ" + b"\0" * 62 + b"Wine builtin DLL" + b"\0" * 64
NATIVE_DLL = b"MZ\x90\0" + b"\0" * 60 + b"This program cannot be run in DOS mode." + b"\0" * 64
USER_REG = """WINE REGISTRY Version 2
;; All keys relative to \\\\User\\\\S-1-5-21-0-0-0-1000

[Software\\\\Wine\\\\DllOverrides] 1700000000
#time=1d9aaaaaaaaaaaa
"*d3dcompiler_47"="native"
"atl100"="native,builtin"
"""


def prefix_with(env, dll: bytes | None, reg: str | None = None) -> Path:
    prefix = Path(env.proton["prefix"])
    sys32 = prefix / "drive_c" / "windows" / "system32"
    sys32.mkdir(parents=True, exist_ok=True)
    (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")
    if dll is not None:
        (sys32 / "d3dcompiler_47.dll").write_bytes(dll)
    if reg is not None:
        (prefix / "user.reg").write_text(reg)
    return prefix


def problem(mgr):
    return next((p for p in mgr.problems() if p.key == "proton:d3dcompiler_47"), None)


def setup(env):
    add_mod(env, "Community Shaders", {"SKSE/Plugins/CommunityShaders.dll": "x"})
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Community Shaders"], True)
    return mgr


def test_no_problem_without_community_shaders(env):
    prefix_with(env, WINE_DLL)
    assert problem(Manager(env)) is None


def test_wine_builtin_compiler_is_reported_with_winetricks_fix(env):
    prefix_with(env, WINE_DLL)
    p = problem(setup(env))
    assert p is not None and p.tool == ["winetricks", "d3dcompiler_47"]
    assert "only has Wine's own" in p.detail and p.fix_label == "Install with Winetricks"


def test_fresh_prefix_is_reported(env):
    assert "no d3dcompiler_47.dll yet" in problem(setup(env)).detail


def test_native_dll_needs_override(env):
    prefix_with(env, NATIVE_DLL, reg="WINE REGISTRY Version 2\n")
    assert "override" in problem(setup(env)).detail


def test_native_dll_with_registry_override_is_fine(env):
    prefix = prefix_with(env, NATIVE_DLL, reg=USER_REG)
    assert proton.dll_override(prefix, "d3dcompiler_47.dll") == "native"
    assert problem(setup(env)) is None


def test_native_dll_with_environment_override_is_fine(env):
    prefix_with(env, NATIVE_DLL, reg="WINE REGISTRY Version 2\n")
    env.proton["env"] = {"WINEDLLOVERRIDES": "dxgi=n,b;d3dcompiler_47,d3dcompiler_43=n"}
    assert problem(setup(env)) is None


def test_manual_install_in_data_is_detected(env):
    prefix_with(env, WINE_DLL)
    target = env.data_dir / "SKSE" / "Plugins"
    target.mkdir(parents=True)
    (target / "CommunityShaders.dll").write_text("x")
    assert problem(Manager(env)) is not None
