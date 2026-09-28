"""补片面板(模型树): 显隐、颜色、类型、面数, 以及补片重命名。

与三维窗口是双向联动的:
* 在三维窗口里单击某个补片表面 -> 这里对应的行被选中;
* 在这里选中某一行        -> 三维窗口里该补片高亮(粗橙边);
* 双击"补片"列改名(或点"重命名…")-> 主窗口会同步改网格 boundary 与所有场的
  boundaryField, 并把新的 boundary 文件一起写出去。
"""

from __future__ import annotations

from PyQt6 import QtCore, QtGui, QtWidgets

from ..foam.polymesh import PolyMesh
from .widgets import ColorButton

__all__ = ["PatchPanel"]

#: 可以在界面里选择的补片网格类型(常用的几种)
PATCH_MESH_TYPES = [
    "patch", "wall", "empty", "wedge", "symmetry", "symmetryPlane", "cyclic", "cyclicAMI",
]


class _NameOnlyDelegate(QtWidgets.QStyledItemDelegate):
    """只允许编辑"补片"列(第 1 列)。"""

    def createEditor(self, parent, option, index):  # noqa: N802
        if index.column() != 1:
            return None
        return super().createEditor(parent, option, index)


class PatchPanel(QtWidgets.QWidget):
    patchSelected = QtCore.pyqtSignal(str)
    patchVisibilityChanged = QtCore.pyqtSignal(str, bool)
    patchColorChanged = QtCore.pyqtSignal(str, tuple)
    patchRenameRequested = QtCore.pyqtSignal(str, str)
    patchTypeChangeRequested = QtCore.pyqtSignal(str, str)
    volumeColorToggled = QtCore.pyqtSignal(bool)
    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mesh: PolyMesh | None = None
        self._items: dict[str, QtWidgets.QTreeWidgetItem] = {}
        self._loading = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(4)

        head = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel("边界补片")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        head.addWidget(title)
        self.lbl_stats = QtWidgets.QLabel("尚未加载网格")
        self.lbl_stats.setEnabled(False)
        head.addWidget(self.lbl_stats, 1)
        lay.addLayout(head)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["显示", "补片", "类型", "面数", "颜色"])
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setItemDelegate(_NameOnlyDelegate(self.tree))
        self.tree.setEditTriggers(
            QtWidgets.QAbstractItemView.EditTrigger.DoubleClicked
            | QtWidgets.QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.tree.setColumnWidth(0, 40)
        self.tree.setColumnWidth(1, 118)
        self.tree.setColumnWidth(2, 56)
        self.tree.setColumnWidth(3, 52)
        self.tree.setColumnWidth(4, 40)
        self.tree.setMinimumHeight(150)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.tree.itemSelectionChanged.connect(self._on_selection_changed)
        lay.addWidget(self.tree, 1)

        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        for text, tip, slot in (
            ("全选", "显示所有补片", lambda: self._set_all(True)),
            ("全不选", "隐藏所有补片", lambda: self._set_all(False)),
            ("重命名…", "给选中的补片改名(也可以直接双击上表的名字)", self._rename_dialog),
        ):
            b = QtWidgets.QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            row.addWidget(b)
        self.cb_volume = QtWidgets.QCheckBox("按体积着色")
        self.cb_volume.setToolTip("用颜色表示单元体积大小, 快速看网格疏密")
        self.cb_volume.toggled.connect(self.volumeColorToggled)
        row.addWidget(self.cb_volume)
        row.addStretch(1)
        lay.addLayout(row)

        hint = QtWidgets.QLabel(
            "提示: 在三维窗口里单击补片表面即可选中; 双击名字可改名; "
            "“类型”列可以直接改补片的网格类型(会同步写入 polyMesh/boundary)"
        )
        hint.setWordWrap(True)
        hint.setEnabled(False)
        lay.addWidget(hint)

        self.setEnabled(False)

    # ------------------------------------------------------------------
    def set_mesh(self, mesh: PolyMesh | None, colors: dict | None = None) -> None:
        self.mesh = mesh
        self._loading = True
        self.tree.clear()
        self._items.clear()
        if mesh is None:
            self.lbl_stats.setText("尚未加载网格")
            self._loading = False
            self.setEnabled(False)
            return
        self.setEnabled(True)
        self.lbl_stats.setText(
            f"{mesh.n_cells:,} 单元 / {mesh.n_faces:,} 面 / {len(mesh.patches)} 个补片"
        )
        for patch in mesh.patches:
            item = QtWidgets.QTreeWidgetItem(["", patch.name, "", str(patch.n_faces), ""])
            item.setFlags(
                item.flags()
                | QtCore.Qt.ItemFlag.ItemIsUserCheckable
                | QtCore.Qt.ItemFlag.ItemIsEditable
                | QtCore.Qt.ItemFlag.ItemIsSelectable
            )
            item.setCheckState(
                0, QtCore.Qt.CheckState.Unchecked if patch.is_empty else QtCore.Qt.CheckState.Checked
            )
            item.setData(1, QtCore.Qt.ItemDataRole.UserRole, patch.name)
            item.setToolTip(1, "双击可重命名")
            color = (colors or {}).get(patch.name, (0.5, 0.5, 0.5))
            btn = ColorButton(color)
            btn.colorChanged.connect(
                lambda rgb, name=patch.name: self.patchColorChanged.emit(name, tuple(rgb))
            )
            combo = QtWidgets.QComboBox()
            combo.addItems(PATCH_MESH_TYPES)
            if patch.type not in PATCH_MESH_TYPES:
                combo.addItem(patch.type)
            combo.setCurrentText(patch.type)
            combo.setToolTip(
                "补片的**网格类型**(写在 constant/polyMesh/boundary 里)。\n"
                "边界条件类型必须与之匹配: 例如 empty/wedge/symmetry 只能用在\n"
                "同样类型的补片上; 从 ANSA 等工具导入的网格常常全是 wall。"
            )
            combo.currentTextChanged.connect(
                lambda text, name=patch.name: self.patchTypeChangeRequested.emit(name, text)
            )
            self.tree.addTopLevelItem(item)
            self.tree.setItemWidget(item, 2, combo)
            self.tree.setItemWidget(item, 4, self._wrap(btn))
            self._items[patch.name] = item
        self._loading = False

    @staticmethod
    def _wrap(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        holder = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(holder)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.addWidget(widget)
        lay.addStretch(1)
        return holder

    # ------------------------------------------------------------------
    def select(self, name: str, emit: bool = True) -> None:
        item = self._items.get(name)
        if item is None:
            return
        self._loading = not emit
        self.tree.setCurrentItem(item)
        item.setSelected(True)
        self.tree.scrollToItem(item)
        self._loading = False

    def selected_patch(self) -> str | None:
        item = self.tree.currentItem()
        if item is None:
            return None
        return item.data(1, QtCore.Qt.ItemDataRole.UserRole)

    def set_patch_checked(self, name: str, checked: bool) -> None:
        item = self._items.get(name)
        if item is None:
            return
        self._loading = True
        item.setCheckState(0, QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
        self._loading = False

    def mark_modified(self) -> None:
        """补片改过名后, 表格里的名字/统计需要重刷。"""
        self.set_mesh(self.mesh)

    # ------------------------------------------------------------------
    def _on_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if self._loading:
            return
        name = item.data(1, QtCore.Qt.ItemDataRole.UserRole)
        if column == 0:
            self.patchVisibilityChanged.emit(
                name, item.checkState(0) == QtCore.Qt.CheckState.Checked
            )
        elif column == 1:
            new = item.text(1).strip()
            if new and new != name:
                self.patchRenameRequested.emit(name, new)

    def _on_selection_changed(self) -> None:
        if self._loading:
            return
        name = self.selected_patch()
        if name:
            self.patchSelected.emit(name)

    def _set_all(self, visible: bool) -> None:
        self._loading = True
        for name, item in self._items.items():
            item.setCheckState(
                0, QtCore.Qt.CheckState.Checked if visible else QtCore.Qt.CheckState.Unchecked
            )
        self._loading = False
        for name in self._items:
            self.patchVisibilityChanged.emit(name, visible)

    def _rename_dialog(self) -> None:
        old = self.selected_patch()
        if not old:
            QtWidgets.QMessageBox.information(self, "提示", "请先在列表或三维窗口里选中一个补片。")
            return
        new, ok = QtWidgets.QInputDialog.getText(self, "重命名补片", "新的补片名:", text=old)
        if ok and new.strip() and new.strip() != old:
            self.patchRenameRequested.emit(old, new.strip())
