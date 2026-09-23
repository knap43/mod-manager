#!/usr/bin/env bash
# Build helper for Mod Manager. Run ./build.sh help for the list of commands.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD="$ROOT/build"
DIST="$ROOT/dist"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/modmanager-build"
NAME=modmanager
VERSION="$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/pyproject.toml" | head -n1)"
ARCH="$(uname -m)"
PYTHON="${PYTHON:-python3}"
# Python bundled into the AppImage (python-build-standalone release series).
APPIMAGE_PYTHON="${APPIMAGE_PYTHON:-3.13}"

msg() { printf '\033[1;34m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "'$1' is required${2:+ — $2}"; }

usage() {
  cat <<EOF
Usage: ./build.sh <command> [options]

  deps            Install build and runtime dependencies with pacman (Arch)
  run [args]      Run from the source tree without installing
  test            Run the test suite
  wheel           Build a Python wheel into dist/
  arch [-i]       Build an Arch package into dist/ with makepkg (-i also installs it);
                  other options are passed to makepkg, e.g. --nocheck
  appimage        Build a self-contained AppImage into dist/
  clean           Remove build/ and dist/

Version: $VERSION
EOF
}

# --------------------------------------------------------------------------- deps

cmd_deps() {
  need pacman "this command is for Arch Linux and derivatives"
  msg "Installing required packages"
  sudo pacman -S --needed python pyside6 python-yaml python-build python-installer python-setuptools \
    python-wheel python-pytest base-devel
  # Optional: umu-launcher runs games through Proton, 7zip extracts archives faster
  # than bsdtar, fuse2 lets AppImages mount.
  local pkg
  for pkg in umu-launcher 7zip fuse2; do
    pacman -Qq "$pkg" >/dev/null 2>&1 && continue
    msg "Installing $pkg"
    sudo pacman -S --needed --noconfirm "$pkg" \
      || warn "$pkg is not in your enabled repositories; install it from the AUR (e.g. 'paru -S $pkg')."
  done
}

# ---------------------------------------------------------------------- run/test

cmd_run() {
  cd "$ROOT"
  PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}" exec "$PYTHON" -m modmanager "$@"
}

cmd_test() {
  cd "$ROOT"
  "$PYTHON" -m pytest -q "$@"
}

# ------------------------------------------------------------------------- wheel

cmd_wheel() {
  cd "$ROOT"
  mkdir -p "$DIST"
  rm -f "$DIST/$NAME-$VERSION"-*.whl
  msg "Building wheel"
  # Probe build.__main__: the project's build/ folder would satisfy a plain 'import build'.
  if "$PYTHON" -c 'import build.__main__' 2>/dev/null; then
    "$PYTHON" -m build --wheel --no-isolation --outdir "$DIST" >/dev/null
  else
    "$PYTHON" -m pip wheel --quiet --no-deps -w "$DIST" .
  fi
  rm -rf "$ROOT/build/lib" "$ROOT/build/bdist."* "$ROOT/src/$NAME.egg-info"
  WHEEL="$(ls "$DIST/$NAME-$VERSION"-*.whl)"
  msg "Wheel: ${WHEEL#"$ROOT/"}"
}

# -------------------------------------------------------------------------- arch

source_tarball() {
  # Current working tree (including uncommitted changes) as <name>-<version>/.
  local out="$1"
  tar -C "$ROOT" -czf "$out" \
    --exclude-vcs --exclude='./build' --exclude='./dist' --exclude='./.venv' \
    --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.egg-info' \
    --transform "s,^\.,$NAME-$VERSION," .
}

cmd_arch() {
  need makepkg "install base-devel"
  local install=() passthrough=()
  for arg in "$@"; do
    case "$arg" in
      -i|--install) install=(--syncdeps --install) ;;
      *) passthrough+=("$arg") ;;
    esac
  done
  local dir="$BUILD/arch"
  rm -rf "$dir"
  mkdir -p "$dir" "$DIST"
  sed "s/^pkgver=.*/pkgver=$VERSION/" "$ROOT/packaging/arch/PKGBUILD" > "$dir/PKGBUILD"
  source_tarball "$dir/$NAME-$VERSION.tar.gz"
  msg "Running makepkg"
  (cd "$dir" && PKGDEST="$DIST" makepkg --force --cleanbuild "${install[@]}" "${passthrough[@]}")
  msg "Package: $(ls -t "$DIST"/$NAME-"$VERSION"-*.pkg.tar.* | head -n1)"
}

# ---------------------------------------------------------------------- appimage

fetch() {
  # fetch <url> <file>: download once into the cache.
  [[ -s "$2" ]] && return 0
  msg "Downloading ${1##*/}"
  curl -fL --retry 3 -o "$2.part" "$1" && mv "$2.part" "$2"
}

python_standalone() {
  # Prints the path of a cached, checksum-verified python-build-standalone tarball.
  local pointer prefix sums asset sha
  pointer="$(curl -fsSL https://raw.githubusercontent.com/astral-sh/python-build-standalone/latest-release/latest-release.json)"
  prefix="$("$PYTHON" -c 'import json,sys; print(json.load(sys.stdin)["asset_url_prefix"])' <<<"$pointer")"
  sums="$(curl -fsSL "$prefix/SHA256SUMS")"
  local pattern="cpython-${APPIMAGE_PYTHON//./\\.}\.[0-9]+\+[0-9]+-${ARCH}-unknown-linux-gnu-install_only_stripped\.tar\.gz$"
  read -r sha asset < <(grep -E "  $pattern" <<<"$sums" | head -n1) \
    || die "No Python $APPIMAGE_PYTHON build for $ARCH in ${prefix##*/}"
  fetch "$prefix/$asset" "$CACHE/$asset" >&2
  echo "$sha  $CACHE/$asset" | sha256sum --check --status || { rm -f "$CACHE/$asset"; die "Checksum mismatch for $asset"; }
  echo "$CACHE/$asset"
}

cmd_appimage() {
  need curl
  need sha256sum
  mkdir -p "$CACHE" "$DIST"
  cmd_wheel

  local appdir="$BUILD/AppDir"
  rm -rf "$appdir"
  mkdir -p "$appdir/usr"

  local tarball
  tarball="$(python_standalone)"
  msg "Unpacking ${tarball##*/}"
  tar -xzf "$tarball" -C "$appdir/usr" --strip-components=1

  local py="$appdir/usr/bin/python3"
  msg "Installing $NAME and PySide6 into the AppDir"
  "$py" -m pip install --quiet --no-cache-dir --no-warn-script-location --disable-pip-version-check "$WHEEL"

  msg "Trimming"
  local site
  site="$("$py" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  # Qt tooling, QML and headers are not needed at runtime.
  (cd "$site/PySide6" && rm -rf assistant designer linguist lrelease lupdate uic rcc qmlformat qmllint \
    qmlls qmlcachegen qsb balsam balsamui svgtoqml Qt/qml Qt/translations Qt/libexec Qt/metatypes \
    include typesystems \
    glue scripts doc examples ./*.pyi)
  rm -rf "$site"/pip "$site"/pip-* "$appdir/usr/bin/pip"* "$appdir/usr/include" "$appdir/usr/share"
  local stdlib
  stdlib="$("$py" -c 'import sysconfig; print(sysconfig.get_paths()["stdlib"])')"
  rm -rf "$stdlib"/{idlelib,tkinter,turtledemo,ensurepip,lib2to3,test} "$stdlib"/config-*
  rm -rf "$appdir/usr/lib"/{libtcl,libtk,tcl,tk,itcl,thread}* 2>/dev/null || true
  "$py" "$ROOT/packaging/appimage/prune_qt.py" "$site"

  msg "Checking that the bundled Qt still loads"
  QT_QPA_PLATFORM=offscreen "$py" -I -c '
from PySide6.QtWidgets import QApplication
app = QApplication(["check"])
import modmanager.ui.main_window
from modmanager.ui import theme
theme.apply(app)
' || die "The trimmed AppDir no longer imports; see the error above."

  msg "Writing AppRun and metadata"
  cat >"$appdir/AppRun" <<'EOF'
#!/bin/sh
# -I: ignore the user's PYTHONPATH/PYTHONHOME and site-packages; use only the bundled Python.
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/python3" -I -m modmanager "$@"
EOF
  chmod +x "$appdir/AppRun"
  cp "$ROOT/data/modmanager.desktop" "$appdir/$NAME.desktop"
  cp "$ROOT/data/modmanager.svg" "$appdir/$NAME.svg"
  ln -sf "$NAME.svg" "$appdir/.DirIcon"
  install -Dm644 "$ROOT/data/modmanager.desktop" "$appdir/usr/share/applications/$NAME.desktop"
  install -Dm644 "$ROOT/data/modmanager.svg" "$appdir/usr/share/icons/hicolor/scalable/apps/$NAME.svg"

  local tool="$CACHE/appimagetool-$ARCH.AppImage"
  fetch "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage" "$tool"
  chmod +x "$tool"
  local out="$DIST/ModManager-$VERSION-$ARCH.AppImage"
  msg "Packing $(du -sh "$appdir" | cut -f1) into ${out#"$ROOT/"}"
  # Extract-and-run works without FUSE (containers, CI).
  APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$tool" --no-appstream "$appdir" "$out" \
    >"$BUILD/appimagetool.log" 2>&1 || { cat "$BUILD/appimagetool.log"; die "appimagetool failed"; }
  msg "AppImage: ${out#"$ROOT/"} ($(du -h "$out" | cut -f1))"
}

# ------------------------------------------------------------------------- clean

cmd_clean() {
  rm -rf "$BUILD" "$DIST" "$ROOT/src/$NAME.egg-info"
  msg "Removed build/ and dist/"
}

# -------------------------------------------------------------------------- main

command="${1:-help}"
shift || true
case "$command" in
  deps|run|test|wheel|arch|appimage|clean) "cmd_$command" "$@" ;;
  help|-h|--help) usage ;;
  *) usage; die "unknown command: $command" ;;
esac
