"""GUI 通用小部件与辅助函数。"""

from __future__ import annotations

from typing import Callable

from PyQt6 import QtCore, QtGui, QtWidgets

from ..foam import dictfile
from ..foam.dictfile import Dimensioned, FoamDict

__all__ = [
    "ValueEdit",
    "ColorButton",
    "add_atom_row",
    "add_dimensioned_row",
    "add_separator",
    "form_group",
    "mono_font",
]


# ---------------------------------------------------------------------------
# 值编辑器: uniform / 宏 / 原样
# ---------------------------------------------------------------------------
class ValueEdit(QtWidgets.QWidget):
    """编辑 OpenFOAM 里的一个"值"。

    支持三种形式:

    * ``uniform``  : 直接给数(标量 1 个输入框, 矢量 3 个)
    * ``macro``    : 引用宏, 例如 ``$internalField``
    * ``raw``      : 原样文本(可写任意 OpenFOAM 表达式)

    对外接口是 :meth:`set_state` / :meth:`state`,
    形式字符串与 :func:`foamgui.foam.fields.get_param` 的返回值一致。
    """

    changed = QtCore.pyqtSignal()

    def __init__(self, kind: str = "scalar", parent=None):
        super().__init__(parent)
        self.kind = kind
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        self.form_combo = QtWidgets.QComboBox()
        self.form_combo.addItems(["uniform", "$ 宏", "原样"])
        self.form_combo.setToolTip(
            "uniform: 写成 uniform <值>\n"
            "$ 宏  : 引用宏, 例如 $internalField(跟随初始条件)\n"
            "原样  : 直接写 OpenFOAM 表达式"
        )
        layout.addWidget(self.form_combo)

        # 固定创建 3 个输入框, 标量时隐藏后两个(这样可以随时切换场)
        self.paren_l = QtWidgets.QLabel("(")
        self.paren_r = QtWidgets.QLabel(")")
        layout.addWidget(self.paren_l)
        self.edits: list[QtWidgets.QLineEdit] = []
        for _i in range(3):
            e = QtWidgets.QLineEdit()
            e.setMinimumWidth(56)
            e.textChanged.connect(self._on_changed)
            layout.addWidget(e)
            self.edits.append(e)
        layout.addWidget(self.paren_r)
        layout.addStretch(1)

        self.form_combo.currentIndexChanged.connect(self._on_form_changed)
        self._macro_text = "$internalField"
        self._raw_text = ""
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        """切换标量/矢量(标量时只显示第一个输入框)。"""
        self.kind = "vector" if kind == "vector" else "scalar"
        vector = self.kind == "vector"
        self.paren_l.setVisible(vector)
        self.paren_r.setVisible(vector)
        self.edits[1].setVisible(vector)
        self.edits[2].setVisible(vector)
        self._on_form_changed(self.form_combo.currentIndex())

    # -- 内部 ---------------------------------------------------------------
    def _on_changed(self) -> None:
        if self.form_combo.currentIndex() == 2:
            self._raw_text = self.edits[0].text()
        self.changed.emit()

    def _active_edits(self) -> list[QtWidgets.QLineEdit]:
        return self.edits if self.kind == "vector" else self.edits[:1]

    def _on_form_changed(self, index: int) -> None:
        active = self._active_edits()
        vector = self.kind == "vector"
        if index == 1:  # $ 宏
            if not self.edits[0].text().startswith("$"):
                self.edits[0].setText(self._macro_text)
            for i, e in enumerate(self.edits):
                e.setEnabled(i == 0)
            show_extra = False
        elif index == 2:  # 原样文本
            for i, e in enumerate(self.edits):
                e.setEnabled(i == 0)
            show_extra = False
        else:  # uniform
            for e in active:
                e.setEnabled(True)
            show_extra = vector
        self.paren_l.setText("(" if show_extra else "")
        self.paren_r.setText(")" if show_extra else "")
        for e in self.edits[1:]:
            e.setVisible(show_extra)
        self.changed.emit()

    # -- 对外 ---------------------------------------------------------------
    def set_state(self, form: str, tokens: list[str]) -> None:
        """设置当前状态。``form`` 见 :func:`foamgui.foam.fields.get_param`。"""
        tokens = list(tokens or [])
        blocked = self.blockSignals(True)
        if form == "uniform":
            self.form_combo.setCurrentIndex(0)
            for i, e in enumerate(self.edits):
                e.setEnabled(True)
                e.setText(tokens[i] if i < len(tokens) else (tokens[0] if tokens else "0"))
        elif form == "macro":
            self.form_combo.setCurrentIndex(1)
            text = tokens[0] if tokens else "$internalField"
            self.edits[0].setEnabled(True)
            self.edits[0].setText(text)
            for e in self.edits[1:]:
                e.setEnabled(False)
        else:
            self.form_combo.setCurrentIndex(2)
            self.edits[0].setEnabled(True)
            self.edits[0].setText(" ".join(tokens))
            for e in self.edits[1:]:
                e.setEnabled(False)
        self.blockSignals(blocked)

    def state(self) -> tuple[str, list[str]]:
        idx = self.form_combo.currentIndex()
        if idx == 0:
            return ("uniform", [e.text().strip() or "0" for e in self._active_edits()])
        if idx == 1:
            return ("macro", [self.edits[0].text().strip() or "$internalField"])
        return ("raw", [self.edits[0].text().strip()])

    def is_uniform(self) -> bool:
        return self.form_combo.currentIndex() == 0


# ---------------------------------------------------------------------------
# 颜色按钮
# ---------------------------------------------------------------------------
class ColorButton(QtWidgets.QPushButton):
    """点一下弹调色板的小按钮。"""

    colorChanged = QtCore.pyqtSignal(tuple)

    def __init__(self, color=(0.5, 0.5, 0.5), parent=None):
        super().__init__(parent)
        self._color = tuple(color)
        self.setFixedSize(28, 18)
        self.clicked.connect(self._pick)
        self._refresh()

    def _refresh(self) -> None:
        r, g, b = (int(max(0.0, min(1.0, c)) * 255) for c in self._color)
        self.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); border: 1px solid #555;"
        )

    def color(self) -> tuple[float, float, float]:
        return self._color

    def set_color(self, color) -> None:
        self._color = tuple(color)
        self._refresh()

    def _pick(self) -> None:
        r, g, b = (int(max(0.0, min(1.0, c)) * 255) for c in self._color)
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(r, g, b), self, "选择补片颜色")
        if c.isValid():
            self.set_color((c.red() / 255.0, c.green() / 255.0, c.blue() / 255.0))
            self.colorChanged.emit(self._color)


# ---------------------------------------------------------------------------
# 表单辅助
# ---------------------------------------------------------------------------
def add_atom_row(
    form: QtWidgets.QFormLayout,
    d: FoamDict,
    key: str,
    label: str,
    kind: str = "text",
    choices: list[str] | None = None,
    default: str = "",
    tooltip: str = "",
    on_change: Callable[[], None] | None = None,
) -> QtWidgets.QWidget:
    """往表单里加一行, 编辑字典 ``d[key]`` 的原子值。返回创建的控件。"""
    # 注意: 字典里没有这个条目时, 只在界面上显示默认值, **不写回字典**。
    # 只有用户真的改了控件, 才会写进去 —— 这样"没碰过的条目"保持原样,
    # 不会因为打开一次面板就悄悄给案例加一堆默认条目。
    value = dictfile.get_atom(d, key, None)
    if value is None:
        value = default

    if kind == "bool":
        w = QtWidgets.QCheckBox()
        w.setChecked(str(value).lower() in ("1", "true", "yes", "on"))
        w.toggled.connect(lambda v: (d.set(key, "true" if v else "false"), _call(on_change)))
    elif kind == "choice":
        w = QtWidgets.QComboBox()
        w.setEditable(True)
        if choices:
            w.addItems(choices)
        w.setCurrentText(str(value))
        w.currentTextChanged.connect(lambda t: (d.set(key, t), _call(on_change)))
    else:
        w = QtWidgets.QLineEdit(str(value))
        w.textChanged.connect(lambda t: (d.set(key, t), _call(on_change)))
    if tooltip:
        w.setToolTip(tooltip)
    lab = QtWidgets.QLabel(label)
    if tooltip:
        lab.setToolTip(tooltip)
    form.addRow(lab, w)
    return w


def add_dimensioned_row(
    form: QtWidgets.QFormLayout,
    d: FoamDict,
    key: str,
    label: str,
    default_dims: list[str],
    default_value: str = "0",
    tooltip: str = "",
    on_change: Callable[[], None] | None = None,
) -> QtWidgets.QWidget:
    """往表单里加一行, 编辑形如 ``[0 2 -1 0 0 0 0] 1e-05`` 的带量纲值。"""
    v = d.get(key)
    dims = default_dims
    text = default_value
    if isinstance(v, Dimensioned):
        dims = v.dims
        text = dictfile.atom(v.value) or " ".join(dictfile.atoms(v.value)) or default_value
    elif v is not None:
        text = dictfile.atom(v) or str(v)

    box = QtWidgets.QWidget()
    lay = QtWidgets.QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    dim_text = "[" + " ".join(dims) + "]"
    dim_label = QtWidgets.QLabel(dim_text)
    dim_label.setStyleSheet("color: #888;")
    dim_label.setMinimumWidth(dim_label.fontMetrics().horizontalAdvance(dim_text) + 10)
    lay.addWidget(dim_label)
    edit = QtWidgets.QLineEdit(str(text))
    edit.setToolTip("量纲固定, 这里只改数值" if not tooltip else tooltip)
    edit.textChanged.connect(
        lambda t: (d.set(key, Dimensioned(dims, t.strip() or "0")), _call(on_change))
    )
    lay.addWidget(edit, 1)
    form.addRow(QtWidgets.QLabel(label), box)
    return edit


def _call(fn: Callable[[], None] | None) -> None:
    if fn is not None:
        fn()


def add_separator(form: QtWidgets.QFormLayout, text: str = "") -> None:
    if text:
        lab = QtWidgets.QLabel(text)
        f = lab.font()
        f.setBold(True)
        lab.setFont(f)
        form.addRow(lab)
    line = QtWidgets.QFrame()
    line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
    line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
    form.addRow(line)


def form_group(title: str) -> tuple[QtWidgets.QGroupBox, QtWidgets.QFormLayout]:
    box = QtWidgets.QGroupBox(title)
    form = QtWidgets.QFormLayout(box)
    form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
    form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return box, form


def mono_font() -> QtGui.QFont:
    f = QtGui.QFont("Monospace")
    f.setStyleHint(QtGui.QFont.StyleHint.TypeWriter)
    return f
