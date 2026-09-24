"""Item models for the mod list and the plugin list."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QByteArray, QMimeData, QModelIndex, QPointF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetricsF, QPainter, QPalette, QPolygonF
from PySide6.QtWidgets import QApplication, QStyle, QStyledItemDelegate, QStyleOptionViewItem

from modmanager.core.modlist import OVERWRITE, Mod, ModList
from modmanager.core.plugins import PluginEntry, assign_load_indices, partition_masters
from modmanager.ui import theme

MOD_MIME = "application/x-modmanager-mods"
COLLAPSED_ROLE = Qt.UserRole + 10  # True/False for separators, None for everything else
PLUGIN_MIME = "application/x-modmanager-plugins"


class _ReorderModel(QAbstractTableModel):
    """Shared drag-and-drop plumbing: rows are moved in ``dropMimeData``."""

    mime_type = ""

    def supportedDropActions(self):
        return Qt.MoveAction

    def supportedDragActions(self):
        return Qt.MoveAction

    def mimeTypes(self):
        return [self.mime_type]

    def mimeData(self, indexes):
        rows = sorted({i.row() for i in indexes})
        data = QMimeData()
        data.setData(self.mime_type, QByteArray(",".join(map(str, rows)).encode()))
        return data

    def canDropMimeData(self, data, action, row, column, parent):
        return data.hasFormat(self.mime_type) and not self.locked

    def dropMimeData(self, data, action, row, column, parent):
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        rows = [int(r) for r in bytes(data.data(self.mime_type)).decode().split(",") if r]
        if row < 0:
            row = parent.row() if parent.isValid() else self.rowCount()
        self.move_rows(rows, row)
        return True

    def removeRows(self, row, count, parent=QModelIndex()):
        # The view asks to delete the dragged rows after a move; we already moved them.
        return False

    locked = False

    def move_rows(self, rows: list[int], dest: int) -> None:
        raise NotImplementedError


class ModListModel(_ReorderModel):
    COLUMNS = ("Mod", "Conflicts", "Version", "Priority")
    mime_type = MOD_MIME
    changed = Signal()  # Emitted after enabling/disabling or reordering.
    collapsed_changed = Signal()  # A separator was collapsed or expanded.

    def __init__(self, modlist: ModList):
        super().__init__()
        self.modlist = modlist
        self.highlight: str | None = None

    def set_modlist(self, modlist: ModList) -> None:
        self.beginResetModel()
        self.modlist = modlist
        self.highlight = None
        self.endResetModel()

    def reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def mod_at(self, row: int) -> Mod | None:
        mods = self.modlist.mods
        if 0 <= row < len(mods):
            return mods[row]
        if row == len(mods):
            return self.modlist.overwrite
        return None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.modlist.mods) + 1

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        mod = self.mod_at(index.row())
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if mod is None or mod.is_overwrite:
            return base
        base |= Qt.ItemIsDragEnabled
        if index.column() == 0 and not mod.is_separator:
            base |= Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        mod = self.mod_at(index.row())
        if mod is None:
            return None
        col = index.column()
        info = self.modlist.conflicts.get(mod.name)

        if role == Qt.DisplayRole:
            if col == 0:
                if mod.is_separator:
                    total, _ = self.group_counts(mod)
                    return f"{mod.display_name}  ({total})"  # SeparatorDelegate draws the arrow
                return mod.display_name
            if col == 1:
                return _conflict_symbol(info) if mod.enabled or mod.is_overwrite else ""
            if col == 2:
                if mod.meta.get("nexus_update"):
                    return f"{mod.version or '?'}  ↑ {mod.meta.get('nexus_latest_version') or 'new'}"
                return mod.version
            if col == 3:
                return "" if mod.is_overwrite or mod.is_separator else str(index.row())
        elif role == Qt.CheckStateRole and col == 0 and not (mod.is_separator or mod.is_overwrite):
            return Qt.Checked if mod.enabled else Qt.Unchecked
        elif role == Qt.ForegroundRole:
            if col == 2 and mod.meta.get("nexus_update"):
                return QBrush(QColor(theme.WARN))
            if col == 1 and info:
                if info.redundant:
                    return QBrush(QColor(theme.BAD))
                if info.overwrites and info.overwritten_by:
                    return QBrush(QColor(theme.WARN))
                if info.overwrites:
                    return QBrush(QColor(theme.GOOD))
                if info.overwritten_by:
                    return QBrush(QColor(theme.BAD))
            if mod.is_separator:
                return QBrush(QColor("#9bb4d4"))
            if not mod.enabled and not mod.is_overwrite:
                return QBrush(QColor(theme.TEXT_DIM))
        elif role == Qt.BackgroundRole:
            if mod.is_separator:
                return QBrush(QColor(theme.SEPARATOR_BG))
            hl = self.modlist.conflicts.get(self.highlight) if self.highlight else None
            if hl and mod.name != self.highlight:
                if mod.name in hl.overwrites:
                    return QBrush(QColor(theme.WIN_BG))
                if mod.name in hl.overwritten_by:
                    return QBrush(QColor(theme.LOSE_BG))
        elif role == Qt.FontRole:
            if mod.is_separator:
                f = QFont()
                f.setBold(True)
                return f
            if mod.is_overwrite:
                f = QFont()
                f.setItalic(True)
                return f
        elif role == Qt.ToolTipRole:
            if mod.is_separator:
                total, enabled = self.group_counts(mod)
                state = "click the arrow or double-click to expand" if self.modlist.is_collapsed(mod) \
                    else "click the arrow or double-click to collapse"
                return f"{mod.display_name}\n{total} mod(s), {enabled} enabled\n({state})"
            return _mod_tooltip(mod, info)
        elif role == Qt.TextAlignmentRole and col in (1, 3):
            return int(Qt.AlignCenter)
        elif role == Qt.UserRole:
            return mod.name
        elif role == COLLAPSED_ROLE and col == 0 and mod.is_separator:
            return self.modlist.is_collapsed(mod)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or self.locked:
            return False
        mod = self.mod_at(index.row())
        if mod is None or mod.is_overwrite or mod.is_separator:
            return False
        self.set_enabled([mod.name], Qt.CheckState(value) == Qt.Checked)
        return True

    def set_enabled(self, names: list[str], enabled: bool) -> None:
        self.modlist.set_enabled(names, enabled)
        self._refresh_all()

    def move_rows(self, rows: list[int], dest: int) -> None:
        names = [m.name for r in rows if (m := self.mod_at(r)) and not m.is_overwrite]
        if not names:
            return
        dest = min(dest, len(self.modlist.mods))
        if 0 < dest <= len(self.modlist.mods):
            dest = self.modlist.drop_index(dest)
        moving = self.modlist.with_collapsed_members(names)
        self.modlist.move(moving, dest)
        # A mod that lands in a collapsed group would vanish from view: open that group.
        opened = False
        for name in names:
            mod = self.modlist.get(name)
            if mod is None or mod.is_separator:
                continue
            sep = self.modlist.separator_of(self.modlist.index_of(name))
            if sep is not None and self.modlist.is_collapsed(sep) and sep.name not in moving:
                self.modlist.set_collapsed([sep.name], False)
                opened = True
        self._refresh_all()
        if opened:
            self.collapsed_changed.emit()

    def toggle_collapsed(self, name: str, collapsed: bool | None = None) -> None:
        mod = self.modlist.get(name)
        if mod is None or not mod.is_separator:
            return
        state = not self.modlist.is_collapsed(mod) if collapsed is None else collapsed
        self.modlist.set_collapsed([name], state)
        row = self.modlist.index_of(name)
        self.dataChanged.emit(self.index(row, 0), self.index(row, self.columnCount() - 1))
        self.collapsed_changed.emit()

    def set_all_collapsed(self, collapsed: bool) -> None:
        self.modlist.set_collapsed([m.name for m in self.modlist.mods if m.is_separator], collapsed)
        self._emit_all()
        self.collapsed_changed.emit()

    def group_counts(self, mod: Mod) -> tuple[int, int]:
        members = self.modlist.group_members(mod.name)
        return len(members), sum(1 for m in members if m.enabled)

    def set_highlight(self, name: str | None) -> None:
        if name != self.highlight:
            self.highlight = name
            self._emit_all()

    def _refresh_all(self) -> None:
        self.layoutAboutToBeChanged.emit()
        self.layoutChanged.emit()
        self._emit_all()
        self.changed.emit()

    def _emit_all(self) -> None:
        if self.rowCount():
            self.dataChanged.emit(self.index(0, 0), self.index(self.rowCount() - 1, self.columnCount() - 1))


def _conflict_symbol(info) -> str:
    if not info:
        return ""
    if info.redundant:
        return "✕"
    if info.overwrites and info.overwritten_by:
        return "±"
    if info.overwrites:
        return "+"
    if info.overwritten_by:
        return "−"
    return ""


def _mod_tooltip(mod: Mod, info) -> str:
    if mod.is_separator:
        return "Separator"
    lines = [mod.display_name]
    if mod.is_overwrite:
        lines.append("Files created by the game and tools while running.")
    if mod.meta.get("source"):
        lines.append(f"Installed from: {mod.meta['source']}")
    if mod.meta.get("nexus_id"):
        lines.append(f"Nexus Mods ID: {mod.meta['nexus_id']}")
    if mod.meta.get("nexus_update"):
        lines.append(f"Update available on Nexus Mods: {mod.meta.get('nexus_latest_version') or 'newer file'}")
    if info:
        lines.append(f"{info.total_files} files")
        if info.overwrites:
            lines.append(f"Overwrites {info.winning_files} file(s) of: " + ", ".join(sorted(info.overwrites)))
        if info.overwritten_by:
            lines.append(
                f"{info.losing_files} file(s) overwritten by: " + ", ".join(sorted(info.overwritten_by))
            )
        if info.redundant:
            lines.append("Every file of this mod is overwritten — it has no effect.")
    return "\n".join(lines)


class PluginListModel(_ReorderModel):
    COLUMNS = ("Plugin", "Type", "Index", "Mod")
    mime_type = PLUGIN_MIME
    changed = Signal()

    def __init__(self, modlist: ModList, light_supported: bool):
        super().__init__()
        self.modlist = modlist
        self.light_supported = light_supported
        self.entries: list[PluginEntry] = []

    def set_entries(self, entries: list[PluginEntry], modlist: ModList | None = None) -> None:
        self.beginResetModel()
        if modlist is not None:
            self.modlist = modlist
        self.entries = entries
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.entries)

    def columnCount(self, parent=QModelIndex()):
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        e = self.entries[index.row()]
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if e.implicit:
            return base
        base |= Qt.ItemIsDragEnabled
        if index.column() == 0:
            base |= Qt.ItemIsUserCheckable
        return base

    def data(self, index, role=Qt.DisplayRole):
        e = self.entries[index.row()]
        col = index.column()
        problem = bool(e.missing_masters or e.late_masters)
        if role == Qt.DisplayRole:
            if col == 0:
                return ("⚠ " if problem else "") + e.name
            if col == 1:
                return e.kind
            if col == 2:
                return e.load_index
            if col == 3:
                return e.origin or "<game>"
        elif role == Qt.CheckStateRole and col == 0:
            return Qt.Checked if e.enabled else Qt.Unchecked
        elif role == Qt.ForegroundRole:
            if col == 0 and problem:
                return QBrush(QColor(theme.BAD))
            if not e.enabled or e.implicit or (col == 3 and not e.origin):
                return QBrush(QColor(theme.TEXT_DIM))
        elif role == Qt.FontRole and col == 0 and e.is_master:
            f = QFont()
            f.setBold(e.name.lower().endswith(".esm"))
            return f
        elif role == Qt.ToolTipRole:
            lines = [e.name, e.kind]
            if e.implicit:
                lines.append("Always loaded by the game.")
            if e.masters:
                lines.append("Masters: " + ", ".join(e.masters))
            if e.missing_masters:
                lines.append("Missing masters: " + ", ".join(e.missing_masters))
            if e.late_masters:
                lines.append("Loaded before its masters: " + ", ".join(e.late_masters))
            return "\n".join(lines)
        elif role == Qt.TextAlignmentRole and col == 2:
            return int(Qt.AlignCenter)
        return None

    def setData(self, index, value, role=Qt.EditRole):
        if role != Qt.CheckStateRole or self.locked:
            return False
        e = self.entries[index.row()]
        if e.implicit:
            return False
        e.enabled = Qt.CheckState(value) == Qt.Checked
        self._commit()
        return True

    def set_enabled(self, rows: list[int], enabled: bool) -> None:
        for r in rows:
            if 0 <= r < len(self.entries) and not self.entries[r].implicit:
                self.entries[r].enabled = enabled
        self._commit()

    def move_rows(self, rows: list[int], dest: int) -> None:
        moving = [self.entries[r] for r in rows if 0 <= r < len(self.entries) and not self.entries[r].implicit]
        if not moving:
            return
        before = sum(1 for r in rows if r < dest)
        moving_ids = {id(e) for e in moving}
        rest = [e for e in self.entries if id(e) not in moving_ids]
        dest = max(0, min(len(rest), dest - before))
        self.layoutAboutToBeChanged.emit()
        self.entries = partition_masters(rest[:dest] + moving + rest[dest:])
        self.layoutChanged.emit()
        self._commit()

    def _commit(self) -> None:
        assign_load_indices(self.entries, self.light_supported)
        self.modlist.save_plugin_order(self.entries)
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, len(self.COLUMNS) - 1))
        self.changed.emit()


class SeparatorDelegate(QStyledItemDelegate):
    """Draws the collapse arrow of separators as a triangle as tall as the title's capitals."""

    LEFT = 6    # Padding before the arrow
    GAP = 8     # Space between arrow and title

    @classmethod
    def arrow_zone(cls, font: QFont) -> float:
        """Width from the cell's left edge that counts as clicking the arrow."""
        return cls.LEFT + QFontMetricsF(font).capHeight() + cls.GAP

    def paint(self, painter, option, index):
        collapsed = index.data(COLLAPSED_ROLE)
        if collapsed is None:
            return super().paint(painter, option, index)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, opt.widget)

        fm = QFontMetricsF(opt.font)
        cap = fm.capHeight()
        # Centre the arrow on the capitals: the text line is centred in the cell.
        baseline = opt.rect.top() + (opt.rect.height() - fm.height()) / 2 + fm.ascent()
        cy = baseline - cap / 2
        x = opt.rect.left() + self.LEFT
        if collapsed:  # ▶: as tall as a capital letter
            w = cap * 0.866
            points = [QPointF(x, cy - cap / 2), QPointF(x + w, cy), QPointF(x, cy + cap / 2)]
        else:          # ▼: as wide as a capital letter is tall
            h = cap * 0.866
            points = [QPointF(x, cy - h / 2), QPointF(x + cap, cy - h / 2), QPointF(x + cap / 2, cy + h / 2)]
        brush = index.data(Qt.ForegroundRole)
        color = brush.color() if isinstance(brush, QBrush) else opt.palette.color(QPalette.Text)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF(points))
        painter.restore()

        text_opt = QStyleOptionViewItem(option)
        text_opt.rect = option.rect.adjusted(int(self.arrow_zone(opt.font)), 0, 0, 0)
        super().paint(painter, text_opt, index)


__all__ = ["ModListModel", "PluginListModel", "SeparatorDelegate", "OVERWRITE"]
