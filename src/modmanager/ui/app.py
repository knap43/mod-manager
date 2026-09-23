"""Application entry point."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from modmanager import APP_NAME, __version__
from modmanager.core import ipc, paths
from modmanager.core.instance import Instance, InstanceRegistry
from modmanager.ui import main_window, theme
from modmanager.ui.dialogs import InstancePicker
from modmanager.ui.main_window import MainWindow, QtLogHandler


def setup_logging(verbose: bool) -> QtLogHandler:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    log_dir = paths.data_home() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "modmanager.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    root.addHandler(console)
    qt_handler = QtLogHandler()
    qt_handler.setLevel(logging.INFO)
    root.addHandler(qt_handler)
    return qt_handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="modmanager", description=f"{APP_NAME} {__version__}")
    parser.add_argument("--instance", type=Path, help="open the instance in this folder")
    parser.add_argument("--pick", action="store_true", help="show the instance picker")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log debug output to the terminal")
    parser.add_argument("--nxm", metavar="URL", help="download an nxm:// link from Nexus Mods")
    args, qt_args = parser.parse_known_args(argv if argv is not None else sys.argv[1:])
    # Some browsers pass the link as a bare argument.
    link = args.nxm or next((a for a in qt_args if a.lower().startswith("nxm://")), None)
    qt_args = [a for a in qt_args if a != link]
    if link and ipc.send(link):
        return 0  # The running instance takes it from here.

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setDesktopFileName("modmanager")
    theme.apply(app)
    handler = setup_logging(args.verbose)

    registry = InstanceRegistry()
    path: Path | None = args.instance
    if path is None and not args.pick and registry.last:
        path = registry.valid().get(registry.last)
    if path is None:
        picker = InstancePicker(registry)
        if not picker.exec() or picker.chosen is None:
            return 0
        path = picker.chosen
    try:
        instance = Instance.load(path.expanduser())
    except (OSError, ValueError) as exc:
        QMessageBox.critical(None, APP_NAME, f"Could not open instance:\n{exc}")
        return 1
    registry.add(instance)

    main_window.nxm_router = main_window.NxmRouter()
    listener = ipc.Listener(main_window.nxm_router.link.emit)
    listener.start()
    window = MainWindow(instance, registry, handler)
    window.show()
    if link:
        QTimer.singleShot(300, lambda: main_window.nxm_router.link.emit(link))
    try:
        return app.exec()
    finally:
        listener.stop()


if __name__ == "__main__":
    sys.exit(main())
