"""主窗口: 把网格 / 初始条件 / 边界条件 / 求解设置 / 生成预览 串起来。"""

from __future__ import annotations

import os
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from ..foam import dictfile, ofenv
from ..foam.case import FoamCase
from .bc_tab import BoundaryConditionsTab, InitialConditionsTab
from .mesh_tab import MeshTab
from .output_tab import OutputTab
from .solver_tab import SolverTab

__all__ = ["MainWindow"]

RECENT_KEY = "foamgui/recent_cases"


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("OpenFOAM 前处理助手")
        self.resize(1500, 950)
        self.case: FoamCase | None = None
        self._dirty = False

        self.mesh_tab = MeshTab(self)
        self.ic_tab = InitialConditionsTab(self)
        self.bc_tab = BoundaryConditionsTab(self)
        self.solver_tab = SolverTab(self)
        self.output_tab = OutputTab(self)

        self.tabs = QtWidgets.QTabWidget()
        self.tabs.addTab(self.mesh_tab, "1. 网格")
        self.tabs.addTab(self.ic_tab, "2. 初始条件")
        self.tabs.addTab(self.bc_tab, "3. 边界条件")
        self.tabs.addTab(self.solver_tab, "4. 求解设置")
        self.tabs.addTab(self.output_tab, "5. 生成字典")
        self.setCentralWidget(self.tabs)

        self._build_actions()
        self._build_menus()
        self._build_dock()
        self._build_statusbar()

        for tab in (self.ic_tab, self.bc_tab, self.solver_tab):
            tab.changed.connect(self._on_changed)
        for tab in (self.mesh_tab, self.ic_tab, self.bc_tab, self.solver_tab, self.output_tab):
            tab.statusMessage.connect(self.show_message)
        self.tabs.currentChanged.connect(self._on_tab_changed)

        self._restore_geometry()

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
        dock.setMinimumWidth(320)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.RightDockWidgetArea, dock)
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
        self.mesh_tab.set_case(case, load_mesh=mesh_error is None)
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

    # ------------------------------------------------------------------
    def _show_help(self) -> None:
        QtWidgets.QMessageBox.information(
            self,
            "使用说明",
            "<h3>OpenFOAM 前处理助手</h3>"
            "<ol>"
            "<li><b>打开案例</b>: 选择包含 <code>constant/polyMesh</code> 的案例目录 "
            "(例如 airFoil2D)。</li>"
            "<li><b>1. 网格</b>: 读取并三维显示 polyMesh, 可按补片显示/上色、显示内部网格线、"
            "剖切看内部、导出图片。</li>"
            "<li><b>2. 初始条件</b>: 设置每个场 <code>0/&lt;场&gt;</code> 的 internalField。</li>"
            "<li><b>3. 边界条件</b>: 选择场, 逐补片选择边界条件类型并填参数; "
            "可以按补片名一键推荐(会根据 inlet/outlet/wall/empty 自动判断)。</li>"
            "<li><b>4. 求解设置</b>: controlDict / 湍流模型 / 物性 / 离散格式 / 线性求解器。</li>"
            "<li><b>5. 生成字典</b>: 预览每个文件的内容, 确认后写入案例目录(自动备份到 "
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
