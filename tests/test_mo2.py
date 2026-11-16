from pathlib import Path

from modmanager.core import mo2
from modmanager.core.modlist import ModList


def make_mo2(tmp_path: Path, game: Path) -> Path:
    folder = tmp_path / "Modding" / "MO2"
    (folder / "mods" / "SkyUI").mkdir(parents=True)
    (folder / "mods" / "SkyUI" / "SkyUI_SE.esp").write_bytes(b"")
    (folder / "overwrite").mkdir()
    prof = folder / "profiles" / "Main"
    prof.mkdir(parents=True)
    (prof / "modlist.txt").write_text("# MO2\n+SkyUI\n-Tools_separator\n*Unmanaged: Dawnguard\n")
    (prof / "plugins.txt").write_text("# MO2\n*SkyUI_SE.esp\n")
    tools = tmp_path / "Modding" / "SSEEdit"
    tools.mkdir()
    (tools / "SSEEdit.exe").write_bytes(b"MZ")
    win_game = "Z:" + str(game).replace("/", "\\\\")
    (folder / "ModOrganizer.ini").write_text(
        "[General]\n"
        "gameName=Skyrim Special Edition\n"
        "selected_profile=@ByteArray(Main)\n"
        f"gamePath=@ByteArray({win_game})\n"
        "[customExecutables]\n"
        "size=3\n"
        "1\\binary=C:/Games/Skyrim Special Edition/SkyrimSE.exe\n"
        "1\\title=Skyrim\n"
        "2\\binary=C:/Old/Place/Modding/SSEEdit/SSEEdit.exe\n"
        "2\\title=SSEEdit\n"
        "2\\arguments=\"-quickautoclean\"\n"
        "3\\binary=C:/Gone/Tool.exe\n"
        "3\\title=Gone\n"
    )
    return folder


def test_import_mo2_in_place(env, tmp_path):
    game = env.game_dir
    folder = make_mo2(tmp_path, game)
    info = mo2.inspect(folder)
    assert info.game.id == "skyrimse" and info.game_dir == game and info.profile == "Main"
    assert info.unresolved == ["Gone (C:/Gone/Tool.exe)"]

    inst = mo2.import_instance(folder)
    assert inst.path == folder.resolve() and inst.active_profile.name == "Main"
    assert inst.profile_names() == ["Main"]
    exes = {e.name: e for e in inst.executables}
    assert Path(exes["SSEEdit"].path) == tmp_path / "Modding" / "SSEEdit" / "SSEEdit.exe"
    assert exes["SSEEdit"].args == "-quickautoclean"
    assert "Skyrim" not in exes  # duplicate of the game's default executable
    ml = ModList(inst)
    assert [(m.name, m.enabled) for m in ml.mods if not m.name.endswith("_separator")] == [("SkyUI", True)]
    # Importing again reuses the instance.
    assert mo2.import_instance(folder).path == inst.path
