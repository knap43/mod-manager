"""Remove the parts of PySide6 that Mod Manager never loads.

Keeps the Python modules below, the Qt plugins a desktop session needs, and the
shared libraries those depend on (followed through ELF DT_NEEDED entries).
Usage: python prune_qt.py <site-packages>
"""

from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

KEEP_MODULES = {"QtCore", "QtGui", "QtWidgets", "QtSvg", "QtSvgWidgets"}
KEEP_PLUGINS = {
    "platforms", "platformthemes", "platforminputcontexts", "imageformats", "iconengines",
    "xcbglintegrations", "egldeviceintegrations", "generic", "wayland-decoration-client",
    "wayland-graphics-integration-client", "wayland-shell-integration",
}
# The on-screen keyboard pulls in Qt Quick/QML (and its QML half is not in the Essentials wheel).
DROP_PLUGIN_FILES = {"libqtvirtualkeyboardplugin.so"}


def needed(path: Path) -> list[str]:
    """DT_NEEDED entries of a 64-bit little-endian ELF file."""
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return []
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    result = []
    for _name, sh_type, _f, _a, offset, size, link, _i, _al, entsize in sections:
        if sh_type != 6:  # SHT_DYNAMIC
            continue
        strtab_off = sections[link][4]
        for pos in range(offset, offset + size, entsize or 16):
            tag, val = struct.unpack_from("<qQ", data, pos)
            if tag == 0:
                break
            if tag == 1:  # DT_NEEDED
                end = data.index(b"\0", strtab_off + val)
                result.append(data[strtab_off + val : end].decode())
    return result


def main(site: Path) -> None:
    pyside = site / "PySide6"
    qt_lib = pyside / "Qt" / "lib"
    plugins = pyside / "Qt" / "plugins"

    for module in pyside.glob("Qt*.abi3.so"):
        if module.name.split(".")[0] not in KEEP_MODULES:
            module.unlink()
    for lib in pyside.glob("libpyside6qml*"):
        lib.unlink()
    for folder in plugins.iterdir():
        if folder.name not in KEEP_PLUGINS:
            shutil.rmtree(folder)
    for name in DROP_PLUGIN_FILES:
        for plugin in plugins.rglob(name):
            plugin.unlink()

    roots = [p for p in pyside.glob("*.so*") if p.is_file()]
    roots += [p for p in (site / "shiboken6").glob("*.so*") if p.is_file()]
    roots += [p for p in plugins.rglob("*.so") if p.is_file()]
    available = {p.name: p for p in qt_lib.iterdir()}
    keep: set[str] = set()
    queue = list(roots)
    while queue:
        for dep in needed(queue.pop()):
            if dep in available and dep not in keep:
                keep.add(dep)
                queue.append(available[dep])

    removed = 0
    for name, path in available.items():
        if name not in keep and (path.is_file() or path.is_symlink()):
            removed += path.stat().st_size if path.is_file() else 0
            path.unlink()
    print(f"kept {len(keep)} Qt libraries, removed {removed / 1048576:.0f} MB")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
