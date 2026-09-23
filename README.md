# Mod Manager

A native Linux mod manager modelled on Mod Organizer 2. It works with any game
whose mods are files placed in a folder, and has first-class support for
Skyrim Special Edition (plus Skyrim LE and Fallout 4). Windows programs run
through Proton with [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher).

## Features

- **Instances and profiles** in MO2's layout: each instance has its own `mods/`,
  `overwrite/`, `downloads/` and `profiles/`. Every profile has its own mod order,
  enabled mods and plugin load order (`modlist.txt` uses MO2's format).
- **Mod list** ordered by priority (drag and drop, `Ctrl+Up/Down`, `Space` toggles),
  separators, file conflict indicators (`+` wins, `−` loses, `±` both, `✕` fully
  overwritten) and conflict highlighting for the selected mod. Double-click a mod
  to list its conflicting files.
- **Plugin load order** with drag and drop, read from each plugin's header:
  masters are kept above regular plugins, light plugins get `FE:xxx` indices,
  and missing or late masters are flagged. Base-game masters and Creation Club
  content (`Skyrim.ccc`) are loaded implicitly, as the engine does.
- **Deployment into the Data folder** by hard link (default), symbolic link or copy.
  Deployment is incremental and recorded in a manifest, so **Purge** restores the
  game folder exactly:
  - game files that a mod replaces are backed up and restored;
  - folders are merged case-insensitively (`Textures/` and `textures/` become one);
  - edits a tool (such as xEdit) makes to a deployed file are written back into
    its mod rather than lost;
  - files the game or tools create while running are moved into **Overwrite**.
- **Proton via umu-launcher**: pick the Wine prefix (a dedicated one per instance,
  or Steam's own), the Proton build (UMU-Proton, latest GE-Proton, or any detected
  build), `GAMEID`/`STORE` and extra environment variables. `winecfg` and
  Winetricks run in the same prefix. **Run** deploys, writes `plugins.txt` into the
  prefix, starts the program and streams its output to the log.
- **FOMOD installers** (`fomod/ModuleConfig.xml`): a step-by-step wizard with option
  images and descriptions, all group types, condition flags, steps that appear or
  disappear depending on earlier answers, options whose type depends on your installed
  plugins or game version, and conditional file installs. Your choices are remembered
  and preselected when you reinstall or update the mod. **Manual…** falls back to
  picking the files yourself.
- **Installing** from archives (zip natively; 7z/rar through 7-Zip, `bsdtar` or `unrar`)
  or folders, with automatic detection of the folder that maps onto `Data`.
  Nexus-style file names (`Name-1234-1-0-1700000000.7z`) give the mod's name,
  version and Nexus ID. Archives can be dropped onto the window.

## Requirements

- Python 3.10+ and PySide6 (Qt 6); the AppImage bundles both
- [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) (`umu-run`)
  to run Windows programs
- `bsdtar` (libarchive, always present on Arch) or 7-Zip to install `.7z`/`.rar` archives
- For the Steam version of Skyrim, Steam must be running (the game uses Steam DRM)

## Building and installing

Everything goes through `./build.sh` (`./build.sh help` lists the commands).
Results are written to `dist/`.

### Arch Linux package

```sh
./build.sh deps          # pacman: pyside6, build tools, base-devel; then umu-launcher, 7zip, fuse2
./build.sh arch -i       # build with makepkg and install the package
```

`./build.sh arch` without `-i` only builds `dist/modmanager-<version>-1-any.pkg.tar.zst`.
Any other option goes to makepkg (for example `--nocheck`). The package is built from
the working tree, uncommitted changes included, using `packaging/arch/PKGBUILD`.
If `umu-launcher` is not in your enabled repositories, `deps` tells you to get it from the AUR.

### AppImage

```sh
./build.sh appimage      # dist/ModManager-<version>-x86_64.AppImage
```

This bundles a relocatable Python ([python-build-standalone](https://github.com/astral-sh/python-build-standalone),
checksum-verified) and the PySide6 Essentials wheel. It strips the Qt parts the app
never loads, checks that the result still starts, and packs everything with
[appimagetool](https://github.com/AppImage/appimagetool). It needs `curl`, network
access and nothing else: no FUSE, no root. Downloads are cached in `~/.cache/modmanager-build`.

When the AppImage starts `umu-run` or a game, it removes its own variables
(`APPDIR`, paths into the mount) from the environment first, so they see your
system as usual. Running an AppImage requires FUSE 2 (`fuse2` on Arch); on the X11
platform, Qt also needs `xcb-util-cursor`.

### Running from the source tree

```sh
./build.sh run           # needs python and pyside6 installed
./build.sh test
```

`data/modmanager.desktop` and `data/modmanager.svg` are the desktop entry and
icon; the Arch package installs both.

## Getting started with Skyrim SE

1. Start the program and create an instance. The Steam install is detected
   automatically. **Put the instance folder on the same drive as the game**, so that
   mods can be hard-linked; otherwise symbolic links are used.
2. Open **Game › Settings › Proton**. By default the instance gets its own prefix,
   and umu creates it on the first run; **Use Steam's prefix** reuses the one Steam made.
   Leave Proton on UMU-Proton or choose GE-Proton.
3. Install mods with **Install from archive** (or put archives in the Downloads tab),
   tick them, and order them. Lower in the list means higher priority.
4. Adjust the plugin order in the **Plugins** tab.
5. Choose an executable and click **Run**. To use SKSE, add `skse64_loader.exe`
   under **Edit** (executables).

Mods stay deployed after the game exits, so starting the game from Steam also
uses them. **Purge** returns the game folder to vanilla.

## Where things are stored

| What | Where |
| --- | --- |
| Instance list | `~/.config/modmanager/instances.json` |
| Instances (default) | `~/.local/share/modmanager/instances/<name>/` |
| Deployment manifest and backups | `<instance>/deploy/` |
| Program output from the last run | `<instance>/logs/` |
| Application log | `~/.local/share/modmanager/logs/modmanager.log` |
| Game `plugins.txt` | `<prefix>/drive_c/users/steamuser/AppData/Local/Skyrim Special Edition/` (`… GOG/` or `… EPIC/` for those editions, detected from the game folder) |

## Development

```sh
pip install -e '.[dev]'
pytest
```

The code is split into `modmanager/core` (no Qt: instances, mod list, plugins,
deployment, Proton, Steam detection) and `modmanager/ui` (PySide6).

## Roadmap

- Nexus Mods integration (`nxm://` links, downloads, update checks)
- SKSE detection
- C# script installers (`script.cs`), which only a few very old mods use
- Automatic load order sorting (LOOT) and dependency detection
