"""求解设置页: controlDict / fvSchemes / fvSolution / momentumTransport / physicalProperties。

所有控件都直接读写案例内存里的 :class:`~foamgui.foam.dictfile.FoamDict`,
因此文件里 GUI 没有覆盖到的条目都会被原样保留。
"""

from __future__ import annotations

from PyQt6 import QtCore, QtWidgets

from ..foam import dictfile
from ..foam import fields as fields_mod
from ..foam.dictfile import FoamDict
from .widgets import add_atom_row, add_dimensioned_row, commit_form

__all__ = ["SolverTab"]


SOLVERS_INCOMPRESSIBLE = [
    "incompressibleFluid",
    "incompressibleVoF",
    "incompressibleMultiphaseVoF",
    "shallowWaterFoam",
    "potentialFoam",
]
SOLVERS_COMPRESSIBLE = [
    "fluid",
    "multicomponentFluid",
    "compressibleVoF",
    "isothermalFluid",
    "buoyantFluid",
]
DDT_SCHEMES = ["steadyState", "Euler", "backward", "CrankNicolson 0.9"]
GRAD_SCHEMES = ["Gauss linear", "cellLimited Gauss linear 1", "leastSquares", "Gauss cubic"]
LAPLACIAN_SCHEMES = [
    "Gauss linear corrected",
    "Gauss linear orthogonal",
    "Gauss linear limited 0.5",
    "Gauss linear limited corrected 0.33",
]
INTERPOLATION_SCHEMES = ["linear", "linearUpwind grad(U)", "cubic", "vanLeer", "limitedLinear 1"]
SNGRAD_SCHEMES = ["corrected", "uncorrected", "bounded corrected", "limited corrected 0.5"]
RAS_MODELS = fields_mod.RAS_MODELS
LES_MODELS = fields_mod.LES_MODELS


class SolverTab(QtWidgets.QWidget):
    """求解参数总览页。"""

    changed = QtCore.pyqtSignal()
    statusMessage = QtCore.pyqtSignal(str)
    fieldsChanged = QtCore.pyqtSignal()      # 新增了 0/ 里的场, 需要刷新边界/初始条件页

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case = None
        self._loading = False

        host = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(host)
        v.setContentsMargins(10, 10, 10, 10)
        v.setSpacing(10)
        bar = QtWidgets.QHBoxLayout()
        self.btn_commit = QtWidgets.QPushButton("把当前显示的值写入字典")
        self.btn_commit.setToolTip(
            "平时只有你改动过的条目才会写进字典(没碰过的保持原样)。\n"
            "点这里会把所有页签上显示的条目(包括默认值)都写进去,\n"
            "适合给缺少必需条目的案例(例如缺 pRefCell/pRefValue)一次性补齐。"
        )
        self.btn_commit.clicked.connect(self._commit_all)
        bar.addWidget(self.btn_commit)
        self.lbl_commit = QtWidgets.QLabel("")
        self.lbl_commit.setEnabled(False)
        bar.addWidget(self.lbl_commit, 1)
        v.addLayout(bar)
        v.addWidget(self._build_control_dict())
        v.addWidget(self._build_turbulence())
        v.addWidget(self._build_physical())
        v.addWidget(self._build_fv_schemes())
        v.addWidget(self._build_fv_solution())
        v.addStretch(1)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(host)
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll)

    # ------------------------------------------------------------------
    # 界面骨架
    # ------------------------------------------------------------------
    def _build_control_dict(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("时间与输出 (system/controlDict)")
        self.control_form = QtWidgets.QFormLayout(box)
        self.control_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        return box

    def _build_turbulence(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("湍流模型 (constant/momentumTransport)")
        self.turb_form = QtWidgets.QFormLayout(box)
        self.turb_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        return box

    def _build_physical(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("物性 (constant/physicalProperties)")
        self.phys_form = QtWidgets.QFormLayout(box)
        self.phys_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        return box

    def _build_fv_schemes(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("离散格式 (system/fvSchemes)")
        outer = QtWidgets.QVBoxLayout(box)
        self.scheme_form = QtWidgets.QFormLayout()
        self.scheme_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        outer.addLayout(self.scheme_form)
        outer.addWidget(QtWidgets.QLabel("divSchemes 明细"))
        self.div_table = QtWidgets.QTableWidget(0, 2)
        self.div_table.setHorizontalHeaderLabels(["div 项", "格式"])
        self.div_table.horizontalHeader().setStretchLastSection(True)
        self.div_table.verticalHeader().setVisible(False)
        self.div_table.setMinimumHeight(170)
        self.div_table.itemChanged.connect(lambda _i: self._rebuild_div_dict())
        outer.addWidget(self.div_table)
        row = QtWidgets.QHBoxLayout()
        b_add = QtWidgets.QPushButton("增加一行")
        b_del = QtWidgets.QPushButton("删除选中行")
        b_add.clicked.connect(self._add_div_row)
        b_del.clicked.connect(self._del_div_row)
        row.addWidget(b_add)
        row.addWidget(b_del)
        row.addStretch(1)
        outer.addLayout(row)
        return box

    def _build_fv_solution(self) -> QtWidgets.QGroupBox:
        box = QtWidgets.QGroupBox("线性求解器与松弛 (system/fvSolution)")
        outer = QtWidgets.QVBoxLayout(box)
        outer.addWidget(QtWidgets.QLabel("solvers(每个场一行)"))
        self.solver_table = QtWidgets.QTableWidget(0, 6)
        self.solver_table.setHorizontalHeaderLabels(
            ["场", "solver", "smoother", "nSweeps", "tolerance", "relTol"]
        )
        self.solver_table.horizontalHeader().setStretchLastSection(True)
        self.solver_table.verticalHeader().setVisible(False)
        self.solver_table.setMinimumHeight(180)
        self.solver_table.itemChanged.connect(lambda _i: self._rebuild_solvers())
        outer.addWidget(self.solver_table)
        self.solution_form = QtWidgets.QFormLayout()
        self.solution_form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        outer.addLayout(self.solution_form)
        return box

    # ------------------------------------------------------------------
    def set_case(self, case) -> None:
        self.case = case
        self.lbl_commit.setText("")
        self._loading = True
        try:
            self._fill_control_dict()
            self._fill_turbulence()
            self._fill_physical()
            self._fill_schemes()
            self._fill_solution()
        finally:
            self._loading = False

    @staticmethod
    def _clear_form(form: QtWidgets.QFormLayout) -> None:
        while form.count():
            item = form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    # -- controlDict ----------------------------------------------------
    def _fill_control_dict(self) -> None:
        f = self.control_form
        self._clear_form(f)
        d = self.case.control_dict() if self.case else FoamDict()
        self._cd = d
        cb = self.changed.emit
        # OpenFOAM 13 只认 `solver`(10 之前叫 `application`), 所以这里**永远写 solver**。
        # 但如果案例里只有老式的 `application`, 就把它的值显示出来当初始值, 并给出提示;
        # 用户改动后会新增/更新 `solver`, 原来的 `application` 原样保留(不静默删用户的东西)。
        legacy = None
        if "solver" not in d and "application" in d:
            legacy = dictfile.get_atom(d, "application", "") or ""
        solver_widget = add_atom_row(
            f, d, "solver", "求解器 solver", "choice",
            SOLVERS_INCOMPRESSIBLE + SOLVERS_COMPRESSIBLE,
            legacy or "incompressibleFluid",
            "OpenFOAM 13 用 foamRun + 模块名, 必须写成 solver incompressibleFluid;", cb)

        def _tidy_solver(*_a) -> None:
            """选了求解器之后: 清掉老式 application, 并把 solver 放到文件开头。"""
            if "application" in d:
                del d["application"]
                self.statusMessage.emit(
                    "已用 solver 取代老式的 application 条目(OpenFOAM 13 只用 solver)"
                )
            d.move_to_front("solver")

        if legacy:
            solver_widget.currentTextChanged.connect(_tidy_solver)
        if legacy:
            hint = QtWidgets.QLabel(
                f"⚠ 该案例用的是老式写法 <code>application {legacy};</code>,"
                "OpenFOAM 13 需要 <code>solver</code>;<br>在这里选择后会把 "
                "<code>solver</code> 写进 controlDict(原 application 条目保留不动)。"
            )
            hint.setWordWrap(True)
            hint.setEnabled(False)
            f.addRow(hint)
        add_atom_row(f, d, "startFrom", "startFrom", "choice",
                     ["startTime", "latestTime", "firstTime"], "startTime", "", cb)
        add_atom_row(f, d, "startTime", "startTime", "text", None, "0", "", cb)
        add_atom_row(f, d, "stopAt", "stopAt", "choice",
                     ["endTime", "writeNow", "noWriteNow", "nextWrite"], "endTime", "", cb)
        add_atom_row(f, d, "endTime", "endTime", "text", None, "500", "", cb)
        add_atom_row(f, d, "deltaT", "deltaT", "text", None, "1", "", cb)
        add_atom_row(f, d, "writeControl", "writeControl", "choice",
                     ["timeStep", "runTime", "adjustableRunTime", "clockTime", "cpuTime"],
                     "timeStep", "", cb)
        add_atom_row(f, d, "writeInterval", "writeInterval", "text", None, "50", "", cb)
        add_atom_row(f, d, "purgeWrite", "purgeWrite", "text", None, "0", "", cb)
        add_atom_row(f, d, "writeFormat", "writeFormat", "choice", ["ascii", "binary"], "ascii", "", cb)
        add_atom_row(f, d, "writePrecision", "writePrecision", "text", None, "6", "", cb)
        add_atom_row(f, d, "writeCompression", "writeCompression", "choice", ["off", "on"], "off", "", cb)
        add_atom_row(f, d, "runTimeModifiable", "runTimeModifiable", "bool", None, "true", "", cb)
        add_atom_row(f, d, "adjustTimeStep", "adjustTimeStep", "bool", None, "false",
                     "自适应时间步(通常配合 maxCo)", cb)
        add_atom_row(f, d, "maxCo", "maxCo", "text", None, "1", "最大库朗数", cb)

    # -- 湍流 -----------------------------------------------------------
    def _fill_turbulence(self) -> None:
        f = self.turb_form
        self._clear_form(f)
        d = self.case.constant.get("momentumTransport", (FoamDict(), ""))[0] if self.case else FoamDict()
        self._mt = d
        add_atom_row(f, d, "simulationType", "simulationType", "choice",
                     ["laminar", "RAS", "LES"], "RAS", "层流 / 雷诺平均 / 大涡模拟",
                     self._on_sim_type, editable=False)
        sim = dictfile.get_atom(d, "simulationType", "RAS")
        if sim in ("RAS", "LES"):
            # 一键补齐该模型需要的场(k/omega/epsilon/nuTilda/nut):
            # 少了任何一个, 求解器都会报 "cannot find field ..."
            sub_preview = dictfile.get_dict(d, sim)
            model_now = dictfile.get_atom(sub_preview, "model", "") if sub_preview else ""
            needed = fields_mod.TURBULENCE_FIELDS.get(model_now or "", [])
            missing = [n for n in needed if self.case is not None and n not in self.case.fields]
            bar = QtWidgets.QHBoxLayout()
            btn = QtWidgets.QPushButton("补齐该模型需要的场")
            btn.setToolTip(
                "在 0/ 里自动添加该湍流模型需要的场(用推荐边界条件),\n"
                "并在 fvSchemes/fvSolution 里补上对应的条目"
            )
            btn.setEnabled(bool(missing))
            btn.clicked.connect(self._add_missing_fields)
            bar.addWidget(btn)
            info = QtWidgets.QLabel(
                f"需要: {'、'.join(needed) or '(无)'}" + (f"; 缺少: {'、'.join(missing)}" if missing else " (已齐全)")
            )
            info.setEnabled(False)
            bar.addWidget(info, 1)
            f.addRow(bar)
        if sim in ("RAS", "LES"):
            sub = dictfile.get_dict(d, sim)
            if sub is None:
                sub = FoamDict()
                d.set(sim, sub)
            # 模型只能从列表里选: 否则容易写出 RAS { model laminar; } 这种非法组合
            add_atom_row(f, sub, "model", f"{sim} 模型", "choice",
                         RAS_MODELS if sim == "RAS" else LES_MODELS,
                         "SpalartAllmaras" if sim == "RAS" else "Smagorinsky",
                         "只能从列表里选; 要算层流请把上面的 simulationType 改成 laminar",
                         self._on_model_changed, editable=False)
            add_atom_row(f, sub, "turbulence", "turbulence", "choice", ["on", "off"], "on", "", self.changed.emit)
            cur_model = dictfile.get_atom(sub, "model", "") or ""
            valid = RAS_MODELS if sim == "RAS" else LES_MODELS
            if cur_model and cur_model not in valid:
                QtCore.QTimer.singleShot(
                    0,
                    lambda m=cur_model, s=sim: self.statusMessage.emit(
                        f"⚠ constant/momentumTransport 里是 {s} {{ model {m}; }}, "
                        f"但 {m} 不在 {s} 模型列表里(求解器会报错); "
                        "要层流请把 simulationType 改成 laminar, 否则请重新选一个模型"
                    ),
                )
            add_atom_row(f, sub, "printCoeffs", "printCoeffs", "bool", None, "true", "", self.changed.emit)
        else:
            hint = QtWidgets.QLabel("层流: 只需要 0/U 与 0/p, 湍流场可以不要")
            hint.setEnabled(False)
            f.addRow(hint)

    def _add_missing_fields(self) -> None:
        """按当前湍流模型补齐 0/ 里缺少的场, 并补上字典条目。"""
        if self.case is None:
            return
        sim = dictfile.get_atom(self._mt, "simulationType", "laminar")
        sub = dictfile.get_dict(self._mt, sim)
        model = dictfile.get_atom(sub, "model", "") if sub else ""
        needed = fields_mod.TURBULENCE_FIELDS.get(model or "", [])
        missing = [n for n in needed if n not in self.case.fields]
        if not missing:
            self.statusMessage.emit("该模型需要的场已经齐全")
            return
        for name in missing:
            self.case.add_field(name)
        self.case.sync_patches()
        added = self.case.ensure_schemes_for_fields(missing)
        self._fill_schemes()
        self._fill_solution()
        self.changed.emit()
        self.fieldsChanged.emit()
        msg = f"已添加场 {'、'.join(missing)}"
        if added:
            msg += "; 同时补上 " + "、".join(added)
        self.statusMessage.emit(msg)

    def _commit_all(self) -> None:
        """把各表单当前显示的值全部写进字典(显式补齐默认条目)。"""
        n = 0
        for form in (self.control_form, self.turb_form, self.phys_form,
                     self.scheme_form, self.solution_form):
            n += commit_form(form)
        self.changed.emit()
        self.lbl_commit.setText(f"已写入 {n} 个条目")
        self.statusMessage.emit(f"求解设置: 已把 {n} 个条目写入字典(可在生成字典页预览)")

    def _on_sim_type(self, *_a) -> None:
        self._fill_turbulence()
        self.changed.emit()

    def _on_model_changed(self, *_a) -> None:
        """切换湍流模型后, 提示需要在 0/ 里准备哪些场。"""
        self.changed.emit()
        if self.case is None:
            return
        sim = dictfile.get_atom(self._mt, "simulationType", "laminar")
        if sim == "laminar":
            return
        sub = dictfile.get_dict(self._mt, sim)
        model = dictfile.get_atom(sub, "model", "") if sub else ""
        needed = fields_mod.TURBULENCE_FIELDS.get(model or "", [])
        missing = [n for n in needed if n not in self.case.fields]
        if missing:
            self.statusMessage.emit(
                f"模型 {model} 需要场 {'、'.join(needed)}; 当前缺少 {'、'.join(missing)},"
                " 请到『初始条件』页添加(否则求解器会报找不到场)"
            )
        else:
            self.statusMessage.emit(f"模型 {model} 需要的场都已存在: {'、'.join(needed) or '(无)'}")

    # -- 物性 -----------------------------------------------------------
    def _fill_physical(self) -> None:
        f = self.phys_form
        self._clear_form(f)
        d = self.case.constant.get("physicalProperties", (FoamDict(), ""))[0] if self.case else FoamDict()
        self._pp = d
        cb = self.changed.emit
        add_atom_row(f, d, "viscosityModel", "viscosityModel", "choice",
                     ["constant", "polynomial", "BirdCarreau", "CrossPowerLaw"], "constant", "", cb)
        add_dimensioned_row(f, d, "rho", "密度 rho", ["1", "-3", "0", "0", "0", "0", "0"], "1",
                            "不可压求解器里 rho 只用于后处理", cb)
        add_dimensioned_row(f, d, "nu", "运动粘度 nu", ["0", "2", "-1", "0", "0", "0", "0"], "1e-05",
                            "空气约 1.5e-05", cb)

    # -- fvSchemes ------------------------------------------------------
    def _fill_schemes(self) -> None:
        f = self.scheme_form
        self._clear_form(f)
        d = self.case.system.get("fvSchemes", (FoamDict(), ""))[0] if self.case else FoamDict()
        self._fs = d
        for key, title, choices in (
            ("ddtSchemes", "ddtSchemes", DDT_SCHEMES),
            ("gradSchemes", "gradSchemes", GRAD_SCHEMES),
            ("laplacianSchemes", "laplacianSchemes", LAPLACIAN_SCHEMES),
            ("interpolationSchemes", "interpolationSchemes", INTERPOLATION_SCHEMES),
            ("snGradSchemes", "snGradSchemes", SNGRAD_SCHEMES),
        ):
            sub = dictfile.get_dict(d, key)
            if sub is None:
                sub = FoamDict()
                sub.set("default", choices[0])
                d.set(key, sub)
            add_atom_row(f, sub, "default", title + " default", "choice", choices,
                         dictfile.get_atom(sub, "default", choices[0]), "", self.changed.emit)
        wd = dictfile.get_dict(d, "wallDist")
        if wd is None:
            wd = FoamDict()
            wd.set("method", "meshWave")
            d.set("wallDist", wd)
        add_atom_row(f, wd, "method", "wallDist method", "choice",
                     ["meshWave", "Poisson", "adiabatic"], "meshWave", "", self.changed.emit)
        self._fill_div_table()

    def _fill_div_table(self) -> None:
        div = dictfile.get_dict(self._fs, "divSchemes")
        if div is None:
            div = FoamDict()
            div.set("default", "none")
            self._fs.set("divSchemes", div)
        self.div_table.blockSignals(True)
        self.div_table.setRowCount(0)
        for k, v in div.items:
            r = self.div_table.rowCount()
            self.div_table.insertRow(r)
            self.div_table.setItem(r, 0, QtWidgets.QTableWidgetItem(k))
            self.div_table.setItem(r, 1, QtWidgets.QTableWidgetItem(dictfile.format_value(v)))
        self.div_table.blockSignals(False)

    def _rebuild_div_dict(self) -> None:
        if self._loading or self.case is None:
            return
        new = FoamDict()
        for i in range(self.div_table.rowCount()):
            k = self.div_table.item(i, 0)
            v = self.div_table.item(i, 1)
            if k and v and k.text().strip():
                new.set(k.text().strip(), v.text().strip() or "none")
        self._fs.set("divSchemes", new)
        self.changed.emit()

    def _add_div_row(self) -> None:
        r = self.div_table.rowCount()
        self.div_table.blockSignals(True)
        self.div_table.insertRow(r)
        self.div_table.setItem(r, 0, QtWidgets.QTableWidgetItem("div(phi,U)"))
        self.div_table.setItem(r, 1, QtWidgets.QTableWidgetItem("bounded Gauss linearUpwind grad(U)"))
        self.div_table.blockSignals(False)
        self._rebuild_div_dict()

    def _del_div_row(self) -> None:
        r = self.div_table.currentRow()
        if r < 0:
            return
        self.div_table.blockSignals(True)
        self.div_table.removeRow(r)
        self.div_table.blockSignals(False)
        self._rebuild_div_dict()

    # -- fvSolution -----------------------------------------------------
    def _fill_solution(self) -> None:
        d = self.case.system.get("fvSolution", (FoamDict(), ""))[0] if self.case else FoamDict()
        self._fvs = d
        solvers = dictfile.get_dict(d, "solvers")
        if solvers is None:
            solvers = FoamDict()
            d.set("solvers", solvers)
        self.solver_table.blockSignals(True)
        self.solver_table.setRowCount(0)
        for name, sub in solvers.items:
            if not isinstance(sub, FoamDict):
                continue
            r = self.solver_table.rowCount()
            self.solver_table.insertRow(r)
            self.solver_table.setItem(r, 0, QtWidgets.QTableWidgetItem(name))
            for col, key in enumerate(["solver", "smoother", "nSweeps", "tolerance", "relTol"]):
                self.solver_table.setItem(
                    r, col + 1, QtWidgets.QTableWidgetItem(dictfile.get_atom(sub, key, ""))
                )
        self.solver_table.blockSignals(False)
        self._fill_solution_extras()

    def _fill_solution_extras(self) -> None:
        f = self.solution_form
        self._clear_form(f)
        d = self._fvs
        cb = self.changed.emit
        sim = dictfile.get_dict(d, "SIMPLE")
        pimple = dictfile.get_dict(d, "PIMPLE")
        # 稳态用 SIMPLE, 瞬态用 PIMPLE; 两者都要能填"非正交修正步数"和压力参考
        if sim is not None:
            add_atom_row(f, sim, "nNonOrthogonalCorrectors", "SIMPLE 非正交修正步数", "text", None, "0", "", cb)
            add_atom_row(f, sim, "consistent", "SIMPLE consistent", "choice", ["yes", "no"], "yes", "", cb)
            add_atom_row(f, sim, "pRefCell", "参考压力单元 pRefCell", "text", None, "0", "", cb)
            add_atom_row(f, sim, "pRefValue", "参考压力值 pRefValue", "text", None, "0", "", cb)
        if pimple is not None:
            add_atom_row(f, pimple, "nOuterCorrectors", "PIMPLE 外迭代次数", "text", None, "1", "", cb)
            add_atom_row(f, pimple, "nCorrectors", "PIMPLE 内迭代次数", "text", None, "2", "", cb)
            add_atom_row(f, pimple, "nNonOrthogonalCorrectors", "PIMPLE 非正交修正步数", "text", None, "0", "", cb)
            add_atom_row(f, pimple, "momentumPredictor", "动量预测 momentumPredictor",
                         "choice", ["yes", "no"], "yes", "瞬态常关掉(no)以省时间", cb)
            add_atom_row(f, pimple, "pRefCell", "参考压力单元 pRefCell", "text", None, "0", "", cb)
            add_atom_row(f, pimple, "pRefValue", "参考压力值 pRefValue", "text", None, "0", "", cb)
        if sim is None and pimple is None:
            # 文件里既没有 SIMPLE 也没有 PIMPLE: 不悄悄加, 给个显式按钮
            steady = self._is_steady()
            algo = "SIMPLE" if steady else "PIMPLE"
            btn = QtWidgets.QPushButton(f"补上 {algo} 算法段({'稳态' if steady else '瞬态'})")
            btn.setToolTip(
                "fvSolution 里没有 SIMPLE/PIMPLE 段, 求解器无法运行。\n"
                f"按当前时间格式({'steadyState' if steady else '非稳态'})补一个 {algo} 段"
            )

            def _add_algo() -> None:
                sub = FoamDict()
                if algo == "SIMPLE":
                    sub.set("nNonOrthogonalCorrectors", "0")
                    sub.set("consistent", "yes")
                else:
                    sub.set("nOuterCorrectors", "2")
                    sub.set("nCorrectors", "2")
                    sub.set("nNonOrthogonalCorrectors", "0")
                sub.set("pRefCell", "0")
                sub.set("pRefValue", "0")
                d.set(algo, sub)
                self._fill_solution_extras()
                self.changed.emit()
                self.statusMessage.emit(f"已补上 {algo} 算法段(可继续调整)")

            btn.clicked.connect(_add_algo)
            f.addRow(btn)

    def _is_steady(self) -> bool:
        """按 fvSchemes 的 ddtSchemes/default 判断是不是稳态。"""
        if self.case is None:
            return True
        fvs = self.case.get("system", "fvSchemes")
        ddt = dictfile.get_dict(fvs, "ddtSchemes") if fvs is not None else None
        return (dictfile.get_atom(ddt, "default", "steadyState") or "").strip().lower() == "steadystate"
        relax = dictfile.get_dict(d, "relaxationFactors")
        if relax is not None:
            fields = dictfile.get_dict(relax, "fields")
            if fields is None:
                fields = FoamDict()
                relax.set("fields", fields)
            self._relax_table(f, "松弛因子 fields", fields)
            eqs = dictfile.get_dict(relax, "equations")
            if eqs is None:
                eqs = FoamDict()
                relax.set("equations", eqs)
            self._relax_table(f, "松弛因子 equations", eqs)

    def _relax_table(self, f: QtWidgets.QFormLayout, title: str, d: FoamDict) -> None:
        t = QtWidgets.QTableWidget(len(d.items), 2)
        t.setHorizontalHeaderLabels(["场", "因子"])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setStretchLastSection(True)
        t.setMinimumHeight(120)
        t.blockSignals(True)
        for r, (k, _v) in enumerate(d.items):
            t.setItem(r, 0, QtWidgets.QTableWidgetItem(k))
            t.setItem(r, 1, QtWidgets.QTableWidgetItem(dictfile.get_atom(d, k, "")))
        t.blockSignals(False)
        t.itemChanged.connect(lambda _i, dd=d, tt=t: self._rebuild_relax(dd, tt))
        f.addRow(QtWidgets.QLabel(title))
        f.addRow(t)

    def _rebuild_relax(self, d: FoamDict, t: QtWidgets.QTableWidget) -> None:
        if self._loading:
            return
        new = FoamDict()
        for r in range(t.rowCount()):
            k = t.item(r, 0)
            v = t.item(r, 1)
            if k and v and k.text().strip():
                new.set(k.text().strip(), v.text().strip() or "1")
        for key in list(d.keys()):
            del d[key]
        for key, val in new.items:
            d.set(key, val)
        self.changed.emit()

    def _rebuild_solvers(self) -> None:
        if self._loading or self.case is None:
            return
        solvers = dictfile.get_dict(self._fvs, "solvers")
        if solvers is None:
            return
        new = FoamDict()
        for r in range(self.solver_table.rowCount()):
            name_item = self.solver_table.item(r, 0)
            if name_item is None or not name_item.text().strip():
                continue
            sub = FoamDict()
            for col, key in enumerate(["solver", "smoother", "nSweeps", "tolerance", "relTol"]):
                it = self.solver_table.item(r, col + 1)
                if it is not None and it.text().strip():
                    sub.set(key, it.text().strip())
            new.set(name_item.text().strip(), sub)
        for key in list(solvers.keys()):
            del solvers[key]
        for k, v in new.items:
            solvers.set(k, v)
        self.changed.emit()
