"""Dialogs: instances, settings (incl. Proton), executables, installing and conflicts."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from modmanager.core import paths, proton, steam
from modmanager.core.games import GAMES, GameDef, get_game
from modmanager.core.installer import has_fomod, looks_like_data
from modmanager.core.instance import DEPLOY_METHODS, Executable, Instance, InstanceRegistry, sanitize_name

METHOD_LABELS = {
    "hardlink": "Hard links (recommended — needs the instance on the same drive as the game)",
    "symlink": "Symbolic links",
    "copy": "Copy files (slow, uses disk space)",
}


def hint(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("hint")
    label.setWordWrap(True)
    return label


def path_row(edit: QLineEdit, directory: bool = True, caption: str = "Select", filter_: str = "") -> QWidget:
    row = QWidget()
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(edit, 1)
    btn = QPushButton("Browse…")

    def browse():
        start = edit.text() or str(Path.home())
        if directory:
            chosen = QFileDialog.getExistingDirectory(row, caption, start)
        else:
            chosen, _ = QFileDialog.getOpenFileName(row, caption, start, filter_)
        if chosen:
            edit.setText(chosen)

    btn.clicked.connect(browse)
    lay.addWidget(btn)
    return row


def combo_value(combo: QComboBox) -> str:
    """Data of the selected item, or the typed text of an editable combo box."""
    if combo.currentIndex() >= 0 and combo.currentText() == combo.itemText(combo.currentIndex()):
        return combo.currentData() or ""
    return combo.currentText().strip()


def set_status(label: QLabel, ok: bool, text: str) -> None:
    label.setObjectName("good" if ok else "bad")
    label.setWordWrap(True)
    label.setText(text)
    label.style().unpolish(label)
    label.style().polish(label)


# ---------------------------------------------------------------------- instances


class NewInstanceDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("New instance")
        self.setMinimumWidth(640)
        self.instance: Instance | None = None

        self.game = QComboBox()
        for g in GAMES.values():
            self.game.addItem(g.name, g.id)
        self.name = QLineEdit()
        self.game_dir = QLineEdit()
        self.data_sub = QLineEdit()
        self.location = QLineEdit()
        self.method = QComboBox()
        for m in DEPLOY_METHODS:
            self.method.addItem(METHOD_LABELS[m], m)
        self.fs_status = QLabel()
        self.fs_status.setWordWrap(True)

        detect = QPushButton("Detect")
        detect.clicked.connect(self._detect)
        game_row = path_row(self.game_dir, caption="Game folder")
        game_row.layout().addWidget(detect)

        form = QFormLayout()
        form.addRow("Game", self.game)
        form.addRow("Instance name", self.name)
        form.addRow("Game folder", game_row)
        self.data_sub_label = QLabel("Mods go into")
        form.addRow(self.data_sub_label, self.data_sub)
        form.addRow("Instance folder", path_row(self.location, caption="Instance folder"))
        form.addRow("Deployment", self.method)
        form.addRow("", self.fs_status)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._create)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(hint(
            "An instance holds the mods, profiles and Wine prefix for one game. "
            "Keep it on the same drive as the game so mods can be hard-linked into the Data folder."
        ))
        lay.addLayout(form)
        lay.addWidget(buttons)

        self.game.currentIndexChanged.connect(self._game_changed)
        self.name.textEdited.connect(self._name_edited)
        self.game_dir.textChanged.connect(self._check_fs)
        self.location.textChanged.connect(self._check_fs)
        self.method.currentIndexChanged.connect(self._check_fs)
        self._location_touched = False
        self.location.textEdited.connect(lambda: setattr(self, "_location_touched", True))
        self._game_changed()

    @property
    def game_def(self) -> GameDef:
        return get_game(self.game.currentData())

    def _game_changed(self) -> None:
        g = self.game_def
        self.name.setText(g.name if g.id != "generic" else "")
        self._name_edited()
        generic = g.id == "generic"
        self.data_sub.setVisible(generic)
        self.data_sub_label.setVisible(generic)
        self.data_sub.setPlaceholderText("Sub-folder of the game folder mods are placed in (empty = game folder)")
        self.game_dir.clear()
        self._detect(quiet=True)

    def _name_edited(self) -> None:
        if not getattr(self, "_location_touched", False):
            name = sanitize_name(self.name.text() or "Instance")
            self.location.setText(str(paths.default_instances_dir() / name))

    def _detect(self, quiet: bool = False) -> None:
        g = self.game_def
        found = steam.find_game(g.steam_app_id, g.steam_dir) if g.steam_app_id else None
        if found:
            self.game_dir.setText(str(found))
        elif not quiet:
            QMessageBox.information(self, "Not found", f"Could not find {g.name} in any Steam library.")

    def _check_fs(self) -> None:
        game_dir, loc = self.game_dir.text().strip(), self.location.text().strip()
        if not game_dir or not loc:
            self.fs_status.clear()
            return
        if self.method.currentData() == "hardlink" and not paths.same_filesystem(Path(game_dir), Path(loc)):
            set_status(self.fs_status, False,
                       "The instance folder is on a different drive than the game, so hard links are "
                       "impossible and symbolic links will be used instead. Choose a folder on the game's "
                       "drive for best compatibility.")
        else:
            set_status(self.fs_status, True, "Instance and game are on the same drive.")

    def _create(self) -> None:
        g = self.game_def
        game_dir = Path(self.game_dir.text().strip()).expanduser()
        location = Path(self.location.text().strip()).expanduser()
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Enter a name for the instance.")
            return
        if not game_dir.is_dir():
            QMessageBox.warning(self, "Game folder", "Select the folder the game is installed in.")
            return
        if g.binary and not paths.find_child_ci(game_dir, g.binary):
            if QMessageBox.question(
                self, "Game folder",
                f"{g.binary} was not found in that folder. Use it anyway?",
            ) != QMessageBox.Yes:
                return
        try:
            self.instance = Instance.create(
                location, name, g.id, game_dir,
                data_subdir=self.data_sub.text().strip() if g.id == "generic" else None,
                deploy_method=self.method.currentData(),
            )
        except OSError as exc:
            QMessageBox.critical(self, "Could not create instance", str(exc))
            return
        self.accept()


class InstancePicker(QDialog):
    """Choose, create or register instances."""

    def __init__(self, registry: InstanceRegistry, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Instances")
        self.setMinimumSize(520, 340)
        self.registry = registry
        self.chosen: Path | None = None

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._open())
        new = QPushButton("New…")
        new.clicked.connect(self._new)
        add = QPushButton("Add existing…")
        add.clicked.connect(self._add)
        remove = QPushButton("Forget")
        remove.clicked.connect(self._remove)
        self.open_btn = QPushButton("Open")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self._open)

        side = QVBoxLayout()
        for b in (new, add, remove):
            side.addWidget(b)
        side.addStretch(1)
        side.addWidget(self.open_btn)
        lay = QHBoxLayout(self)
        lay.addWidget(self.list, 1)
        lay.addLayout(side)
        self._fill()

    def _fill(self) -> None:
        self.list.clear()
        for name, path in self.registry.valid().items():
            item = QListWidgetItem(f"{name}\n{path}")
            item.setData(Qt.UserRole, str(path))
            self.list.addItem(item)
            if name == self.registry.last:
                self.list.setCurrentItem(item)
        if self.list.currentRow() < 0 and self.list.count():
            self.list.setCurrentRow(0)
        self.open_btn.setEnabled(self.list.count() > 0)

    def _new(self) -> None:
        dlg = NewInstanceDialog(self)
        if dlg.exec() and dlg.instance:
            self.registry.add(dlg.instance)
            self.chosen = dlg.instance.path
            self.accept()

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Instance folder", str(paths.default_instances_dir()))
        if not folder:
            return
        try:
            inst = Instance.load(Path(folder))
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Not an instance", str(exc))
            return
        self.registry.add(inst)
        self._fill()

    def _remove(self) -> None:
        item = self.list.currentItem()
        if item is None:
            return
        name = item.text().split("\n", 1)[0]
        if QMessageBox.question(
            self, "Forget instance",
            f"Remove '{name}' from the list? Its files stay on disk.",
        ) == QMessageBox.Yes:
            self.registry.remove(name)
            self._fill()

    def _open(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self.chosen = Path(item.data(Qt.UserRole))
            self.accept()


# ----------------------------------------------------------------------- settings


class SettingsDialog(QDialog):
    def __init__(self, instance: Instance, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Settings — {instance.name}")
        self.setMinimumWidth(700)
        self.instance = instance
        cfg = instance.config
        pcfg = instance.proton

        tabs = QTabWidget()

        # General
        general = QWidget()
        form = QFormLayout(general)
        self.game_dir = QLineEdit(cfg.get("game_dir", ""))
        self.data_sub = QLineEdit(cfg.get("data_subdir", ""))
        self.method = QComboBox()
        for m in DEPLOY_METHODS:
            self.method.addItem(METHOD_LABELS[m], m)
        self.method.setCurrentIndex(DEPLOY_METHODS.index(instance.deploy_method))
        form.addRow("Game", QLabel(instance.game.name))
        inst_path = QLineEdit(str(instance.path))
        inst_path.setReadOnly(True)
        form.addRow("Instance folder", inst_path)
        form.addRow("Game folder", path_row(self.game_dir, caption="Game folder"))
        form.addRow("Mods go into", self.data_sub)
        form.addRow("Deployment", self.method)
        form.addRow("", hint(
            "Mods are linked into the game's folder when you deploy or run a program, and removed "
            "again by Purge. Game files a mod replaces are backed up and restored on purge."
        ))
        tabs.addTab(general, "General")

        # Proton
        prot = QWidget()
        pform = QFormLayout(prot)
        self.umu = QLineEdit(pcfg.get("umu_run", ""))
        self.umu.setPlaceholderText("Auto-detect from PATH")
        self.umu_status = QLabel()
        self.umu.textChanged.connect(self._check_umu)
        self.prefix = QLineEdit(pcfg.get("prefix", ""))
        self.prefix_status = QLabel()
        self.prefix.textChanged.connect(self._check_prefix)
        prefix_btns = QHBoxLayout()
        own = QPushButton("Use instance prefix")
        own.clicked.connect(lambda: self.prefix.setText(str(instance.path / "prefix")))
        prefix_btns.addWidget(own)
        steam_btn = QPushButton("Use Steam's prefix")
        steam_btn.setEnabled(bool(instance.game.steam_app_id))
        steam_btn.clicked.connect(self._use_steam_prefix)
        prefix_btns.addWidget(steam_btn)
        prefix_btns.addStretch(1)

        self.proton_path = QComboBox()
        self.proton_path.setEditable(True)
        self.proton_path.addItem("UMU-Proton (umu default)", "")
        self.proton_path.addItem("GE-Proton (latest, downloaded automatically)", "GE-Proton")
        for build in steam.proton_builds():
            self.proton_path.addItem(f"{build.name}  —  {build.path}", str(build.path))
        current = pcfg.get("proton_path", "")
        idx = self.proton_path.findData(current)
        if idx >= 0:
            self.proton_path.setCurrentIndex(idx)
        else:
            self.proton_path.setEditText(current)
        self.game_id = QLineEdit(pcfg.get("game_id", instance.game.umu_id))
        self.store = QComboBox()
        self.store.setEditable(True)
        for s in ("", "steam", "gog", "egs", "none"):
            self.store.addItem(s or "(unset)", s)
        idx = self.store.findData(pcfg.get("store", ""))
        self.store.setCurrentIndex(max(idx, 0))
        self.env = QPlainTextEdit("\n".join(f"{k}={v}" for k, v in (pcfg.get("env") or {}).items()))
        self.env.setPlaceholderText("KEY=VALUE per line, e.g.\nDXVK_HUD=fps\nPROTON_LOG=1")
        self.env.setMaximumHeight(110)

        pform.addRow("umu-run", path_row(self.umu, directory=False, caption="umu-run executable"))
        pform.addRow("", self.umu_status)
        pform.addRow("Wine prefix", path_row(self.prefix, caption="Wine prefix"))
        pform.addRow("", prefix_btns)
        pform.addRow("", self.prefix_status)
        pform.addRow("Proton", self.proton_path)
        pform.addRow("GAMEID", self.game_id)
        pform.addRow("STORE", self.store)
        pform.addRow("Environment", self.env)
        pform.addRow("", hint(
            "Windows programs run as: WINEPREFIX=… GAMEID=… PROTONPATH=… umu-run program.exe. "
            "GAMEID selects protonfixes from the umu database; for Steam games it is umu-<appid>, "
            "which also tells the Steam API which game is running. The Steam version of the game "
            "needs Steam to be running."
        ))
        tabs.addTab(prot, "Proton")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
        self._check_umu()
        self._check_prefix()
        self.new_method: str | None = None

    def _check_umu(self) -> None:
        found = proton.find_umu_run(self.umu.text().strip())
        if found:
            set_status(self.umu_status, True, f"Using {found}")
        else:
            set_status(self.umu_status, False,
                       "umu-run not found — install umu-launcher from your distribution or "
                       "github.com/Open-Wine-Components/umu-launcher.")

    def _check_prefix(self) -> None:
        text = self.prefix.text().strip()
        if not text:
            set_status(self.prefix_status, False, "A prefix is required to run Windows programs.")
        elif proton.prefix_initialized(Path(text).expanduser()):
            set_status(self.prefix_status, True, "Existing prefix.")
        else:
            set_status(self.prefix_status, True, "Will be created on first run.")

    def _use_steam_prefix(self) -> None:
        found = steam.find_compatdata(self.instance.game.steam_app_id)
        if found is None:
            QMessageBox.information(
                self, "Not found",
                "Steam has not created a prefix for this game yet. Run it once from Steam first.",
            )
            return
        self.prefix.setText(str(found))

    def _save(self) -> None:
        cfg = self.instance.config
        pcfg = self.instance.proton
        game_dir = Path(self.game_dir.text().strip()).expanduser()
        if not game_dir.is_dir():
            QMessageBox.warning(self, "Game folder", "The game folder does not exist.")
            return
        env: dict[str, str] = {}
        for line in self.env.toPlainText().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                QMessageBox.warning(self, "Environment", f"Expected KEY=VALUE, got: {line}")
                return
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
        cfg["game_dir"] = str(game_dir)
        cfg["data_subdir"] = self.data_sub.text().strip()
        pcfg["umu_run"] = self.umu.text().strip()
        pcfg["prefix"] = self.prefix.text().strip()
        pcfg["proton_path"] = combo_value(self.proton_path)
        pcfg["game_id"] = self.game_id.text().strip() or self.instance.game.umu_id
        pcfg["store"] = combo_value(self.store)
        pcfg["env"] = env
        self.instance.save()
        method = self.method.currentData()
        self.new_method = method if method != self.instance.deploy_method else None
        self.accept()


# -------------------------------------------------------------------- executables


class ExecutablesDialog(QDialog):
    def __init__(self, instance: Instance, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Executables")
        self.setMinimumSize(720, 380)
        self.instance = instance
        self.exes = instance.executables

        self.list = QListWidget()
        self.list.currentRowChanged.connect(self._load_row)
        self.name = QLineEdit()
        self.path = QLineEdit()
        self.args = QLineEdit()
        self.cwd = QLineEdit()
        self.cwd.setPlaceholderText("Default: the program's folder")
        self.use_proton = QCheckBox("Run through Proton (umu-run) — automatic for .exe/.bat/.msi")
        for w in (self.name, self.path, self.args, self.cwd):
            w.textEdited.connect(self._store_row)
        self.use_proton.toggled.connect(self._store_row)

        add = QPushButton("Add")
        add.clicked.connect(self._add)
        remove = QPushButton("Remove")
        remove.clicked.connect(self._remove)
        up = QPushButton("Up")
        up.clicked.connect(lambda: self._move(-1))
        down = QPushButton("Down")
        down.clicked.connect(lambda: self._move(1))
        reset = QPushButton("Add defaults")
        reset.clicked.connect(self._defaults)

        form = QFormLayout()
        form.addRow("Title", self.name)
        form.addRow("Program", path_row(self.path, directory=False, caption="Program",
                                        filter_="Programs (*.exe *.bat *.msi *.sh);;All files (*)"))
        form.addRow("Arguments", self.args)
        form.addRow("Start in", path_row(self.cwd, caption="Working folder"))
        form.addRow("", self.use_proton)
        form.addRow("", hint("Tip: add skse64_loader.exe from the game folder to start Skyrim with SKSE."))
        self.form_widget = QWidget()
        self.form_widget.setLayout(form)

        left = QVBoxLayout()
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        for b in (add, remove, up, down):
            row.addWidget(b)
        left.addLayout(row)
        left.addWidget(reset)
        body = QHBoxLayout()
        body.addLayout(left, 2)
        body.addWidget(self.form_widget, 3)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(body)
        lay.addWidget(buttons)
        self._fill()

    def _fill(self, select: int = 0) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        for e in self.exes:
            self.list.addItem(e.name or "(untitled)")
        self.list.blockSignals(False)
        if self.exes:
            self.list.setCurrentRow(max(0, min(select, len(self.exes) - 1)))
        self._load_row(self.list.currentRow())

    def _load_row(self, row: int) -> None:
        self.form_widget.setEnabled(0 <= row < len(self.exes))
        e = self.exes[row] if 0 <= row < len(self.exes) else Executable("", "")
        for w, v in ((self.name, e.name), (self.path, e.path), (self.args, e.args), (self.cwd, e.cwd)):
            w.blockSignals(True)
            w.setText(v)
            w.blockSignals(False)
        self.use_proton.blockSignals(True)
        self.use_proton.setChecked(e.use_proton)
        self.use_proton.blockSignals(False)

    def _store_row(self, *_):
        row = self.list.currentRow()
        if not 0 <= row < len(self.exes):
            return
        e = self.exes[row]
        path_changed = e.path != self.path.text()
        e.name, e.path, e.args, e.cwd = self.name.text(), self.path.text(), self.args.text(), self.cwd.text()
        if path_changed and not e.name and e.path:
            e.name = Path(e.path).stem
            self.name.setText(e.name)
        e.use_proton = self.use_proton.isChecked()
        self.list.item(row).setText(e.name or "(untitled)")

    def _add(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Program", str(self.instance.game_dir), "Programs (*.exe *.bat *.msi *.sh);;All files (*)"
        )
        if not path:
            return
        self.exes.append(Executable(Path(path).stem, path, use_proton=proton.needs_proton(path)))
        self._fill(len(self.exes) - 1)

    def _remove(self) -> None:
        row = self.list.currentRow()
        if 0 <= row < len(self.exes):
            del self.exes[row]
            self._fill(row)

    def _move(self, delta: int) -> None:
        row = self.list.currentRow()
        new = row + delta
        if 0 <= row < len(self.exes) and 0 <= new < len(self.exes):
            self.exes[row], self.exes[new] = self.exes[new], self.exes[row]
            self._fill(new)

    def _defaults(self) -> None:
        have = {e.path for e in self.exes}
        self.exes += [e for e in self.instance.default_executables() if e.path not in have]
        self._fill(len(self.exes) - 1)

    def _save(self) -> None:
        self.instance.executables = [e for e in self.exes if e.path]
        self.instance.save()
        self.accept()


# --------------------------------------------------------------------- installing


class InstallDialog(QDialog):
    """Name the mod and confirm which folder of the archive maps onto Data."""

    def __init__(self, staging: Path, root: Path, confident: bool, name: str, game: GameDef,
                 existing: list[str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Install mod")
        self.setMinimumSize(620, 520)
        self.staging = staging
        self.game = game
        self.root = root
        self.existing = {n.lower() for n in existing}

        self.name = QLineEdit(name)
        self.name.textChanged.connect(self._update_name_status)
        self.name_status = QLabel()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Archive contents", ""])
        self.tree.setColumnWidth(0, 400)
        self.tree.itemDoubleClicked.connect(lambda item, _c: self._set_root_item(item))
        self.root_status = QLabel()
        self.root_status.setWordWrap(True)
        set_root = QPushButton("Use selected folder as Data")
        set_root.clicked.connect(lambda: self._set_root_item(self.tree.currentItem()))

        top = QFormLayout()
        top.addRow("Name", self.name)
        top.addRow("", self.name_status)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        if has_fomod(staging) or any(has_fomod(Path(e.path)) for e in os.scandir(staging) if e.is_dir()):
            lay.addWidget(hint(
                "This archive contains a FOMOD installer, which is not supported yet. Pick the folder "
                "holding the option you want (it should contain meshes, textures, plugins and so on)."
            ))
        lay.addWidget(self.tree, 1)
        row = QHBoxLayout()
        row.addWidget(self.root_status, 1)
        row.addWidget(set_root)
        lay.addLayout(row)
        self.merge = QCheckBox("Merge into the existing mod instead of replacing it")
        lay.addWidget(self.merge)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._items: dict[Path, QTreeWidgetItem] = {}
        top_item = QTreeWidgetItem(["<archive root>", ""])
        top_item.setData(0, Qt.UserRole, str(staging))
        self.tree.addTopLevelItem(top_item)
        self._items[staging] = top_item
        self._populate(staging, top_item, 0)
        top_item.setExpanded(True)
        self._set_root(root)
        self._update_name_status()

    def _populate(self, folder: Path, parent: QTreeWidgetItem, depth: int) -> None:
        try:
            entries = sorted(os.scandir(folder), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for i, e in enumerate(entries):
            if i >= 300:
                parent.addChild(QTreeWidgetItem([f"… {len(entries) - i} more", ""]))
                break
            item = QTreeWidgetItem([e.name + ("/" if e.is_dir() else ""), ""])
            parent.addChild(item)
            if e.is_dir():
                item.setData(0, Qt.UserRole, e.path)
                self._items[Path(e.path)] = item
                if depth < 6:
                    self._populate(Path(e.path), item, depth + 1)
                if depth < 1:
                    item.setExpanded(True)

    def _set_root_item(self, item: QTreeWidgetItem | None) -> None:
        if item is None or not item.data(0, Qt.UserRole):
            return
        self._set_root(Path(item.data(0, Qt.UserRole)))

    def _set_root(self, root: Path) -> None:
        old = self._items.get(self.root)
        if old is not None:
            old.setText(1, "")
            f = old.font(0)
            f.setBold(False)
            old.setFont(0, f)
        self.root = root
        item = self._items.get(root)
        if item is not None:
            item.setText(1, "← Data")
            f = QFont(item.font(0))
            f.setBold(True)
            item.setFont(0, f)
            parent = item.parent()
            while parent is not None:
                parent.setExpanded(True)
                parent = parent.parent()
            item.setExpanded(True)
            self.tree.setCurrentItem(item)
            self.tree.scrollToItem(item)
        if not self.game.data_dirs:
            set_status(self.root_status, True, "The selected folder's contents will be placed in the game's mod folder.")
        elif looks_like_data(root, self.game):
            set_status(self.root_status, True, "Looks right: the selected folder contains game data.")
        else:
            set_status(self.root_status, False,
                       "No meshes, textures, plugins or other game data at this level. "
                       "Select the folder that maps onto Data.")

    def _update_name_status(self) -> None:
        exists = sanitize_name(self.name.text()).lower() in self.existing
        self.merge.setVisible(exists)
        if exists:
            set_status(self.name_status, False, "A mod with this name exists and will be replaced.")
        else:
            self.name_status.clear()

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Name", "Enter a name for the mod.")
            return
        self.accept()

    @property
    def mode(self) -> str:
        return "merge" if self.merge.isVisible() and self.merge.isChecked() else "replace"


# ---------------------------------------------------------------------- conflicts


class ConflictsDialog(QDialog):
    def __init__(self, name: str, won: list[tuple[str, str]], lost: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Conflicts — {name}")
        self.setMinimumSize(760, 480)
        tabs = QTabWidget()
        for title, rows, header in (
            (f"Winning ({len(won)})", won, "Overwrites"),
            (f"Losing ({len(lost)})", lost, "Overwritten by"),
        ):
            tree = QTreeWidget()
            tree.setRootIsDecorated(False)
            tree.setAlternatingRowColors(True)
            tree.setHeaderLabels(["File", header])
            tree.setColumnWidth(0, 460)
            tree.addTopLevelItems([QTreeWidgetItem([f, m]) for f, m in rows])
            tabs.addTab(tree, title)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)
