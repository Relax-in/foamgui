"""网格显示页: 三维窗口 + 补片/显示控制。"""

from __future__ import annotations

import os
import time

from PyQt6 import QtCore, QtWidgets

from ..foam.polymesh import PolyMesh
from .mesh_scene import MeshScene
from .widgets import ColorButton

__all__ = ["MeshView", "MeshTab"]


class MeshView(QtWidgets.QWidget):
    """承载 VTK 渲染窗口的 Qt 控件。

    在无图形环境下(自检/CI)可以设置环境变量 ``FOAMGUI_SKIP_VTK_WIDGET=1``,
    此时用一个占位控件代替, 场景本身仍然可用(可离屏渲染)。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = MeshScene()
        self.vtk_widget = None
        self.render_window = None
        self.interactor = None
        self._marker = None
        self._placeholder = None

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        if os.environ.get("FOAMGUI_SKIP_VTK_WIDGET"):
            self._placeholder = QtWidgets.QLabel("(自检模式: 未创建 VTK 渲染窗口)")
            self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(self._placeholder)
            return

        try:
            from vtkmodules.qt.QVTKRenderWindowInteractor import QVTKRenderWindowInteractor
            from vtkmodules.vtkInteractionStyle import vtkInteractorStyleTrackballCamera
            from vtkmodules.vtkInteractionWidgets import vtkOrientationMarkerWidget

            self.vtk_widget = QVTKRenderWindowInteractor(self)
            layout.addWidget(self.vtk_widget)
            self.render_window = self.vtk_widget.GetRenderWindow()
            self.render_window.AddRenderer(self.scene.renderer)
            self.vtk_widget.Initialize()
            self.interactor = self.render_window.GetInteractor()
            if self.interactor is None:  # 个别 VTK 版本要 Initialize 之后才有
                self.interactor = self.vtk_widget.GetRenderWindow().GetInteractor()
            if self.interactor is not None:
                self.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())

            self._marker = vtkOrientationMarkerWidget()
            self._marker.SetOrientationMarker(self.scene.axes_actor)
            if self.interactor is not None:
                self._marker.SetInteractor(self.interactor)
            self._marker.SetViewport(0.0, 0.0, 0.16, 0.22)
            if self.interactor is not None:
                self._marker.SetEnabled(1)
                self._marker.InteractiveOff()
        except Exception as exc:  # pragma: no cover - 环境相关
            self._placeholder = QtWidgets.QLabel(
                f"无法创建 VTK 渲染窗口:\n{exc}\n\n(补片与参数设置功能仍可使用)"
            )
            self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._placeholder.setWordWrap(True)
            layout.addWidget(self._placeholder)

    # -- 对外接口 -----------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.render_window is not None

    def set_mesh(self, mesh: PolyMesh) -> None:
        self.scene.set_mesh(mesh)
        self.scene.reset_camera("+z")
        self.render()

    def render(self) -> None:
        if self.render_window is not None:
            self.render_window.Render()

    def reset_view(self, direction: str) -> None:
        self.scene.reset_camera(direction)
        self.render()

    def export_png(self, path: str) -> bool:
        if self.render_window is None:
            return False
        from vtkmodules.vtkIOImage import vtkPNGWriter
        from vtkmodules.vtkRenderingCore import vtkWindowToImageFilter

        self.render_window.Render()
        w2i = vtkWindowToImageFilter()
        w2i.SetInput(self.render_window)
        w2i.Update()
        writer = vtkPNGWriter()
        writer.SetFileName(path)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()
        return os.path.exists(path)


class MeshTab(QtWidgets.QWidget):
    """网格页: 左边三维窗口, 右边补片与显示控制。"""

    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case = None
        self.mesh: PolyMesh | None = None
        self._patch_buttons: dict[str, QtWidgets.QTreeWidgetItem] = {}

        self.view = MeshView(self)
        self.panel = self._build_panel()

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self.view)
        splitter.addWidget(self.panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([760, 400])

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)

        self._clip_timer = QtCore.QTimer(self)
        self._clip_timer.setSingleShot(True)
        self._clip_timer.setInterval(120)
        self._clip_timer.timeout.connect(self._apply_clip)

    # ------------------------------------------------------------------
    def _build_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(panel)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(8)

        # 补片列表
        box = QtWidgets.QGroupBox("边界补片")
        bl = QtWidgets.QVBoxLayout(box)
        self.patch_tree = QtWidgets.QTreeWidget()
        self.patch_tree.setColumnCount(5)
        self.patch_tree.setHeaderLabels(["显示", "补片", "类型", "面数", "颜色"])
        self.patch_tree.setRootIsDecorated(False)
        self.patch_tree.setUniformRowHeights(True)
        self.patch_tree.setMinimumHeight(130)
        self.patch_tree.setColumnWidth(0, 42)
        self.patch_tree.setColumnWidth(1, 96)
        self.patch_tree.setColumnWidth(2, 56)
        self.patch_tree.setColumnWidth(3, 52)
        self.patch_tree.setColumnWidth(4, 40)
        self.patch_tree.itemChanged.connect(self._on_patch_item_changed)
        bl.addWidget(self.patch_tree)
        row = QtWidgets.QHBoxLayout()
        btn_all = QtWidgets.QPushButton("全选")
        btn_none = QtWidgets.QPushButton("全不选")
        btn_all.clicked.connect(lambda: self._set_all_patches(True))
        btn_none.clicked.connect(lambda: self._set_all_patches(False))
        row.addWidget(btn_all)
        row.addWidget(btn_none)
        bl.addLayout(row)
        lay.addWidget(box)

        # 显示选项
        box2 = QtWidgets.QGroupBox("显示")
        f2 = QtWidgets.QFormLayout(box2)
        self.cb_internal = QtWidgets.QCheckBox("显示内部网格线")
        self.cb_internal.toggled.connect(lambda v: (self.view.scene.set_edges_visible(v), self.view.render()))
        self.cb_outline = QtWidgets.QCheckBox("显示外框")
        self.cb_outline.setChecked(True)
        self.cb_outline.toggled.connect(lambda v: (self.view.scene.set_outline_visible(v), self.view.render()))
        self.cb_patch_edges = QtWidgets.QCheckBox("补片边线")
        self.cb_patch_edges.setChecked(True)
        self.cb_patch_edges.toggled.connect(
            lambda v: (self.view.scene.set_patch_edges_visible(v), self.view.render())
        )
        self.cb_volume_color = QtWidgets.QCheckBox("按单元体积着色")
        self.cb_volume_color.toggled.connect(self._on_volume_color)
        f2.addRow(self.cb_internal)
        f2.addRow(self.cb_outline)
        f2.addRow(self.cb_patch_edges)
        f2.addRow(self.cb_volume_color)
        self.sl_opacity = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.sl_opacity.setRange(15, 100)
        self.sl_opacity.setValue(100)
        self.sl_opacity.valueChanged.connect(
            lambda v: (self.view.scene.set_patch_opacity_all(v / 100.0), self.view.render())
        )
        f2.addRow("补片透明度", self.sl_opacity)
        lay.addWidget(box2)

        # 剖切
        box3 = QtWidgets.QGroupBox("内部剖切")
        f3 = QtWidgets.QFormLayout(box3)
        self.cb_clip = QtWidgets.QCheckBox("启用剖切")
        self.cb_clip.toggled.connect(lambda _v: self._schedule_clip())
        self.cmb_clip_axis = QtWidgets.QComboBox()
        self.cmb_clip_axis.addItems(["x", "y", "z"])
        self.cmb_clip_axis.setCurrentText("z")
        self.cmb_clip_axis.currentTextChanged.connect(lambda _t: self._schedule_clip())
        self.sl_clip = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.sl_clip.setRange(0, 100)
        self.sl_clip.setValue(50)
        self.sl_clip.valueChanged.connect(lambda _v: self._schedule_clip())
        self.cb_clip_invert = QtWidgets.QCheckBox("反向")
        self.cb_clip_invert.toggled.connect(lambda _v: self._schedule_clip())
        self.cb_clip_edges = QtWidgets.QCheckBox("显示网格线")
        self.cb_clip_edges.toggled.connect(
            lambda v: (self.view.scene.set_clip_edges_visible(v), self.view.render())
        )
        f3.addRow(self.cb_clip)
        f3.addRow("剖切方向", self.cmb_clip_axis)
        f3.addRow("位置", self.sl_clip)
        f3.addRow(self.cb_clip_invert)
        f3.addRow(self.cb_clip_edges)
        lay.addWidget(box3)

        # 视角
        box4 = QtWidgets.QGroupBox("视角")
        g = QtWidgets.QGridLayout(box4)
        for i, (label, direction) in enumerate(
            [("+X", "+x"), ("-X", "-x"), ("+Y", "+y"), ("-Y", "-y"), ("+Z", "+z"), ("-Z", "-z")]
        ):
            b = QtWidgets.QPushButton(label)
            b.clicked.connect(lambda _c=False, d=direction: self.view.reset_view(d))
            g.addWidget(b, i // 3, i % 3)
        b_iso = QtWidgets.QPushButton("等轴测")
        b_iso.clicked.connect(lambda: self.view.reset_view("iso"))
        g.addWidget(b_iso, 2, 0, 1, 3)
        b_png = QtWidgets.QPushButton("导出图片…")
        b_png.clicked.connect(self._export_png)
        g.addWidget(b_png, 3, 0, 1, 3)
        lay.addWidget(box4)

        # 信息
        box5 = QtWidgets.QGroupBox("网格信息")
        v5 = QtWidgets.QVBoxLayout(box5)
        self.lbl_info = QtWidgets.QLabel("尚未加载网格")
        self.lbl_info.setWordWrap(True)
        self.lbl_info.setMinimumHeight(150)
        self.lbl_info.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        v5.addWidget(self.lbl_info)
        lay.addWidget(box5)

        lay.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        return scroll

    # ------------------------------------------------------------------
    # 加载与刷新
    # ------------------------------------------------------------------
    def set_case(self, case, load_mesh: bool = True) -> None:
        self.case = case
        self.patch_tree.blockSignals(True)
        self.patch_tree.clear()
        self._patch_buttons.clear()
        self.patch_tree.blockSignals(False)
        self.mesh = None
        self.lbl_info.setText("正在读取网格…")
        if not load_mesh or case is None:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            t0 = time.time()
            mesh = case.load_mesh()
            self.mesh = mesh
            self.view.set_mesh(mesh)
            self._fill_patches(mesh)
            self._fill_info(mesh, time.time() - t0)
            self.statusMessage.emit(f"网格读取完成: {mesh.n_cells} 个单元, {mesh.n_faces} 个面")
        except Exception as exc:
            self.lbl_info.setText(f"网格读取失败:\n{exc}")
            self.statusMessage.emit(f"网格读取失败: {exc}")
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def _fill_patches(self, mesh: PolyMesh) -> None:
        self.patch_tree.blockSignals(True)
        self.patch_tree.clear()
        self._patch_buttons.clear()
        for patch in mesh.patches:
            item = QtWidgets.QTreeWidgetItem(["", patch.name, patch.type, str(patch.n_faces), ""])
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                0, QtCore.Qt.CheckState.Unchecked if patch.is_empty else QtCore.Qt.CheckState.Checked
            )
            color = self.view.scene.patch_colors.get(patch.name, (0.5, 0.5, 0.5))
            btn = ColorButton(color)
            btn.colorChanged.connect(
                lambda rgb, name=patch.name: (self.view.scene.set_patch_color(name, rgb), self.view.render())
            )
            self.patch_tree.addTopLevelItem(item)
            self.patch_tree.setItemWidget(item, 4, self._wrap(btn))
            self._patch_buttons[patch.name] = item
        self.patch_tree.blockSignals(False)

        # 2D 案例(有 empty 补片)默认打开内部网格线, 更能看清网格
        is_2d = any(p.is_empty for p in mesh.patches)
        self.cb_internal.setChecked(is_2d)
        self.view.scene.set_edges_visible(is_2d)
        visible = [p.name for p in mesh.patches if not p.is_empty]
        self.statusMessage.emit("显示补片: " + ", ".join(visible) if visible else "")

    @staticmethod
    def _wrap(widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        holder = QtWidgets.QWidget()
        lay = QtWidgets.QHBoxLayout(holder)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.addWidget(widget)
        lay.addStretch(1)
        return holder

    def _fill_info(self, mesh: PolyMesh, elapsed: float) -> None:
        lo, hi = mesh.bounds
        sizes = hi - lo
        vols = self.view.scene.cell_volumes
        text = (
            f"网格文件格式: {mesh.header_format}\n"
            f"点数: {mesh.n_points:,}    面数: {mesh.n_faces:,}\n"
            f"内部面: {mesh.n_internal_faces:,}    单元数: {mesh.n_cells:,}\n"
            f"补片数: {len(mesh.patches)}\n"
            f"包围盒: ({lo[0]:.4g}, {lo[1]:.4g}, {lo[2]:.4g}) ~ "
            f"({hi[0]:.4g}, {hi[1]:.4g}, {hi[2]:.4g})\n"
            f"尺寸: {sizes[0]:.4g} × {sizes[1]:.4g} × {sizes[2]:.4g}"
        )
        if sizes[2] > 0 and min(sizes[0], sizes[1]) / max(sizes[2], 1e-30) > 50:
            text += "\n(判定为二维网格: 厚度方向仅一层单元)"
        if vols is not None and vols.size:
            text += (
                f"\n单元体积: 最小 {vols.min():.4g}  最大 {vols.max():.4g}\n"
                f"          平均 {vols.mean():.4g}  总和 {vols.sum():.6g}\n"
                f"最大/最小比: {vols.max() / max(vols.min(), 1e-30):.3g}"
            )
        text += f"\n读取耗时: {elapsed:.2f} s"
        self.lbl_info.setText(text)

    # ------------------------------------------------------------------
    def _on_patch_item_changed(self, item: QtWidgets.QTreeWidgetItem, column: int) -> None:
        if column != 0:
            return
        name = item.text(1)
        visible = item.checkState(0) == QtCore.Qt.CheckState.Checked
        self.view.scene.set_patch_visible(name, visible)
        self.view.render()

    def _set_all_patches(self, visible: bool) -> None:
        self.patch_tree.blockSignals(True)
        for name, item in self._patch_buttons.items():
            item.setCheckState(0, QtCore.Qt.CheckState.Checked if visible else QtCore.Qt.CheckState.Unchecked)
            self.view.scene.set_patch_visible(name, visible)
        self.patch_tree.blockSignals(False)
        self.view.render()

    def _on_volume_color(self, enabled: bool) -> None:
        if enabled:
            self.view.scene.apply_volume_colors()
        else:
            self.view.scene.reset_patch_colors()
        self.view.render()

    def _schedule_clip(self) -> None:
        self._clip_timer.start()

    def _apply_clip(self) -> None:
        self.view.scene.set_clip(
            self.cb_clip.isChecked(),
            self.cmb_clip_axis.currentText(),
            self.sl_clip.value() / 100.0,
            self.cb_clip_invert.isChecked(),
        )
        self.view.render()

    def _export_png(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出当前视图", "mesh_view.png", "PNG 图片 (*.png)"
        )
        if not path:
            return
        if self.view.export_png(path):
            self.statusMessage.emit(f"已导出图片: {path}")
        else:
            QtWidgets.QMessageBox.warning(self, "导出失败", "当前环境没有可用的 VTK 渲染窗口。")
