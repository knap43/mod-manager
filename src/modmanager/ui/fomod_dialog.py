"""Wizard for FOMOD installers."""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QButtonGroup, QCheckBox, QComboBox, QDialog, QFormLayout, QFrame, QGroupBox,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QRadioButton, QScrollArea, QSizePolicy, QSplitter,
    QTextBrowser, QVBoxLayout, QWidget,
)

from modmanager.core import fomod
from modmanager.core.fomod import FomodSession, ModuleInfo, Option
from modmanager.core.instance import sanitize_name

GROUP_HINTS = {
    fomod.SELECT_ONE: "Select one",
    fomod.SELECT_AT_MOST_ONE: "Select one or none",
    fomod.SELECT_AT_LEAST_ONE: "Select at least one",
    fomod.SELECT_ALL: "All are installed",
    fomod.SELECT_ANY: "Select any",
}
TYPE_HINTS = {
    fomod.REQUIRED: "Required",
    fomod.RECOMMENDED: "Recommended",
    fomod.NOT_USABLE: "Not usable with your setup",
    fomod.COULD_BE_USABLE: "Could be usable — check the description",
}


class FomodDialog(QDialog):
    """Returns Accepted to install, Rejected to cancel, or MANUAL to use the manual installer."""

    MANUAL = 2

    def __init__(self, session: FomodSession, info: ModuleInfo, name: str, existing: list[str], parent=None):
        super().__init__(parent)
        self.session = session
        self.info = info
        self.existing = {n.lower() for n in existing}
        config = session.config
        title = info.name or config.name or name
        self.setWindowTitle(f"Install — {title}")
        self.setMinimumSize(900, 620)

        # Name and header
        self.name = QComboBox()
        self.name.setEditable(True)
        for candidate in dict.fromkeys(n for n in (name, info.name, config.name) if n):
            self.name.addItem(candidate)
        self.name.setCurrentText(name)
        self.name.editTextChanged.connect(self._update_name_status)
        self.merge = QCheckBox("Merge into the existing mod instead of replacing it")
        self.name_status = QLabel()
        self.name_status.setObjectName("hint")
        form = QFormLayout()
        form.addRow("Name", self.name)
        self.exists_row = QWidget()
        row = QHBoxLayout(self.exists_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.name_status)
        row.addWidget(self.merge)
        row.addStretch(1)
        form.addRow("", self.exists_row)

        details = " · ".join(x for x in (
            f"by {info.author}" if info.author else "",
            f"version {info.version}" if info.version else "",
            info.website,
        ) if x)
        header = QLabel(f"<b>{_esc(config.name or title)}</b>" + (f"<br><span>{_esc(details)}</span>" if details else ""))
        header.setTextFormat(Qt.RichText)
        header.setOpenExternalLinks(True)

        self.step_title = QLabel()
        self.step_title.setObjectName("sectionTitle")

        # Options (left) and description (right)
        self.options_host = QWidget()
        self.options_layout = QVBoxLayout(self.options_host)
        self.options_layout.setContentsMargins(0, 0, 6, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setWidget(self.options_host)

        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumHeight(220)
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.image.setStyleSheet("background: #1d1d1d; border: 1px solid #3f3f3f;")
        self.description = QTextBrowser()
        self.description.setOpenExternalLinks(True)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.addWidget(self.image, 3)
        rv.addWidget(self.description, 2)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(scroll)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        split.setSizes([440, 440])

        # Buttons
        manual = QPushButton("Manual…")
        manual.setToolTip("Skip the installer and choose the files yourself")
        manual.clicked.connect(lambda: self.done(self.MANUAL))
        self.back_btn = QPushButton("Back")
        self.back_btn.clicked.connect(self._back)
        self.next_btn = QPushButton("Next")
        self.next_btn.setObjectName("primary")
        self.next_btn.setDefault(True)
        self.next_btn.clicked.connect(self._next)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons = QHBoxLayout()
        buttons.addWidget(manual)
        buttons.addStretch(1)
        for b in (self.back_btn, self.next_btn, cancel):
            buttons.addWidget(b)

        lay = QVBoxLayout(self)
        lay.addLayout(form)
        lay.addWidget(header)
        lay.addWidget(self.step_title)
        lay.addWidget(split, 1)
        lay.addLayout(buttons)

        self._pixmap: QPixmap | None = None
        self._buttons: dict[tuple[int, int], QAbstractButton] = {}
        self._none_buttons: dict[int, QRadioButton] = {}
        self._hover: dict[QAbstractButton, Option] = {}
        self.current = session.next_step(-1)
        self._update_name_status()
        self._show_step()

    # ------------------------------------------------------------------ steps

    def _show_step(self) -> None:
        while self.options_layout.count():
            item = self.options_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._buttons.clear()
        self._none_buttons.clear()
        self._hover.clear()

        if self.current is None:
            self.step_title.setText("Ready to install")
            label = QLabel("This installer has no options to choose from.")
            label.setObjectName("hint")
            self.options_layout.addWidget(label)
            self.options_layout.addStretch(1)
            self._describe(None)
            self._update_nav()
            return

        i = self.current
        step = self.session.config.steps[i]
        sel = self.session.ensure_selection(i)
        first: Option | None = None

        for g, group in enumerate(step.groups):
            box = QGroupBox(group.name or "Options")
            bl = QVBoxLayout(box)
            hint = QLabel(GROUP_HINTS.get(group.type, ""))
            hint.setObjectName("hint")
            bl.addWidget(hint)
            radio = group.type in (fomod.SELECT_ONE, fomod.SELECT_AT_MOST_ONE)
            bgroup = QButtonGroup(box)
            bgroup.setExclusive(radio)
            for o, opt in enumerate(group.options):
                otype = self.session.option_type(i, g, o)
                button: QAbstractButton = QRadioButton(opt.name) if radio else QCheckBox(opt.name)
                locked = otype in (fomod.REQUIRED, fomod.NOT_USABLE) or group.type == fomod.SELECT_ALL
                button.setEnabled(not locked)
                button.setChecked(o in sel[g])
                tip = TYPE_HINTS.get(otype)
                if tip:
                    button.setToolTip(tip)
                    if otype in (fomod.RECOMMENDED, fomod.COULD_BE_USABLE):
                        button.setText(f"{opt.name}   ({tip.split(' —')[0].lower()})")
                button.toggled.connect(lambda on, g=g, o=o: self._toggled(g, o, on))
                button.installEventFilter(self)
                bgroup.addButton(button)
                bl.addWidget(button)
                self._buttons[(g, o)] = button
                self._hover[button] = opt
                if first is None and o in sel[g]:
                    first = opt
            if group.type == fomod.SELECT_AT_MOST_ONE:
                none = QRadioButton("None")
                none.setChecked(not sel[g])
                none.toggled.connect(lambda on, g=g: on and self._select_none(g))
                bgroup.addButton(none)
                bl.addWidget(none)
                self._none_buttons[g] = none
            self.options_layout.addWidget(box)
        self.options_layout.addStretch(1)
        self._describe(first)
        self._update_nav()

    def _toggled(self, g: int, o: int, on: bool) -> None:
        if self.current is None:
            return
        group = self.session.config.steps[self.current].groups[g]
        if not on and group.type in (fomod.SELECT_ONE, fomod.SELECT_AT_MOST_ONE):
            return  # The newly checked radio (or "None") reports the change.
        self.session.select(self.current, g, o, on)
        if on:
            self._describe(self._hover.get(self._buttons[(g, o)]))
        self._sync()

    def _select_none(self, g: int) -> None:
        if self.current is None:
            return
        sel = self.session.ensure_selection(self.current)
        for o in list(sel[g]):
            self.session.select(self.current, g, o, False)
        self._sync()

    def _sync(self) -> None:
        """Reflect the session's (rule-enforced) selection in the widgets."""
        sel = self.session.ensure_selection(self.current)
        for (g, o), button in self._buttons.items():
            button.blockSignals(True)
            button.setChecked(o in sel[g])
            button.blockSignals(False)
        for g, none in self._none_buttons.items():
            none.blockSignals(True)
            none.setChecked(not sel[g])
            none.blockSignals(False)
        self._update_nav()

    def _update_nav(self) -> None:
        cur = -1 if self.current is None else self.current
        if self.current is not None:
            # Later steps can appear or disappear as choices change, so recount every time.
            visible = [j for j in range(len(self.session.config.steps)) if self.session.is_visible(j)]
            position = f"  ({visible.index(cur) + 1} of {len(visible)})" if cur in visible else ""
            self.step_title.setText((self.session.config.steps[cur].name or "Options") + position)
        self.back_btn.setEnabled(self.current is not None and self.session.previous_step(cur) is not None)
        last = self.current is None or self.session.next_step(cur) is None
        self.next_btn.setText("Install" if last else "Next")

    def _next(self) -> None:
        if self.current is not None:
            error = self.session.validate(self.current)
            if error:
                QMessageBox.warning(self, "Installer", error)
                return
            nxt = self.session.next_step(self.current)
            if nxt is not None:
                self.current = nxt
                self._show_step()
                return
        if not self.mod_name:
            QMessageBox.warning(self, "Installer", "Enter a name for the mod.")
            return
        self.accept()

    def _back(self) -> None:
        if self.current is None:
            return
        prev = self.session.previous_step(self.current)
        if prev is not None:
            self.current = prev
            self._show_step()

    # ------------------------------------------------------------ description

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Enter and obj in self._hover:
            self._describe(self._hover[obj])
        return super().eventFilter(obj, event)

    def _describe(self, opt: Option | None) -> None:
        if opt is None:
            self.description.setPlainText(self.info.description or "")
            self._set_image(self.session.config.image)
            return
        self.description.setPlainText(opt.description or opt.name)
        self._set_image(opt.image or self.session.config.image)

    def _set_image(self, rel: str) -> None:
        path = fomod.resolve_ci(self.session.root, rel) if rel else None
        pix = QPixmap(str(path)) if path is not None and path.is_file() else QPixmap()
        self._pixmap = pix if not pix.isNull() else None
        self._scale_image()

    def _scale_image(self) -> None:
        if self._pixmap is None:
            self.image.clear()
            return
        self.image.setPixmap(self._pixmap.scaled(
            self.image.contentsRect().size(), Qt.KeepAspectRatio, Qt.SmoothTransformation,
        ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale_image()

    # ------------------------------------------------------------------- name

    @property
    def mod_name(self) -> str:
        return self.name.currentText().strip()

    @property
    def mode(self) -> str:
        # isVisible() is False for every child once the dialog has closed, so use isHidden().
        return "merge" if not self.exists_row.isHidden() and self.merge.isChecked() else "replace"

    def _update_name_status(self) -> None:
        exists = sanitize_name(self.mod_name).lower() in self.existing
        self.exists_row.setVisible(exists)
        self.name_status.setText("A mod with this name exists and will be replaced unless you merge.")


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
