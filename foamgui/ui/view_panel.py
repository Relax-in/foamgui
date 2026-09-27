"""三维视图面板: 常驻的 VTK 窗口 + 视角/投影/显示/剖切控制条。

设计要点(按用户要求调整):
* 三维窗口不再属于某个页签, 而是常驻在左侧, 右边切换初始条件/边界条件/
  求解设置/生成字典时网格一直可见;
* 鼠标**单击**(按下与抬起之间几乎没移动)三维窗口里的补片表面即可选中它,
  拖动仍然是旋转/平移, 互不影响。
"""

from __future__ import annotations

import os
import time

from PyQt6 import QtCore, QtGui, QtWidgets

from ..foam.polymesh import PolyMesh
from .mesh_scene import MeshScene

__all__ = ["MeshView", "ViewPanel"]


class MeshView(QtWidgets.QWidget):
    """承载 VTK 渲染窗口的控件(带补片拾取)。"""

    patchClicked = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.scene = MeshScene()
        self.vtk_widget = None
        self.render_window = None
        self.interactor = None
        self._marker = None
        self._placeholder = None
        self._press_pos: tuple[int, int] | None = None
        self._dragged = False

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
            self.render_window.SetMultiSamples(0)
            self.vtk_widget.Initialize()
            self.interactor = self.render_window.GetInteractor()
            if self.interactor is not None:
                self.interactor.SetInteractorStyle(vtkInteractorStyleTrackballCamera())
            # 拾取在 Qt 层处理(见 _on_* ): VTK 的 LeftButtonReleaseEvent 在本版本
            # 由 QVTK 转发时不会真正派发, 挂在交互器上收不到, 所以不依赖它。
            self.vtk_widget.installEventFilter(self)

            self._marker = vtkOrientationMarkerWidget()
            self._marker.SetOrientationMarker(self.scene.axes_actor)
            if self.interactor is not None:
                self._marker.SetInteractor(self.interactor)
                self._marker.SetViewport(0.0, 0.0, 0.16, 0.22)
                self._marker.SetEnabled(1)
                self._marker.InteractiveOff()
        except Exception as exc:  # pragma: no cover - 环境相关
            self._placeholder = QtWidgets.QLabel(
                f"无法创建 VTK 渲染窗口:\n{exc}\n\n(补片与参数设置功能仍可使用)"
            )
            self._placeholder.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            self._placeholder.setWordWrap(True)
            layout.addWidget(self._placeholder)

    # -- 拾取(在 Qt 层处理鼠标事件) ------------------------------------------
    def _to_vtk_coords(self, pos) -> tuple[int, int]:
        """Qt 控件坐标 -> VTK 显示坐标(与 QVTK 内部的换算保持一致)。"""
        widget = self.vtk_widget
        dpr = widget.devicePixelRatio()
        return (
            int(round(pos.x() * dpr)),
            int(round((widget.height() - pos.y() - 1) * dpr)),
        )

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is not self.vtk_widget or self.vtk_widget is None:
            return False
        etype = event.type()
        if etype == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._press_pos = event.position()
            self._dragged = False
        elif etype == QtCore.QEvent.Type.MouseMove and self._press_pos is not None:
            p = event.position()
            if abs(p.x() - self._press_pos.x()) > 4 or abs(p.y() - self._press_pos.y()) > 4:
                self._dragged = True
        elif etype == QtCore.QEvent.Type.MouseButtonRelease and event.button() == QtCore.Qt.MouseButton.LeftButton:
            pos, dragged = self._press_pos, self._dragged
            self._press_pos = None
            self._dragged = False
            if pos is not None and not dragged:
                x, y = self._to_vtk_coords(event.position())
                name = self.scene.pick_patch(x, y)
                if name:
                    self.patchClicked.emit(name)
        return False  # 不拦截, 旋转/平移仍由 VTK 的交互样式处理

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


class ViewPanel(QtWidgets.QWidget):
    """三维窗口 + 一条紧凑控制栏(视角 / 投影 / 显示 / 剖切)。"""

    patchClicked = QtCore.pyqtSignal(str)
    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case = None
        self.mesh: PolyMesh | None = None

        self.view = MeshView(self)
        self.view.patchClicked.connect(self.patchClicked)
        controls = self._build_controls()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)
        lay.addWidget(controls)
        lay.addWidget(self.view, 1)

        self._clip_timer = QtCore.QTimer(self)
        self._clip_timer.setSingleShot(True)
        self._clip_timer.setInterval(120)
        self._clip_timer.timeout.connect(self._apply_clip)

    # ------------------------------------------------------------------
    def _small_button(self, text: str, tip: str = "") -> QtWidgets.QPushButton:
        b = QtWidgets.QPushButton(text)
        b.setMaximumHeight(24)
        b.setMinimumWidth(34)
        b.setToolTip(tip or text)
        b.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        return b

    def _build_controls(self) -> QtWidgets.QWidget:
        box = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)

        # 第 1 行: 视角 / 投影 / 导出
        row1 = QtWidgets.QHBoxLayout()
        row1.setSpacing(3)
        row1.addWidget(QtWidgets.QLabel("视角"))
        for label, direction in (
            ("+X", "+x"), ("-X", "-x"), ("+Y", "+y"),
            ("-Y", "-y"), ("+Z", "+z"), ("-Z", "-z"),
        ):
            b = self._small_button(label, f"沿 {label} 方向看")
            b.clicked.connect(lambda _c=False, d=direction: self.view.reset_view(d))
            row1.addWidget(b)
        b_iso = self._small_button("等轴测", "等轴测视角")
        b_iso.clicked.connect(lambda: self.view.reset_view("iso"))
        row1.addWidget(b_iso)
        row1.addSpacing(10)
        row1.addWidget(QtWidgets.QLabel("投影"))
        self.cmb_projection = QtWidgets.QComboBox()
        self.cmb_projection.setMaximumHeight(24)
        self.cmb_projection.addItems(["正交 (工程)", "透视"])
        self.cmb_projection.setToolTip("正交: 平行投影, 便于看几何; 透视: 有近大远小")
        self.cmb_projection.currentIndexChanged.connect(
            lambda i: (self.view.scene.set_projection(i == 0), self.view.render())
        )
        row1.addWidget(self.cmb_projection)
        row1.addStretch(1)
        b_png = self._small_button("导出图片…")
        b_png.clicked.connect(self._export_png)
        row1.addWidget(b_png)
        grid.addLayout(row1, 0, 0)

        # 第 2 行: 显示与剖切
        row2 = QtWidgets.QHBoxLayout()
        row2.setSpacing(6)
        self.cb_internal = QtWidgets.QCheckBox("内部网格线")
        self.cb_internal.setToolTip("显示单元之间的网格线(2D 案例用这个看网格最清楚)")
        self.cb_internal.toggled.connect(
            lambda v: (self.view.scene.set_edges_visible(v), self.view.render())
        )
        self.cb_outline = QtWidgets.QCheckBox("外框")
        self.cb_outline.setChecked(True)
        self.cb_outline.toggled.connect(
            lambda v: (self.view.scene.set_outline_visible(v), self.view.render())
        )
        self.cb_patch_edges = QtWidgets.QCheckBox("补片边线")
        self.cb_patch_edges.setChecked(True)
        self.cb_patch_edges.toggled.connect(
            lambda v: (self.view.scene.set_patch_edges_visible(v), self.view.render())
        )
        for cb in (self.cb_internal, self.cb_outline, self.cb_patch_edges):
            row2.addWidget(cb)
        row2.addSpacing(10)

        self.cb_clip = QtWidgets.QCheckBox("剖切")
        self.cb_clip.setToolTip("沿某个方向剖开, 看网格内部(剖面按单元体积着色)")
        self.cb_clip.toggled.connect(lambda _v: self._schedule_clip())
        row2.addWidget(self.cb_clip)
        self.cmb_clip_axis = QtWidgets.QComboBox()
        self.cmb_clip_axis.setMaximumHeight(24)
        self.cmb_clip_axis.addItems(["x", "y", "z"])
        self.cmb_clip_axis.setCurrentText("z")
        self.cmb_clip_axis.currentTextChanged.connect(lambda _t: self._schedule_clip())
        self.cmb_clip_axis.setEnabled(False)
        row2.addWidget(self.cmb_clip_axis)
        self.sl_clip = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.sl_clip.setRange(0, 100)
        self.sl_clip.setValue(50)
        self.sl_clip.setMaximumWidth(140)
        self.sl_clip.setEnabled(False)
        self.sl_clip.valueChanged.connect(lambda _v: self._schedule_clip())
        row2.addWidget(self.sl_clip)
        self.cb_clip_invert = QtWidgets.QCheckBox("反向")
        self.cb_clip_invert.setEnabled(False)
        self.cb_clip_invert.toggled.connect(lambda _v: self._schedule_clip())
        row2.addWidget(self.cb_clip_invert)
        self.cb_clip_edges = QtWidgets.QCheckBox("剖面网格线")
        self.cb_clip_edges.setEnabled(False)
        self.cb_clip_edges.toggled.connect(
            lambda v: (self.view.scene.set_clip_edges_visible(v), self.view.render())
        )
        row2.addWidget(self.cb_clip_edges)
        row2.addStretch(1)
        grid.addLayout(row2, 1, 0)
        return box

    # ------------------------------------------------------------------
    def set_case(self, case, load_mesh: bool = True) -> None:
        self.case = case
        self.mesh = None
        if not load_mesh or case is None:
            return
        QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.CursorShape.WaitCursor)
        try:
            t0 = time.time()
            mesh = case.load_mesh()
            self.mesh = mesh
            self.view.set_mesh(mesh)
            self.cb_internal.setChecked(any(p.is_empty for p in mesh.patches))
            self.view.scene.set_edges_visible(self.cb_internal.isChecked())
            self.statusMessage.emit(
                f"网格读取完成: {mesh.n_cells:,} 单元 / {mesh.n_points:,} 点 / "
                f"{mesh.n_faces:,} 面 ({mesh.header_format}), 用时 {time.time() - t0:.2f}s"
            )
        except Exception as exc:
            self.statusMessage.emit(f"网格读取失败: {exc}")
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()

    def set_patch_visible(self, name: str, visible: bool) -> None:
        self.view.scene.set_patch_visible(name, visible)
        self.view.render()

    def set_patch_color(self, name: str, color) -> None:
        self.view.scene.set_patch_color(name, color)
        self.view.render()

    def set_selected_patch(self, name: str | None) -> None:
        self.view.scene.set_selected_patch(name)
        self.view.render()

    def set_volume_color(self, enabled: bool) -> None:
        if enabled:
            self.view.scene.apply_volume_colors()
        else:
            self.view.scene.reset_patch_colors()
        self.view.render()

    def apply_clip_enabled_ui(self) -> None:
        on = self.cb_clip.isChecked()
        for w in (self.cmb_clip_axis, self.sl_clip, self.cb_clip_invert, self.cb_clip_edges):
            w.setEnabled(on)

    def _schedule_clip(self) -> None:
        self.apply_clip_enabled_ui()
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
