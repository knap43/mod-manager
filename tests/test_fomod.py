from pathlib import Path

import pytest

from modmanager.core import fomod
from modmanager.core.fomod import FomodContext, FomodSession

CONFIG = r"""<?xml version="1.0" encoding="UTF-16"?>
<config xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
        xsi:noNamespaceSchemaLocation="http://qconsulting.ca/fo3/ModConfig5.0.xsd">
  <moduleName>Test Mod</moduleName>
  <moduleImage path="fomod\images\main.png"/>
  <requiredInstallFiles>
    <folder source="Core" destination=""/>
  </requiredInstallFiles>
  <installSteps order="Explicit">
    <installStep name="Main">
      <optionalFileGroups order="Explicit">
        <group name="Resolution" type="SelectExactlyOne">
          <plugins order="Explicit">
            <plugin name="1K">
              <description>Small</description>
              <files><folder source="Textures 1K" destination="textures"/></files>
              <conditionFlags><flag name="res">1k</flag></conditionFlags>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
            <plugin name="2K">
              <description>Big</description>
              <files><folder source="Textures 2K" destination="Textures"/></files>
              <conditionFlags><flag name="res">2k</flag></conditionFlags>
              <typeDescriptor><type name="Recommended"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
        <group name="Extras" type="SelectAny">
          <plugins order="Ascending">
            <plugin name="Zeta patch">
              <description>Needs Zeta.esp</description>
              <files><file source="patches\ZetaPatch.esp" destination="ZetaPatch.esp"/></files>
              <typeDescriptor>
                <dependencyType>
                  <defaultType name="NotUsable"/>
                  <patterns>
                    <pattern>
                      <dependencies operator="And"><fileDependency file="Zeta.esp" state="Active"/></dependencies>
                      <type name="Recommended"/>
                    </pattern>
                  </patterns>
                </dependencyType>
              </typeDescriptor>
            </plugin>
            <plugin name="Alpha readme">
              <description>Always there</description>
              <files><file source="readme.txt" destination="docs\" alwaysInstall="true"/></files>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
    <installStep name="Only for 2K">
      <visible><flagDependency flag="res" value="2k"/></visible>
      <optionalFileGroups order="Explicit">
        <group name="Parallax" type="SelectAtMostOne">
          <plugins order="Explicit">
            <plugin name="Parallax">
              <description>p</description>
              <conditionFlags><flag name="parallax">On</flag></conditionFlags>
              <typeDescriptor><type name="Optional"/></typeDescriptor>
            </plugin>
          </plugins>
        </group>
      </optionalFileGroups>
    </installStep>
  </installSteps>
  <conditionalFileInstalls>
    <patterns>
      <pattern>
        <dependencies operator="And">
          <flagDependency flag="res" value="2k"/>
          <flagDependency flag="parallax" value="On"/>
        </dependencies>
        <files><file source="Parallax\p.esp" destination="Parallax.esp" priority="5"/></files>
      </pattern>
    </patterns>
  </conditionalFileInstalls>
</config>
"""


@pytest.fixture
def archive(tmp_path):
    root = tmp_path / "Wrapper"
    files = {
        "Core/meshes/core.nif": "core",
        "Core/Test.esp": "plugin-core",
        "Textures 1K/a.dds": "1k",
        "Textures 2K/a.dds": "2k",
        "Textures 2K/sub/b.dds": "2k-b",
        "patches/ZetaPatch.esp": "zeta",
        "readme.txt": "readme",
        "parallax/P.ESP": "parallax",
    }
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    (root / "fomod").mkdir()
    (root / "fomod" / "ModuleConfig.xml").write_bytes(CONFIG.encode("utf-16"))
    (root / "fomod" / "info.xml").write_text("<fomod><Name>Test</Name><Author>Me</Author><Version>1.2</Version></fomod>")
    return tmp_path


def installed(out: Path) -> dict[str, str]:
    return {p.relative_to(out).as_posix(): p.read_text() for p in out.rglob("*") if p.is_file()}


def test_find_and_parse(archive):
    root = fomod.find_fomod(archive)
    assert root == archive / "Wrapper"
    config, info = fomod.load(root)
    assert config.name == "Test Mod" and info.author == "Me" and info.version == "1.2"
    assert [s.name for s in config.steps] == ["Main", "Only for 2K"]
    extras = config.steps[0].groups[1]
    assert [o.name for o in extras.options] == ["Alpha readme", "Zeta patch"]  # order="Ascending"


def test_defaults_visibility_and_files(archive, tmp_path):
    root = fomod.find_fomod(archive)
    config, _ = fomod.load(root)
    s = FomodSession(config, root)
    sel = s.ensure_selection(0)
    assert sel[0] == [1]           # 2K is recommended
    assert sel[1] == []            # Zeta patch not usable without Zeta.esp
    assert s.option_type(0, 1, 1) == fomod.NOT_USABLE
    assert s.next_step(0) == 1     # 2K sets res=2k, which shows step 2
    s.ensure_selection(1)
    s.select(1, 0, 0, True)        # Parallax on
    assert s.validate(1) is None
    out = tmp_path / "out"
    assert s.build(out) == []
    assert installed(out) == {
        "meshes/core.nif": "core",
        "Test.esp": "plugin-core",
        "Textures/a.dds": "2k",
        "Textures/sub/b.dds": "2k-b",
        "docs/readme.txt": "readme",  # alwaysInstall, even though the option wasn't chosen
        "Parallax.esp": "parallax",   # conditional install; source path matched case-insensitively
    }
    assert s.choices() == {
        "Main": {"Resolution": ["2K"], "Extras": []},
        "Only for 2K": {"Parallax": ["Parallax"]},
    }


def test_changing_earlier_answer_hides_later_step(archive, tmp_path):
    root = fomod.find_fomod(archive)
    config, _ = fomod.load(root)
    s = FomodSession(config, root)
    s.ensure_selection(0)
    s.ensure_selection(1)
    s.select(1, 0, 0, True)
    s.select(0, 0, 0, True)        # back to step 1, pick 1K
    assert s.next_step(0) is None
    out = tmp_path / "out"
    s.build(out)
    files = {k.lower(): v for k, v in installed(out).items()}
    assert files["textures/a.dds"] == "1k"
    assert "parallax.esp" not in files   # step hidden, so its flag no longer applies
    assert "Only for 2K" not in s.choices()


def test_file_dependency_and_previous_choices(archive):
    root = fomod.find_fomod(archive)
    config, _ = fomod.load(root)
    ctx = FomodContext(file_state=lambda f: "Active" if f.lower() == "zeta.esp" else "Missing")
    previous = {"Main": {"Resolution": ["1K"], "Extras": []}}
    s = FomodSession(config, root, ctx, previous)
    sel = s.ensure_selection(0)
    assert sel[0] == [0]                   # previous choice wins over "Recommended"
    assert s.option_type(0, 1, 1) == fomod.RECOMMENDED
    assert sel[1] == []                    # previous choice said no extras


def test_group_rules(archive):
    root = fomod.find_fomod(archive)
    config, _ = fomod.load(root)
    s = FomodSession(config, root)
    s.ensure_selection(0)
    s.select(0, 0, 1, False)               # cannot deselect the only choice of SelectExactlyOne
    assert s.selections[0][0] == [1]
    s.ensure_selection(1)
    s.select(1, 0, 0, True)
    s.select(1, 0, 0, False)               # SelectAtMostOne allows none
    assert s.selections[1][0] == []


def test_case_insensitive_folder_merge(archive, tmp_path):
    root = fomod.find_fomod(archive)
    config, _ = fomod.load(root)
    s = FomodSession(config, root)
    s.ensure_selection(0)
    config.required_files.append(fomod.FileInstall("Textures 1K", "TEXTURES", folder=True, order=999))
    out = tmp_path / "out"
    s.build(out)
    folders = [p for p in out.iterdir() if p.name.lower() == "textures"]
    assert len(folders) == 1
    assert (folders[0] / "a.dds").read_text() == "1k"  # applied later, so it wins


def test_broken_encoding_declaration(tmp_path):
    path = tmp_path / "ModuleConfig.xml"
    path.write_bytes(CONFIG.encode("utf-8"))  # Declares UTF-16 but is UTF-8, as some installers do.
    assert fomod.parse_module_config(path).name == "Test Mod"


def test_pe_version(tmp_path):
    import struct

    blob = b"MZ" + b"\0" * 100 + "VS_VERSION_INFO".encode("utf-16-le") + b"\0" * 6
    blob += b"\xbd\x04\xef\xfe" + struct.pack("<III", 0x10000, (1 << 16) | 6, (1170 << 16) | 0)
    exe = tmp_path / "SkyrimSE.exe"
    exe.write_bytes(blob)
    assert fomod.pe_file_version(exe) == (1, 6, 1170, 0)


def test_manager_context(env):
    from conftest import add_mod, make_plugin
    from modmanager.core.manager import Manager

    root = add_mod(env, "Zeta", {"SKSE/Plugins/zeta.dll": "x"})
    make_plugin(root / "Zeta.esp")
    make_plugin(root / "Off.esp")
    mgr = Manager(env)
    mgr.modlist.set_enabled(["Zeta"], True)
    entries = mgr.plugins()
    mgr.modlist.save_plugin_order([
        type(e)(e.name, enabled=e.name != "Off.esp") for e in entries
    ])
    ctx = mgr.fomod_context()
    assert ctx.file_state("Zeta.esp") == "Active"
    assert ctx.file_state("off.esp") == "Inactive"
    assert ctx.file_state("skse\\plugins\\ZETA.dll") == "Active"
    assert ctx.file_state("Skyrim.esm") == "Active"
    assert ctx.file_state("Nope.esp") == "Missing"
