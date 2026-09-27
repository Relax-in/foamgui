"""初始条件(0/ 目录)与边界条件设置页。"""

from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from ..foam import fields as fields_mod
from ..foam.dictfile import FoamDict
from .widgets import ValueEdit

__all__ = ["InitialConditionsTab", "BoundaryConditionsTab"]


# ---------------------------------------------------------------------------
# 初始条件
# ---------------------------------------------------------------------------
class InitialConditionsTab(QtWidgets.QWidget):
    """设置 ``0/<场>`` 里的 internalField(以及查看 dimensions/类型)。"""

    changed = QtCore.pyqtSignal()
    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case = None
        self._rows: list[str] = []
        self._loading = False

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        tip = QtWidgets.QLabel(
            "这里设置每个场的内部初始值(internalField)。"
            "若某个场在文件里是 nonuniform 列表(逐单元给定), 这里只做显示, 不会被覆盖。"
        )
        tip.setWordWrap(True)
        lay.addWidget(tip)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["场", "类型", "量纲 dimensions", "internalField"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 180)
        self.table.itemSelectionChanged.connect(self._on_row_changed)
        lay.addWidget(self.table, 1)

        box = QtWidgets.QGroupBox("编辑选中的场")
        f = QtWidgets.QFormLayout(box)
        self.lbl_field = QtWidgets.QLabel("-")
        self.edit_value = ValueEdit("scalar")
        self.edit_value.setEnabled(False)
        self.edit_value.changed.connect(self._on_value_changed)
        self.lbl_note = QtWidgets.QLabel("")
        self.lbl_note.setWordWrap(True)
        f.addRow("场", self.lbl_field)
        f.addRow("内部值", self.edit_value)
        f.addRow(self.lbl_note)
        lay.addWidget(box)

        row = QtWidgets.QHBoxLayout()
        self.cmb_new_field = QtWidgets.QComboBox()
        self.cmb_new_field.setEditable(True)
        self.cmb_new_field.addItems(sorted(fields_mod.FIELD_CATALOG.keys()))
        btn_add = QtWidgets.QPushButton("添加场")
        btn_del = QtWidgets.QPushButton("删除选中的场")
        btn_add.clicked.connect(self._add_field)
        btn_del.clicked.connect(self._remove_field)
        row.addWidget(QtWidgets.QLabel("新增/补充场:"))
        row.addWidget(self.cmb_new_field, 1)
        row.addWidget(btn_add)
        row.addWidget(btn_del)
        lay.addLayout(row)

    # ------------------------------------------------------------------
    def set_case(self, case) -> None:
        self.case = case
        self.refresh()

    def refresh(self) -> None:
        self._loading = True
        self.table.setRowCount(0)
        self._rows = []
        if self.case is None:
            self._loading = False
            return
        for name, ff in self.case.fields.items():
            r = self.table.rowCount()
            self.table.insertRow(r)
            self._rows.append(name)
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(name))
            self.table.setItem(r, 1, QtWidgets.QTableWidgetItem(ff.kind))
            self.table.setItem(r, 2, QtWidgets.QTableWidgetItem("[" + " ".join(ff.dimensions) + "]"))
            self.table.setItem(r, 3, QtWidgets.QTableWidgetItem(_internal_text(ff)))
        self._loading = False
        if self._rows:
            self.table.selectRow(0)
        else:
            self._on_row_changed()

    def _current_field(self):
        r = self.table.currentRow()
        if self.case is None or r < 0 or r >= len(self._rows):
            return None
        return self.case.fields.get(self._rows[r])

    def _on_row_changed(self) -> None:
        ff = self._current_field()
        if ff is None:
            self.lbl_field.setText("-")
            self.edit_value.setEnabled(False)
            self.lbl_note.setText("")
            return
        self.lbl_field.setText(f"{ff.name}   ({ff.cls})")
        form, tokens = ff.internal
        # 注意: 必须先挂起回写, 再改控件, 否则会把上一个场的值写进当前场
        self._loading = True
        self.edit_value.set_kind(ff.kind)
        self.edit_value.setEnabled(True)
        self.edit_value.set_state(form, tokens)
        self._loading = False
        if form in ("uniform", "macro"):
            self.lbl_note.setText("")
        else:
            self.lbl_note.setText(
                "该场当前是 nonuniform / 表达式形式, 保持原样; 一旦在上面改动就会被覆盖。"
            )

    def _on_value_changed(self) -> None:
        if self._loading:
            return
        ff = self._current_field()
        if ff is None:
            return
        form, tokens = self.edit_value.state()
        if form == "uniform":
            fields_mod.set_internal_field(ff.body, tokens, uniform=True)
        elif form == "macro":
            ff.body.set("internalField", tokens[0])
        else:
            ff.body.set("internalField", " ".join(tokens))
        r = self.table.currentRow()
        item = self.table.item(r, 3)
        if item is not None:
            item.setText(_internal_text(ff))
        self.changed.emit()

    # ------------------------------------------------------------------
    def _add_field(self) -> None:
        if self.case is None:
            return
        name = self.cmb_new_field.currentText().strip()
        if not name:
            return
        if name in self.case.fields:
            QtWidgets.QMessageBox.information(self, "提示", f"场 {name} 已经存在。")
            return
        self.case.add_field(name)
        self.case.sync_patches()
        self.refresh()
        self.statusMessage.emit(f"已添加场 {name}")
        self.changed.emit()

    def _remove_field(self) -> None:
        if self.case is None:
            return
        ff = self._current_field()
        if ff is None:
            return
        ok = QtWidgets.QMessageBox.question(
            self, "确认", f"从本次写出中移除场 {ff.name} ?\n(磁盘上的原文件不会被删除)"
        )
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.case.remove_field(ff.name)
        self.refresh()
        self.statusMessage.emit(f"已移除场 {ff.name}")
        self.changed.emit()


def _internal_text(ff) -> str:
    form, tokens = ff.internal
    if form == "uniform":
        return "uniform " + ("(" + " ".join(tokens) + ")" if len(tokens) > 1 else " ".join(tokens))
    return " ".join(tokens)


# ---------------------------------------------------------------------------
# 边界条件
# ---------------------------------------------------------------------------
class BoundaryConditionsTab(QtWidgets.QWidget):
    """按"场 + 补片"设置边界条件。"""

    changed = QtCore.pyqtSignal()
    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case = None
        self.field = None
        self._loading = False
        self._param_widgets: list[tuple[str, QtWidgets.QWidget]] = []

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        top.addWidget(QtWidgets.QLabel("场:"))
        self.cmb_field = QtWidgets.QComboBox()
        self.cmb_field.setMinimumWidth(160)
        self.cmb_field.currentTextChanged.connect(lambda _t: self._rebuild_table())
        top.addWidget(self.cmb_field)
        self.lbl_field_info = QtWidgets.QLabel("")
        top.addWidget(self.lbl_field_info, 1)
        btn_rec = QtWidgets.QPushButton("按补片名推荐边界条件")
        btn_rec.setToolTip(
            "根据补片名/类型自动推荐(例如 inlet/outlet 用来流条件, walls 用壁面函数), "
            "会覆盖当前场的全部边界条件"
        )
        btn_rec.clicked.connect(self._recommend)
        btn_sync = QtWidgets.QPushButton("同步网格补片")
        btn_sync.setToolTip("把网格里新出现、而场文件里没有的补片补上(用推荐条件)")
        btn_sync.clicked.connect(self._sync)
        top.addWidget(btn_rec)
        top.addWidget(btn_sync)
        lay.addLayout(top)

        self.table = QtWidgets.QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["补片", "边界条件类型", "参数", "补片类型"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 130)
        self.table.setColumnWidth(1, 200)
        self.table.setColumnWidth(2, 300)
        self.table.itemSelectionChanged.connect(lambda: self._rebuild_params())
        lay.addWidget(self.table, 3)

        self.param_box = QtWidgets.QGroupBox("边界条件参数")
        outer = QtWidgets.QVBoxLayout(self.param_box)
        self.param_scroll = QtWidgets.QScrollArea()
        self.param_scroll.setWidgetResizable(True)
        self.param_host = QtWidgets.QWidget()
        self.param_form = QtWidgets.QFormLayout(self.param_host)
        self.param_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        self.param_scroll.setWidget(self.param_host)
        outer.addWidget(self.param_scroll)
        lay.addWidget(self.param_box, 2)

    # ------------------------------------------------------------------
    def set_case(self, case) -> None:
        self.case = case
        self.cmb_field.blockSignals(True)
        self.cmb_field.clear()
        if case is not None:
            self.cmb_field.addItems(list(case.fields.keys()))
        self.cmb_field.blockSignals(False)
        self._rebuild_table()

    def _current_field(self):
        if self.case is None:
            return None
        return self.case.fields.get(self.cmb_field.currentText())

    def _rebuild_table(self) -> None:
        self._loading = True
        self.table.setRowCount(0)
        self.field = self._current_field()
        if self.field is None or self.case is None:
            self.lbl_field_info.setText("")
            self._loading = False
            self._rebuild_params()
            return
        kind = "矢量" if self.field.kind == "vector" else "标量"
        self.lbl_field_info.setText(
            f"{self.field.cls}   类别: {self.field.category}   {kind}   dimensions [{' '.join(self.field.dimensions)}]"
        )
        patches = self.case.patch_info()
        for pname, ptype, _n in patches:
            d = self.field.patch_dict(pname)
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.table.setItem(r, 0, QtWidgets.QTableWidgetItem(pname))
            self.table.setItem(r, 3, QtWidgets.QTableWidgetItem(ptype))
            combo = QtWidgets.QComboBox()
            names = [t.name for t in fields_mod.bc_types_for(self.field.category, ptype)]
            cur = self.field.patch_type(pname)
            combo.addItems(names)
            if cur and cur not in names:
                combo.addItem(cur)
            combo.setCurrentText(cur or (names[0] if names else ""))
            combo.currentTextChanged.connect(
                lambda text, p=pname: self._on_type_changed(p, text)
            )
            self.table.setCellWidget(r, 1, combo)
            self.table.setItem(r, 2, QtWidgets.QTableWidgetItem(fields_mod.param_summary(d)))
        self._loading = False
        if self.table.rowCount():
            self.table.selectRow(0)
        else:
            self._rebuild_params()

    # ------------------------------------------------------------------
    def _on_type_changed(self, patch: str, bctype: str) -> None:
        if self._loading or self.field is None or not bctype:
            return
        d = self.field.patch_dict(patch)
        d.set("type", bctype)
        spec = fields_mod.find_bc_type(self.field.category, bctype)
        # 换类型时清掉旧参数, 用新类型的默认值
        for key in list(d.keys()):
            if key != "type":
                del d[key]
        if spec is not None:
            for p in spec.params:
                if p.optional:
                    continue  # 可选参数留给用户自己添加
                kind = _resolve_kind(p.kind, self.field.kind)
                default = p.default
                if isinstance(default, str) and default.startswith("$"):
                    fields_mod.set_param_macro(d, p.key, default)
                elif isinstance(default, (list, tuple)):
                    fields_mod.set_param_vector(d, p.key, [str(x) for x in default])
                elif kind == "vector":
                    fields_mod.set_param_vector(d, p.key, ["0", "0", "0"])
                else:
                    fields_mod.set_param_scalar(d, p.key, "0" if default is None else str(default))
        self._update_row_summary(patch)
        self._rebuild_params()
        self.changed.emit()

    def _update_row_summary(self, patch: str) -> None:
        if self.field is None:
            return
        d = self.field.patch_dict(patch)
        for r in range(self.table.rowCount()):
            if self.table.item(r, 0) and self.table.item(r, 0).text() == patch:
                item = self.table.item(r, 2)
                if item is not None:
                    item.setText(fields_mod.param_summary(d))
                break

    # ------------------------------------------------------------------
    def _rebuild_params(self) -> None:
        while self.param_form.count():
            item = self.param_form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._param_widgets = []
        if self.field is None:
            return
        r = self.table.currentRow()
        if r < 0:
            return
        patch = self.table.item(r, 0).text()
        d = self.field.patch_dict(patch)
        bctype = self.field.patch_type(patch)
        spec = fields_mod.find_bc_type(self.field.category, bctype)

        head = QtWidgets.QLabel(f"补片 <b>{patch}</b> — 类型 <b>{bctype or '(未设置)'}</b>")
        self.param_form.addRow(head)

        known: set[str] = set()
        for p in (spec.params if spec else []):
            kind = _resolve_kind(p.kind, self.field.kind)
            label = p.label + ("" if not p.optional else " (可选)")
            w = self._make_param_widget(d, p, kind, label)
            known.add(p.key)

        # 目录里没有的参数(用户自定义/更高级的类型)也显示出来, 保证不被丢掉
        extra = [k for k in d.keys() if k not in known and k != "type"]
        if extra:
            sep = QtWidgets.QLabel("其他参数(原样保留)")
            f = sep.font()
            f.setBold(True)
            sep.setFont(f)
            self.param_form.addRow(sep)
            for key in extra:
                self._make_generic_widget(d, key, self.field.kind)

    def _make_param_widget(self, d: FoamDict, p, kind: str, label: str):
        if kind == "word":
            combo = QtWidgets.QComboBox()
            combo.setEditable(True)
            if p.choices:
                combo.addItems(p.choices)
            cur = fields_mod.get_param(d, p.key)
            combo.setCurrentText(cur[1][0] if cur[1] else "")
            combo.currentTextChanged.connect(
                lambda t, key=p.key: self._write_word(key, t)
            )
            self.param_form.addRow(label, combo)
            return combo
        if kind == "bool":
            cb = QtWidgets.QCheckBox()
            cur = fields_mod.get_param(d, p.key)
            cb.setChecked(bool(cur[1]) and cur[1][0] in ("true", "1", "yes", "on"))
            cb.toggled.connect(lambda v, key=p.key: self._write_bool(key, v))
            self.param_form.addRow(label, cb)
            return cb
        edit = ValueEdit(kind)
        form, tokens = fields_mod.get_param(d, p.key)
        if form == "missing":
            if isinstance(p.default, (list, tuple)):
                form, tokens = "uniform", [str(x) for x in p.default]
            elif isinstance(p.default, str) and p.default.startswith("$"):
                form, tokens = "macro", [p.default]
            elif p.default is not None:
                form, tokens = "uniform", [str(p.default)]
            else:
                form, tokens = "uniform", (["0", "0", "0"] if kind == "vector" else ["0"])
        edit.set_state(form, tokens)
        edit.changed.connect(lambda key=p.key, e=edit: self._write_value(key, e))
        bot = QtWidgets.QWidget()
        bl = QtWidgets.QHBoxLayout(bot)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(edit, 1)
        if p.help:
            info = QtWidgets.QLabel("?")
            info.setToolTip(p.help)
            info.setFixedWidth(14)
            bl.addWidget(info)
        self.param_form.addRow(label, bot)
        return edit

    def _make_generic_widget(self, d: FoamDict, key: str, field_kind: str) -> None:
        form, tokens = fields_mod.get_param(d, key)
        kind = "vector" if len(tokens) > 1 else "scalar"
        edit = ValueEdit(kind)
        edit.set_state(form, tokens)
        edit.changed.connect(lambda k=key, e=edit: self._write_value(k, e))
        self.param_form.addRow(key, edit)

    # -- 写回 -----------------------------------------------------------
    def _patch_dict(self) -> FoamDict | None:
        if self.field is None:
            return None
        r = self.table.currentRow()
        if r < 0:
            return None
        return self.field.patch_dict(self.table.item(r, 0).text())

    def _write_value(self, key: str, edit: ValueEdit) -> None:
        if self._loading:
            return
        d = self._patch_dict()
        if d is None:
            return
        form, tokens = edit.state()
        if form == "uniform":
            if edit.kind == "vector":
                fields_mod.set_param_vector(d, key, tokens)
            else:
                fields_mod.set_param_scalar(d, key, tokens[0])
        elif form == "macro":
            fields_mod.set_param_macro(d, key, tokens[0])
        else:
            d.set(key, tokens[0])
        self._refresh_summary_and_changed()

    def _write_word(self, key: str, text: str) -> None:
        if self._loading:
            return
        d = self._patch_dict()
        if d is None:
            return
        fields_mod.set_param_word(d, key, text)
        self._refresh_summary_and_changed()

    def _write_bool(self, key: str, value: bool) -> None:
        if self._loading:
            return
        d = self._patch_dict()
        if d is None:
            return
        fields_mod.set_param_bool(d, key, value)
        self._refresh_summary_and_changed()

    def _refresh_summary_and_changed(self) -> None:
        r = self.table.currentRow()
        if r >= 0 and self.table.item(r, 0):
            self._update_row_summary(self.table.item(r, 0).text())
        self.changed.emit()

    # -- 批量操作 -------------------------------------------------------
    def _recommend(self) -> None:
        if self.case is None or self.field is None:
            return
        ok = QtWidgets.QMessageBox.question(
            self,
            "确认",
            f"用推荐值覆盖场 {self.field.name} 的全部边界条件?",
        )
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        from ..foam.fields import apply_params

        for pname, ptype, _n in self.case.patch_info():
            bc, params = fields_mod.recommend_bc(self.field.category, self.field.name, pname, ptype)
            d = self.field.patch_dict(pname)
            for key in list(d.keys()):
                del d[key]
            d.set("type", bc)
            apply_params(d, self.field.kind, params)
        self._rebuild_table()
        self.changed.emit()
        self.statusMessage.emit(f"场 {self.field.name}: 已套用推荐边界条件")

    def _sync(self) -> None:
        if self.case is None:
            return
        added = self.case.sync_patches()
        self.set_case(self.case)
        self.changed.emit()
        self.statusMessage.emit(f"同步完成, 新增 {len(added)} 个补片条目" if added else "没有需要同步的补片")


def _resolve_kind(kind: str, field_kind: str) -> str:
    if kind == "auto":
        return "vector" if field_kind == "vector" else "scalar"
    return kind
