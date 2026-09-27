"""三维网格场景(纯 VTK, 不依赖 Qt)。

拆成独立模块的好处: 既可以被 Qt 的 ``QVTKRenderWindowInteractor`` 承载,
也可以在无图形界面的环境下离屏渲染出图(自检 / 出报告)。

VTK 数据集都直接从 :class:`~foamgui.foam.polymesh.PolyMesh` 的 numpy 数组构造:

* 体网格   -> ``vtkUnstructuredGrid``, 每个 OpenFOAM 多面体单元用"单元中心 +
  各面三角扇"分解成四面体。VTK 对四面体的裁剪/取边支持最完善, 而把
  OpenFOAM 多面体直接建成 CONVEX_POINT_SET 在裁剪时会崩、体积也不准;
* 边界补片 -> ``vtkPolyData``(每个 patch 一个 actor, 便于单独控制颜色/显隐);
* 内部网格线 -> 所有面片 ``vtkExtractEdges`` 得到的线框(2D 案例正好显示平面网格);
* 内部剖切 -> ``vtkClipDataSet``, 并按"所属原始单元的体积"着色;
* 单元体积 -> 用 :meth:`PolyMesh.cell_volumes` 的 numpy 结果(已与解析解核对)。
"""

from __future__ import annotations

import numpy as np

import vtkmodules.vtkInteractionStyle  # noqa: F401  注册交互样式
import vtkmodules.vtkRenderingOpenGL2  # noqa: F401  注册 OpenGL 后端
from vtkmodules.util.numpy_support import numpy_to_vtk
from vtkmodules.vtkCommonCore import vtkFloatArray, vtkLookupTable, vtkPoints
from vtkmodules.vtkCommonDataModel import (
    VTK_TETRA,
    vtkCellArray,
    vtkPlane,
    vtkPolyData,
    vtkUnstructuredGrid,
)
from vtkmodules.vtkFiltersCore import vtkCellDataToPointData, vtkExtractEdges
from vtkmodules.vtkFiltersGeneral import vtkClipDataSet
from vtkmodules.vtkFiltersGeometry import vtkDataSetSurfaceFilter
from vtkmodules.vtkFiltersModeling import vtkOutlineFilter
from vtkmodules.vtkRenderingAnnotation import vtkAxesActor
from vtkmodules.vtkRenderingCore import vtkActor, vtkPolyDataMapper, vtkRenderer

from ..foam.polymesh import Patch, PolyMesh

# 补片配色
PATCH_PALETTE: list[tuple[float, float, float]] = [
    (0.30, 0.60, 0.95),
    (0.95, 0.45, 0.30),
    (0.40, 0.80, 0.45),
    (0.85, 0.75, 0.30),
    (0.70, 0.45, 0.85),
    (0.35, 0.80, 0.85),
    (0.90, 0.55, 0.75),
    (0.60, 0.60, 0.60),
]
WALL_COLOR = (0.72, 0.72, 0.75)
EMPTY_COLOR = (0.52, 0.55, 0.60)


def default_patch_color(index: int, patch: Patch) -> tuple[float, float, float]:
    if patch.type == "wall":
        return WALL_COLOR
    if patch.type in ("empty", "wedge"):
        return EMPTY_COLOR
    return PATCH_PALETTE[index % len(PATCH_PALETTE)]


class MeshScene:
    """网格的三维场景(含所有 actor 与滤镜)。"""

    def __init__(self):
        self.mesh: PolyMesh | None = None
        self.renderer = vtkRenderer()
        self.renderer.SetBackground(0.12, 0.13, 0.16)
        self.renderer.SetBackground2(0.22, 0.24, 0.30)
        self.renderer.GradientBackgroundOn()

        self.patch_actors: dict[str, vtkActor] = {}
        self.patch_colors: dict[str, tuple[float, float, float]] = {}
        self.patch_visible: dict[str, bool] = {}
        self.grid: vtkUnstructuredGrid | None = None
        self.points_vtk: vtkPoints | None = None
        self.cell_volumes: np.ndarray | None = None

        self.edges_actor: vtkActor | None = None
        self.outline_actor: vtkActor | None = None
        self.clip_actor: vtkActor | None = None
        self.clip_edges_actor: vtkActor | None = None
        self.clip_plane = vtkPlane()
        self.clip_enabled = False
        self.clip_axis = "z"
        self.clip_frac = 0.5
        self.clip_invert = False
        self._clip_filter: vtkClipDataSet | None = None
        self._clip_mapper: vtkPolyDataMapper | None = None
        self._clip_edges: vtkExtractEdges | None = None
        self._clip_surface: vtkDataSetSurfaceFilter | None = None
        self._edges_visible = False
        self._outline_visible = True
        self._clip_edges_visible = False

        self.axes_actor = vtkAxesActor()
        self.axes_widget = None
        self._lut = _make_lut()
        self._bounds: tuple[float, float, float, float, float, float] = (0, 1, 0, 1, 0, 1)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def clear(self) -> None:
        self.renderer.RemoveAllViewProps()
        self.patch_actors.clear()
        self.patch_colors.clear()
        self.patch_visible.clear()
        self.edges_actor = None
        self.outline_actor = None
        self.clip_actor = None
        self.clip_edges_actor = None
        self.grid = None
        self.mesh = None

    def set_mesh(self, mesh: PolyMesh) -> None:
        self.clear()
        self.mesh = mesh
        p = mesh.points
        self._bounds = (
            float(p[:, 0].min()),
            float(p[:, 0].max()),
            float(p[:, 1].min()),
            float(p[:, 1].max()),
            float(p[:, 2].min()),
            float(p[:, 2].max()),
        )
        self.cell_volumes = mesh.cell_volumes()
        self._build_cell_grid(mesh)
        self._build_patch_actors(mesh)
        self._build_wireframe(mesh)
        self._build_outline(mesh)
        self._build_axes()
        self.set_clip(False)

    # -- 体网格(四面体分解) ---------------------------------------------
    def _build_cell_grid(self, mesh: PolyMesh) -> None:
        conn, cells = _tetrahedralise(mesh)
        centroids = mesh.cell_centres()
        pts_np = np.vstack([mesh.points, centroids])
        pts = vtkPoints()
        parr = numpy_to_vtk(np.ascontiguousarray(pts_np, dtype=np.float64), deep=True)
        parr.SetName("points")
        pts.SetData(parr)
        self.points_vtk = pts

        ca = vtkCellArray()
        offsets = np.arange(0, conn.size + 1, 4, dtype=np.int64)
        ca.SetData(
            numpy_to_vtk(offsets, deep=True, array_type=12),
            numpy_to_vtk(conn, deep=True, array_type=12),
        )
        grid = vtkUnstructuredGrid()
        grid.SetPoints(pts)
        grid.SetCells(VTK_TETRA, ca)

        vols = self.cell_volumes
        varr = numpy_to_vtk(np.ascontiguousarray(vols[cells], dtype=np.float64), deep=True)
        varr.SetName("Volume")
        grid.GetCellData().AddArray(varr)
        carr = numpy_to_vtk(cells, deep=True, array_type=12)
        carr.SetName("CellId")
        grid.GetCellData().AddArray(carr)
        self.grid = grid

        clip = vtkClipDataSet()
        # 先把单元数据插值到点上, 剖面着色才能平滑/正确
        c2p = vtkCellDataToPointData()
        c2p.SetInputData(grid)
        c2p.PassCellDataOn()
        c2p.Update()
        clip.SetInputData(c2p.GetOutput())
        clip.SetClipFunction(self.clip_plane)
        clip.SetInsideOut(False)
        # vtkClipDataSet 输出的是 vtkUnstructuredGrid, 必须转成 polydata 才能给 mapper
        surf = vtkDataSetSurfaceFilter()
        surf.SetInputConnection(clip.GetOutputPort())
        self._clip_surface = surf
        cmap = vtkPolyDataMapper()
        cmap.SetInputConnection(surf.GetOutputPort())
        cmap.SetLookupTable(self._lut)
        cmap.SetScalarModeToUsePointFieldData()
        cmap.SelectColorArray("Volume")
        cmap.SetScalarRange(self._volume_range())
        cmap.ScalarVisibilityOn()
        cactor = vtkActor()
        cactor.SetMapper(cmap)
        cactor.GetProperty().SetInterpolationToFlat()
        cactor.SetVisibility(False)
        self.clip_actor = cactor
        self._clip_filter = clip
        self._clip_mapper = cmap
        self.renderer.AddActor(cactor)

        cedges = vtkExtractEdges()
        cedges.SetInputConnection(clip.GetOutputPort())
        cemap = vtkPolyDataMapper()
        cemap.SetInputConnection(cedges.GetOutputPort())
        ceactor = vtkActor()
        ceactor.SetMapper(cemap)
        ceactor.GetProperty().SetColor(0.10, 0.10, 0.12)
        ceactor.GetProperty().SetLineWidth(0.4)
        ceactor.GetProperty().SetOpacity(0.35)
        ceactor.SetVisibility(False)
        self.clip_edges_actor = ceactor
        self._clip_edges = cedges
        self.renderer.AddActor(ceactor)

    def _volume_range(self) -> tuple[float, float]:
        if self.cell_volumes is None or self.cell_volumes.size == 0:
            return (0.0, 1.0)
        lo = float(self.cell_volumes.min())
        hi = float(self.cell_volumes.max())
        return (lo, max(hi, lo + 1e-30))

    # -- 补片 -----------------------------------------------------------
    def _build_patch_actors(self, mesh: PolyMesh) -> None:
        for i, patch in enumerate(mesh.patches):
            polys = vtkCellArray()
            for f in mesh.faces[patch.start_face : patch.start_face + patch.n_faces]:
                polys.InsertNextCell(len(f), f.astype(np.int64))
            pd = vtkPolyData()
            pd.SetPoints(self.points_vtk)
            pd.SetPolys(polys)
            mapper = vtkPolyDataMapper()
            mapper.SetInputData(pd)
            mapper.ScalarVisibilityOff()
            actor = vtkActor()
            actor.SetMapper(mapper)
            color = default_patch_color(i, patch)
            prop = actor.GetProperty()
            prop.SetColor(*color)
            prop.SetEdgeVisibility(True)
            prop.SetEdgeColor(0.12, 0.12, 0.15)
            prop.SetLineWidth(1.0)
            visible = not patch.is_empty
            actor.SetVisibility(visible)
            self.patch_actors[patch.name] = actor
            self.patch_colors[patch.name] = color
            self.patch_visible[patch.name] = visible
            self.renderer.AddActor(actor)

    def _build_wireframe(self, mesh: PolyMesh) -> None:
        polys = vtkCellArray()
        for f in mesh.faces:
            polys.InsertNextCell(len(f), f.astype(np.int64))
        pd = vtkPolyData()
        pd.SetPoints(self.points_vtk)
        pd.SetPolys(polys)
        edges = vtkExtractEdges()
        edges.SetInputData(pd)
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(edges.GetOutputPort())
        actor = vtkActor()
        actor.SetMapper(mapper)
        prop = actor.GetProperty()
        prop.SetColor(0.62, 0.72, 0.85)
        prop.SetLineWidth(1.0)
        prop.SetOpacity(0.9)
        actor.SetVisibility(False)
        self.edges_actor = actor
        self.renderer.AddActor(actor)

    def _build_outline(self, mesh: PolyMesh) -> None:
        polys = vtkCellArray()
        for f in mesh.faces:
            polys.InsertNextCell(len(f), f.astype(np.int64))
        pd = vtkPolyData()
        pd.SetPoints(self.points_vtk)
        pd.SetPolys(polys)
        outline = vtkOutlineFilter()
        outline.SetInputData(pd)
        mapper = vtkPolyDataMapper()
        mapper.SetInputConnection(outline.GetOutputPort())
        actor = vtkActor()
        actor.SetMapper(mapper)
        actor.GetProperty().SetColor(0.80, 0.82, 0.88)
        actor.GetProperty().SetLineWidth(1.5)
        self.outline_actor = actor
        self.renderer.AddActor(actor)

    def _build_axes(self) -> None:
        self.axes_actor.SetXAxisLabelText("X")
        self.axes_actor.SetYAxisLabelText("Y")
        self.axes_actor.SetZAxisLabelText("Z")
        self.axes_actor.SetTotalLength(1.0, 1.0, 1.0)

    # ------------------------------------------------------------------
    # 交互控制
    # ------------------------------------------------------------------
    def set_patch_visible(self, name: str, visible: bool) -> None:
        self.patch_visible[name] = bool(visible)
        actor = self.patch_actors.get(name)
        if actor is not None:
            actor.SetVisibility(bool(visible) and not self.clip_enabled)

    def set_all_patches_visible(self, visible: bool) -> None:
        for name in self.patch_actors:
            self.set_patch_visible(name, visible)

    def set_patch_color(self, name: str, color: tuple[float, float, float]) -> None:
        actor = self.patch_actors.get(name)
        if actor is not None:
            actor.GetProperty().SetColor(*color)
            self.patch_colors[name] = tuple(color)

    def set_patch_opacity_all(self, alpha: float) -> None:
        for actor in self.patch_actors.values():
            actor.GetProperty().SetOpacity(float(alpha))

    def set_edges_visible(self, visible: bool) -> None:
        self._edges_visible = bool(visible)
        if self.edges_actor is not None:
            self.edges_actor.SetVisibility(self._edges_visible and not self.clip_enabled)

    def set_clip_edges_visible(self, visible: bool) -> None:
        self._clip_edges_visible = bool(visible)
        if self.clip_edges_actor is not None:
            self.clip_edges_actor.SetVisibility(self._clip_edges_visible and self.clip_enabled)

    def set_outline_visible(self, visible: bool) -> None:
        self._outline_visible = bool(visible)
        if self.outline_actor is not None:
            self.outline_actor.SetVisibility(self._outline_visible)

    def set_patch_edges_visible(self, visible: bool) -> None:
        for actor in self.patch_actors.values():
            actor.GetProperty().SetEdgeVisibility(bool(visible))

    def set_clip(
        self,
        enabled: bool,
        axis: str | None = None,
        frac: float | None = None,
        invert: bool | None = None,
    ) -> None:
        self.clip_enabled = bool(enabled)
        if axis:
            self.clip_axis = axis
        if frac is not None:
            self.clip_frac = float(frac)
        if invert is not None:
            self.clip_invert = bool(invert)
        self._update_clip()
        for name, actor in self.patch_actors.items():
            actor.SetVisibility(self.patch_visible.get(name, True) and not self.clip_enabled)
        if self.clip_actor is not None:
            self.clip_actor.SetVisibility(self.clip_enabled)
        if self.clip_edges_actor is not None:
            self.clip_edges_actor.SetVisibility(self.clip_enabled and self._clip_edges_visible)
        if self.edges_actor is not None:
            self.edges_actor.SetVisibility(self._edges_visible and not self.clip_enabled)

    def _update_clip(self) -> None:
        p = self._bounds
        axis = {"x": 0, "y": 1, "z": 2}.get(self.clip_axis, 2)
        lo, hi = p[axis * 2], p[axis * 2 + 1]
        pos = lo + (hi - lo) * min(max(self.clip_frac, 0.0), 1.0)
        origin = [0.0, 0.0, 0.0]
        origin[axis] = pos
        normal = [0.0, 0.0, 0.0]
        normal[axis] = 1.0
        self.clip_plane.SetOrigin(*origin)
        self.clip_plane.SetNormal(*normal)
        if self._clip_filter is not None:
            self._clip_filter.SetInsideOut(self.clip_invert)
            self._clip_filter.Update()
        if self._clip_mapper is not None:
            self._clip_mapper.SetScalarRange(self._volume_range())
        if self._clip_surface is not None:
            self._clip_surface.Update()
        if self._clip_edges is not None:
            self._clip_edges.Update()

    def apply_volume_colors(self) -> None:
        """按所属单元的体积给补片着色(快速查看网格疏密)。"""
        if self.cell_volumes is None or self.mesh is None:
            return
        vol = self.cell_volumes
        lo, hi = float(vol.min()), float(vol.max())
        span = max(hi - lo, 1e-30)
        for patch in self.mesh.patches:
            actor = self.patch_actors.get(patch.name)
            if actor is None:
                continue
            mapper = actor.GetMapper()
            pd = mapper.GetInput()
            colours = vtkFloatArray()
            colours.SetName("volume")
            colours.SetNumberOfComponents(3)
            for fi in range(patch.start_face, patch.start_face + patch.n_faces):
                t = float((vol[self.mesh.owner[fi]] - lo) / span)
                rgb = [0.0, 0.0, 0.0]
                self._lut.GetColor(t, rgb)
                colours.InsertNextTuple3(*rgb)
            pd.GetCellData().SetScalars(colours)
            mapper.ScalarVisibilityOn()
            mapper.SetScalarModeToUseCellData()

    def reset_patch_colors(self) -> None:
        if self.mesh is None:
            return
        for i, patch in enumerate(self.mesh.patches):
            actor = self.patch_actors.get(patch.name)
            if actor is None:
                continue
            mapper = actor.GetMapper()
            mapper.ScalarVisibilityOff()
            mapper.GetInput().GetCellData().SetScalars(None)
            self.set_patch_color(patch.name, default_patch_color(i, patch))

    # -- 视角 -----------------------------------------------------------
    def reset_camera(self, direction: str = "+z") -> None:
        cam = self.renderer.GetActiveCamera()
        p = self._bounds
        centre = ((p[0] + p[1]) / 2, (p[2] + p[3]) / 2, (p[4] + p[5]) / 2)
        span = max(p[1] - p[0], p[3] - p[2], p[5] - p[4], 1e-9)
        dist = span * 2.0
        dirs = {
            "+x": (1, 0, 0),
            "-x": (-1, 0, 0),
            "+y": (0, 1, 0),
            "-y": (0, -1, 0),
            "+z": (0, 0, 1),
            "-z": (0, 0, -1),
            "iso": (1.0, -0.8, 0.6),
        }
        v = dirs.get(direction, dirs["+z"])
        norm = max((v[0] ** 2 + v[1] ** 2 + v[2] ** 2) ** 0.5, 1e-9)
        cam.SetFocalPoint(*centre)
        cam.SetPosition(
            centre[0] + v[0] / norm * dist,
            centre[1] + v[1] / norm * dist,
            centre[2] + v[2] / norm * dist,
        )
        if direction in ("+z", "-z", "iso"):
            cam.SetViewUp(0, 1, 0)
        else:
            cam.SetViewUp(0, 0, 1)
        cam.SetParallelProjection(True)
        self.renderer.ResetCamera()
        cam.Zoom(1.1)
        self.renderer.ResetCameraClippingRange()


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _tetrahedralise(mesh: PolyMesh) -> tuple[np.ndarray, np.ndarray]:
    """把多面体网格分解成四面体。

    每个 (单元, 面) 取面的三角扇, 与"单元中心"组成四面体。返回
    ``(connectivity, cell_of_tet)``; connectivity 每 4 个一组指向点数组,
    其中单元中心的编号为 ``nPoints + cell``。
    """
    indptr, fids = mesh.cell_faces()
    entry_cell = np.repeat(np.arange(mesh.n_cells, dtype=np.int64), np.diff(indptr))
    if entry_cell.size == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)
    entry_face = fids.astype(np.int64)
    sizes = mesh.face_sizes[entry_face]
    tri = np.maximum(sizes - 2, 0)
    total = int(tri.sum())
    if total == 0:
        return np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64)

    entry_idx = np.repeat(np.arange(entry_cell.size, dtype=np.int64), tri)
    offset_in_entry = np.arange(total, dtype=np.int64) - np.repeat(
        np.concatenate(([0], np.cumsum(tri)))[:-1], tri
    )
    base = mesh.face_offsets[entry_face][entry_idx]
    fp = mesh.face_points
    i0 = fp[base]
    i1 = fp[base + offset_in_entry + 1]
    i2 = fp[base + offset_in_entry + 2]
    # neighbour 侧的面要反向, 保证以单元中心为顶点时四面体朝外
    is_neighbour = mesh.owner[entry_face[entry_idx]] != entry_cell[entry_idx]
    i1, i2 = np.where(is_neighbour, i2, i1), np.where(is_neighbour, i1, i2)
    centroid = mesh.n_points + entry_cell[entry_idx]

    conn = np.empty(total * 4, dtype=np.int64)
    conn[0::4] = centroid
    conn[1::4] = i0
    conn[2::4] = i1
    conn[3::4] = i2
    return conn, entry_cell[entry_idx]


def _make_lut() -> vtkLookupTable:
    lut = vtkLookupTable()
    lut.SetHueRange(0.66, 0.0)  # 蓝 -> 红
    lut.SetSaturationRange(1.0, 1.0)
    lut.SetValueRange(1.0, 1.0)
    lut.SetNumberOfTableValues(64)
    lut.Build()
    return lut
