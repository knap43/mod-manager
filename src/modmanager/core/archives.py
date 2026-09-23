"""Archive extraction. Zip is handled natively; 7z and rar need 7-Zip, bsdtar or unrar."""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2")


class ArchiveError(Exception):
    pass


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_EXTS)


def archive_stem(path: Path) -> str:
    name = path.name
    for ext in sorted(ARCHIVE_EXTS, key=len, reverse=True):
        if name.lower().endswith(ext):
            return name[: -len(ext)]
    return path.stem


def available_tools() -> dict[str, str]:
    tools = {}
    for tool in ("7zz", "7z", "7za", "bsdtar", "unrar"):
        found = shutil.which(tool)
        if found:
            tools[tool] = found
    return tools


def extract(archive: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if archive.suffix.lower() == ".zip":
        try:
            _extract_zip(archive, dest)
            return
        except (zipfile.BadZipFile, NotImplementedError):
            pass  # Unsupported compression (e.g. Deflate64); try external tools.

    tools = available_tools()
    commands: list[list[str]] = []
    for sevenzip in ("7zz", "7z", "7za"):
        if sevenzip in tools:
            commands.append([tools[sevenzip], "x", "-y", "-bd", f"-o{dest}", str(archive)])
            break
    if "bsdtar" in tools:
        commands.append([tools["bsdtar"], "-xf", str(archive), "-C", str(dest)])
    if "unrar" in tools and archive.suffix.lower() == ".rar":
        commands.append([tools["unrar"], "x", "-o+", "-idq", str(archive), str(dest) + "/"])
    if not commands:
        raise ArchiveError(
            f"Cannot extract {archive.name}: install 7-Zip (7zz/7z), libarchive (bsdtar) or unrar."
        )
    errors = []
    for cmd in commands:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            _check_escapes(dest)
            return
        errors.append(f"{Path(cmd[0]).name}: {(proc.stderr or proc.stdout).strip()[-400:]}")
    raise ArchiveError(f"Failed to extract {archive.name}:\n" + "\n".join(errors))


def _extract_zip(archive: Path, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            target = (root / name).resolve()
            if root not in target.parents and target != root:
                raise ArchiveError(f"Refusing to extract unsafe path: {info.filename}")
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)


def _check_escapes(dest: Path) -> None:
    """Reject symlinks inside an extracted archive that point outside of it."""
    root = dest.resolve()
    for link in [p for p in dest.rglob("*") if p.is_symlink()]:
        target = link.resolve()
        if root not in target.parents:
            link.unlink()
