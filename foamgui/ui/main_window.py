"""主窗口: 把网格 / 初始条件 / 边界条件 / 求解设置 / 生成预览 串起来。"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from ..foam import dictfile, ofenv
from ..foam.case import FoamCase
from .bc_tab import BoundaryConditionsTab, InitialConditionsTab
from .errors import install_excepthook
from .output_tab import OutputTab
from .patch_panel import PatchPanel
from .solver_tab import SolverTab
from .view_panel import ViewPanel

__all__ = ["MainWindow"]

RECENT_KEY = "foamgui/recent_cases"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OpenFOAM 前处理助手")
        self.resize(1500, 950)
        self.case: FoamCase | None = None
        self._dirty = False
        self._syncing_patch = False

        # 左边常驻三维窗口, 右上补片树(模型树), 右下设置页签
        self.view_panel = ViewPanel(self)
        self.patch_panel = PatchPanel(self)
        self.ic_tab = InitialConditionsTab(self)
        self.bc_tab = BoundaryConditionsTab(self)
        self.solver_tab = SolverTab(self)
        self.output_tab = OutputTab(self)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.bc_tab, "边界条件")
        self.tabs.addTab(self.ic_tab, "初始条件")
        self.tabs.addTab(self.solver_tab, "求解设置")
        self.tabs.addTab(self.output_tab, "生成字典")

        right = QtWidgets.QSplitter(QtCore.Qt.Orientation.Vertical)
        right.addWidget(self.patch_panel)
        right.addWidget(self.tabs)
        right.setStretchFactor(0, 0)
        right.setStretchFactor(1, 1)
        right.setSizes([300, 620])

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        split.addWidget(self.view_panel)
        split.addWidget(right)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 0)
        split.setSizes([960, 460])
        self.setCentralWidget(split)

        self._build_actions()
        self._build_dock()   # 菜单里的"视图"要用到 dock, 所以先建 dock
        self._build_menus()
        self._build_statusbar()

        for tab in (self.ic_tab, self.bc_tab, self.solver_tab):
            tab.changed.connect(self._on_changed)
        for tab in (self.ic_tab, self.bc_tab, self.solver_tab, self.output_tab):
            tab.statusMessage.connect(self.show_message)
        self.view_panel.statusMessage.connect(self.show_message)
        self.patch_panel.statusMessage.connect(self.show_message)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        # 三维窗口 <-> 模型树 双向联动
        self.view_panel.patchClicked.connect(self._on_patch_picked)
        self.patch_panel.patchSelected.connect(self._on_patch_selected)
        self.patch_panel.patchVisibilityChanged.connect(self.view_panel.set_patch_visible)
        self.patch_panel.patchColorChanged.connect(self.view_panel.set_patch_color)
        self.patch_panel.patchRenameRequested.connect(self._rename_patch)
        self.patch_panel.patchTypeChangeRequested.connect(self._change_patch_type)
        self.patch_panel.volumeColorToggled.connect(self.view_panel.set_volume_color)
        self.bc_tab.patchActivated.connect(self._on_patch_selected)

        # 槽函数里的未捕获异常在 PyQt6 里默认会让程序直接 abort(表现为"闪退"),
        # 这里换成弹提示 + 继续运行
        install_excepthook()

        self._restore_geometry()

    # ------------------------------------------------------------------
    # 三维窗口 <-> 模型树 联动
    # ------------------------------------------------------------------
    def _on_patch_picked(self, name: str) -> None:
        """在三维窗口里点到了某个补片。"""
        self.patch_panel.select(name)

    def _on_patch_selected(self, name: str) -> None:
        """补片被选中(来自三维窗口或模型树): 两边都高亮, 并跳到它的边界条件。"""
        if self._syncing_patch:
            return
        self._syncing_patch = True
        try:
            self.patch_panel.select(name, emit=False)
            self.patch_panel.set_patch_checked(name, True)
            self.view_panel.set_patch_visible(name, True)
            self.view_panel.set_selected_patch(name)
            self.bc_tab.select_patch(name)
            self.show_message(f"已选中补片: {name}")
        finally:
            self._syncing_patch = False

    def _change_patch_type(self, name: str, new_type: str) -> None:
        """改补片的网格类型(会写进 polyMesh/boundary), 之后要重刷边界条件候选。

        注意: 模型树里的下拉框发出请求时**已经延后了一轮**(见 PatchPanel.
        _request_type_change), 所以这里可以放心地重建面板/弹确认框 —— 不会在
        信号处理过程中把正在发信号的控件销毁掉。
        """
        if self.case is None:
            return
        try:
            self._apply_patch_type_change_inner(name, new_type)
        except Exception:  # noqa: BLE001 - 界面槽函数绝不能把异常抛回 Qt
            import traceback

            traceback.print_exc()
            self.show_message("改补片类型时出错, 详情见终端输出(程序继续运行)")

    def _apply_patch_type_change_inner(self, name: str, new_type: str) -> None:
        try:
            affected = self.case.set_patch_type(name, new_type)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "修改失败", str(exc))
            self.show_message(f"改补片类型失败: {exc}")
            if getattr(self.case, "mesh", None) is not None:
                self.patch_panel.set_mesh(self.case.mesh, self.view_panel.view.scene.patch_colors)
            return
        # 约束型补片(empty/wedge/symmetry)只能用同名的边界条件:
        # 网格类型改了以后, 该补片上原来的边界条件就失效了, 这里提示并同步
        fixed: list[str] = []
        if new_type in ("empty", "wedge", "symmetry", "symmetryPlane"):
            bad = [
                ff for ff in self.case.fields.values()
                if ff.patch_type(name) and ff.patch_type(name) != new_type
            ]
            if bad:
                detail = "\n".join(f"  · {ff.name}: {ff.patch_type(name)}" for ff in bad)
                ok = QtWidgets.QMessageBox.question(
                    self,
                    "同步边界条件",
                    f"补片 {name} 现在是 {new_type} 类型, 它上面的这些边界条件已经失效:\n"
                    f"{detail}\n\n是否自动把它们改成 {new_type} ?",
                )
                if ok == QtWidgets.QMessageBox.StandardButton.Yes:
                    for ff in bad:
                        d = ff.patch_dict(name)
                        for k in list(d.keys()):
                            del d[k]
                        d.set("type", new_type)
                        fixed.append(ff.name)
        # 注意: 这里**不重建模型树** —— 信号就是那棵树里的下拉框发出的,
        # 重建会把发信号的控件销毁掉; 下拉框本身已经显示新类型了。
        self.patch_panel.select(name, emit=False)
        self.bc_tab.set_case(self.case)
        self.bc_tab.select_patch(name)
        self.output_tab.refresh(force=True)
        self._on_changed()
        msg = f"补片 {name} 的网格类型已改为 {new_type}"
        if fixed:
            msg += f"; 已把这些场的边界条件同步为 {new_type}: {'、'.join(fixed)}"
        elif affected:
            msg += f"; 受影响的场: {'、'.join(affected)}(可在『边界条件』页检查)"
        msg += "。写出时会一并更新 constant/polyMesh/boundary"
        self.show_message(msg)

    def _on_fields_changed(self) -> None:
        """0/ 里新增了场: 刷新初始/边界条件页与字典预览。"""
        if self.case is None:
            return
        self.ic_tab.set_case(self.case)
        self.bc_tab.set_case(self.case)
        self.output_tab.refresh(force=True)
        self._on_changed()

    def _rename_patch(self, old: str, new: str) -> None:
        """补片改名(会同步网格 boundary 与各场的 boundaryField)。

        模型树里的改名请求同样已在 PatchPanel._request_rename 里延后过一轮,
        避免单元格编辑器还没提交就把控件树重建掉。
        """
        if self.case is None:
            return
        try:
            self._apply_rename_patch_inner(old, new)
        except Exception:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            self.show_message("补片改名时出错, 详情见终端输出(程序继续运行)")

    def _apply_rename_patch_inner(self, old: str, new: str) -> None:
        if self.case is None:
            return
        try:
            touched = self.case.rename_patch(old, new)
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "重命名失败", str(exc))
            self.patch_panel.set_mesh(self.case.mesh, self.view_panel.view.scene.patch_colors)
            return
        self.view_panel.view.scene.rename_patch_name(old, new)
        self.patch_panel.set_mesh(self.case.mesh, self.view_panel.view.scene.patch_colors)
        self.patch_panel.select(new, emit=False)
        self.view_panel.set_selected_patch(new)
        self.ic_tab.set_case(self.case)
        self.bc_tab.set_case(self.case)
        self.bc_tab.select_patch(new)
        self.output_tab.refresh(force=True)
        self._refresh_case_tree()
        self._on_changed()
        self.show_message(
            f"补片 {old} 已重命名为 {new}; 同步更新了 {len(touched)} 个场, "
            "写出时会一并更新 constant/polyMesh/boundary"
        )

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_actions(self) -> None:
        self.act_open = QtGui.QAction("打开案例…", self)
        self.act_open.setShortcut("Ctrl+O")
        self.act_open.triggered.connect(self.open_case_dialog)

        self.act_reload = QtGui.QAction("重新读取", self)
        self.act_reload.setShortcut("F5")
        self.act_reload.triggered.connect(lambda: self.load_case(self.case.root if self.case else None))

        self.act_recent = QtGui.QAction("最近打开的案例", self)
        self.menu_recent = QtWidgets.QMenu("最近打开的案例", self)

        self.act_write = QtGui.QAction("写出字典文件…", self)
        self.act_write.setShortcut("Ctrl+S")
        self.act_write.triggered.connect(self._write_dicts)

        self.act_quit = QtGui.QAction("退出", self)
        self.act_quit.setShortcut("Ctrl+Q")
        self.act_quit.triggered.connect(self.close)

        self.act_help = QtGui.QAction("使用说明", self)
        self.act_help.triggered.connect(self._show_help)
        self.act_about = QtGui.QAction("关于", self)
        self.act_about.triggered.connect(self._show_about)

    def _build_menus(self) -> None:
        m = self.menuBar().addMenu("文件")
        m.addAction(self.act_open)
        m.addAction(self.act_reload)
        m.addMenu(self.menu_recent)
        m.addSeparator()
        m.addAction(self.act_write)
        m.addSeparator()
        m.addAction(self.act_quit)
        mv = self.menuBar().addMenu("视图")
        mv.addAction(self.dock.toggleViewAction())
        act_fit = QtGui.QAction("重置视角(等轴测)", self)
        act_fit.setShortcut("Ctrl+0")
        act_fit.triggered.connect(lambda: self.view_panel.view.reset_view("iso"))
        mv.addAction(act_fit)
        act_front = QtGui.QAction("正视(+Z)", self)
        act_front.setShortcut("Ctrl+1")
        act_front.triggered.connect(lambda: self.view_panel.view.reset_view("+z"))
        mv.addAction(act_front)
        act_proj = QtGui.QAction("切换 正交/透视", self)
        act_proj.setShortcut("Ctrl+P")
        act_proj.triggered.connect(self._toggle_projection)
        mv.addAction(act_proj)

        mh = self.menuBar().addMenu("帮助")
        mh.addAction(self.act_help)
        mh.addAction(self.act_about)

        tb = self.addToolBar("主工具栏")
        tb.setMovable(False)
        tb.addAction(self.act_open)
        tb.addAction(self.act_reload)
        tb.addSeparator()
        tb.addAction(self.act_write)
        self._reload_recent_menu()

    def _build_dock(self) -> None:
        dock = QtWidgets.QDockWidget("案例信息", self)
        dock.setAllowedAreas(
            QtCore.Qt.DockWidgetArea.LeftDockWidgetArea | QtCore.Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.case_tree = QtWidgets.QTreeWidget()
        self.case_tree.setColumnCount(2)
        self.case_tree.setHeaderLabels(["项目", "值"])
        self.case_tree.setColumnWidth(0, 150)
        self.case_tree.setAlternatingRowColors(True)
        dock.setWidget(self.case_tree)
        dock.setMinimumWidth(300)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.hide()  # 默认收起, 把空间留给三维窗口(视图菜单里可打开)
        self.dock = dock

    def _build_statusbar(self) -> None:
        self.lbl_status = QtWidgets.QLabel("请先打开一个 OpenFOAM 案例目录(例如 airFoil2D)")
        self.statusBar().addWidget(self.lbl_status, 1)
        self.lbl_dirty = QtWidgets.QLabel("")
        self.statusBar().addPermanentWidget(self.lbl_dirty)

    # ------------------------------------------------------------------
    # 案例加载
    # ------------------------------------------------------------------
    def open_case_dialog(self) -> None:
        start = str(self.case.root) if self.case else os.path.expanduser("~")
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择 OpenFOAM 案例目录", start)
        if path:
            self.load_case(path)

    def load_case(self, path: str | Path | None) -> None:
        if not path:
            return
        path = Path(path)
        if not (path / "constant" / "polyMesh").exists():
            ok = QtWidgets.QMessageBox.question(
                self,
                "目录里没有 constant/polyMesh",
                f"{path}\n没有找到 constant/polyMesh, 仍然要打开吗?\n"
                "(没有网格时无法读取补片名, 边界条件只能手工添加)",
            )
            if ok != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            case = FoamCase(path)
            case.load()
        except Exception as exc:
            QtWidgets.QApplication.restoreOverrideCursor()
            QtWidgets.QMessageBox.critical(self, "打开失败", f"读取案例失败:\n{exc}")
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

        self.case = case
        self._dirty = False

        # 先把网格读进来(补片名是边界条件设置的基础)
        mesh_error = None
        try:
            case.load_mesh()
        except Exception as exc:
            mesh_error = str(exc)

        # 场与补片对齐(把网格里的新补片补进各场)
        added = case.sync_patches()
        if added:
            self.show_message(f"已自动补充 {len(added)} 个补片条目")

        self.ic_tab.set_case(case)
        self.bc_tab.set_case(case)
        self.solver_tab.set_case(case)
        self.output_tab.set_case(case)
        self.view_panel.set_case(case, load_mesh=mesh_error is None)
        self.patch_panel.set_mesh(case.mesh, self.view_panel.view.scene.patch_colors)
        if mesh_error:
            QtWidgets.QMessageBox.warning(self, "网格读取失败", mesh_error)
        self._refresh_case_tree()
        self._update_title()
        self._save_recent(str(case.root))
        self._reload_recent_menu()

        for w in case.warnings:
            self.show_message(f"提示: {w}")
        if case.warnings:
            QtWidgets.QMessageBox.information(self, "读取提示", "\n".join(case.warnings))

        self.show_message(
            f"已打开案例 {case.root}   场: {', '.join(case.fields.keys()) or '(无)'}   "
            f"补片: {', '.join(case.patch_names) or '(无)'}"
        )

    def _refresh_case_tree(self) -> None:
        t = self.case_tree
        t.clear()
        if self.case is None:
            return
        case = self.case
        root = QtWidgets.QTreeWidgetItem(["案例目录", str(case.root)])
        t.addTopLevelItem(root)

        cd = case.control_dict()
        solver = dictfile.get_atom(cd, "solver", "-")
        sim = dictfile.get_atom(case.get("constant", "momentumTransport"), "simulationType", "-")
        mt = case.get("constant", "momentumTransport")
        model = "-"
        if sim in ("RAS", "LES"):
            sub = dictfile.get_dict(mt, sim)
            model = dictfile.get_atom(sub, "model", "-") if sub else "-"
        info = QtWidgets.QTreeWidgetItem(
            ["求解设置", f"{solver} / {sim}{' ' + str(model) if model != '-' else ''}"]
        )
        t.addTopLevelItem(info)

        fields_item = QtWidgets.QTreeWidgetItem(["场 (0/)", f"{len(case.fields)} 个"])
        for name, ff in case.fields.items():
            fields_item.addChild(QtWidgets.QTreeWidgetItem([name, ff.cls.replace("vol", "").replace("Field", "")]))
        t.addTopLevelItem(fields_item)

        if case.mesh is not None:
            mesh_item = QtWidgets.QTreeWidgetItem(
                ["网格", f"{case.mesh.n_cells:,} 单元 / {case.mesh.n_points:,} 点"]
            )
            for pname, ptype, nfaces in case.patch_info():
                mesh_item.addChild(QtWidgets.QTreeWidgetItem([pname, f"{ptype}, {nfaces} 面"]))
            t.addTopLevelItem(mesh_item)
        t.expandAll()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _on_changed(self) -> None:
        self._dirty = True
        self._update_title()
        self.output_tab.mark_stale()

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.output_tab and self.case is not None:
            self.output_tab.refresh(force=True)

    def _update_title(self) -> None:
        name = self.case.name if self.case else "(未打开案例)"
        star = "*" if self._dirty else ""
        self.setWindowTitle(f"OpenFOAM 前处理助手 — {name}{star}")
        self.lbl_dirty.setText("有未写出的修改" if self._dirty else "已同步")

    def show_message(self, text: str) -> None:
        self.lbl_status.setText(text)

    # ------------------------------------------------------------------
    def _write_dicts(self) -> None:
        if self.case is None:
            QtWidgets.QMessageBox.information(self, "提示", "请先打开一个案例。")
            return
        self.tabs.setCurrentWidget(self.output_tab)
        self.output_tab.refresh(force=True)
        self.output_tab._write()

    # -- 最近打开 -------------------------------------------------------
    def _settings(self) -> QtCore.QSettings:
        return QtCore.QSettings("foamgui", "OpenFOAMPre")

    def _save_recent(self, path: str) -> None:
        s = self._settings()
        recent = s.value(RECENT_KEY, []) or []
        if isinstance(recent, str):
            recent = [recent]
        recent = [p for p in recent if p != path]
        recent.insert(0, path)
        s.setValue(RECENT_KEY, recent[:8])

    def _reload_recent_menu(self) -> None:
        self.menu_recent.clear()
        s = self._settings()
        recent = s.value(RECENT_KEY, []) or []
        if isinstance(recent, str):
            recent = [recent]
        if not recent:
            act = self.menu_recent.addAction("(空)")
            act.setEnabled(False)
            return
        for p in recent:
            act = self.menu_recent.addAction(p)
            act.triggered.connect(lambda _c=False, path=p: self.load_case(path))

    def _restore_geometry(self) -> None:
        s = self._settings()
        geo = s.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        if self._dirty:
            ok = QtWidgets.QMessageBox.question(
                self, "退出", "还有修改没有写出字典文件, 确定退出吗?"
            )
            if ok != QtWidgets.QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self._settings().setValue("geometry", self.saveGeometry())
        super().closeEvent(event)

    def _toggle_projection(self) -> None:
        combo = self.view_panel.cmb_projection
        combo.setCurrentIndex(1 if combo.currentIndex() == 0 else 0)
        self.show_message(f"投影方式: {combo.currentText()}")

    # ------------------------------------------------------------------
    def _show_help(self) -> None:
        QtWidgets.QMessageBox.information(
            self,
            "使用说明",
            "<h3>OpenFOAM 前处理助手</h3>"
            "<ol>"
            "<li><b>打开案例</b>: 选择包含 <code>constant/polyMesh</code> 的案例目录 "
            "(例如 airFoil2D)。</li>"
            "<li><b>三维窗口(常驻左侧)</b>: 显示 polyMesh; 上方控制栏可切换视角"
            "(±X/±Y/±Z/等轴测)、<b>正交/透视投影</b>、内部网格线、外框、补片边线, "
            "以及沿 x/y/z 剖切看内部(剖面按单元体积着色); 也可以导出图片。</li>"
            "<li><b>单击三维窗口里的补片表面</b>即可选中它: 右上角的模型树会自动选中对应行, "
            "下方边界条件页也会跳到该补片; 反过来在模型树里选一行, 三维窗口里对应补片会高亮。</li>"
            "<li><b>补片改名</b>: 在模型树里双击“补片”列(或点“重命名…”)。改名会同步到"
            "<code>constant/polyMesh/boundary</code> 与所有场的 boundaryField, 写出时一并更新。</li>"
            "<li><b>边界条件</b>: 选择场, 逐补片选择边界条件类型并填参数; "
            "可以按补片名一键推荐(会根据 inlet/outlet/wall/empty 自动判断)。</li>"
            "<li><b>初始条件</b>: 设置每个场 <code>0/&lt;场&gt;</code> 的 internalField。</li>"
            "<li><b>求解设置</b>: controlDict / 湍流模型 / 物性 / 离散格式 / 线性求解器。</li>"
            "<li><b>生成字典</b>: 预览每个文件的内容, 确认后写入案例目录(自动备份到 "
            "<code>foamgui_backup/</code>), 也可以另存到新目录, 或用 foamDictionary 校验。</li>"
            "</ol>"
            "<p>提示: 读取再写出的过程中, GUI 没有覆盖到的字典条目都会被原样保留。</p>",
        )

    def _show_about(self) -> None:
        foam = ofenv.find_foam_bin("foamRun") or "未检测到"
        QtWidgets.QMessageBox.about(
            self,
            "关于",
            "<h3>OpenFOAM 前处理助手</h3>"
            "<p>基于 PyQt6 + VTK 的 OpenFOAM 网格显示与边界条件设置工具。</p>"
            f"<p>OpenFOAM 求解器: <code>{foam}</code></p>",
        )
