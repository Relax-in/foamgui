"""OpenFOAM ``constant/polyMesh`` 网格读取器(纯 numpy 实现)。

支持 OpenFOAM 的 ASCII 与 binary 两种格式:

* ASCII  ``faces`` 既支持经典的嵌套写法 ``4(1 2 3 4)``, 也支持
  OpenFOAM 13 二进制下的紧凑写法 ``CompactListList``(偏移数组 + 展平标签);
* binary 文件在文件头之后是 ASCII 的 "个数 + (" , 紧接原始二进制数据,
  OpenFOAM 13 的 faces 则是"偏移数组, 再一个 ASCII 个数, 再展平标签"两段。

读取结果 :class:`PolyMesh` 保存 numpy 数组以及"单元 -> 点"的 CSR 索引,
足以支撑三维显示、网格统计以及后续的场数据映射。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import dictfile

__all__ = ["Patch", "PolyMesh", "MeshError", "read_polymesh", "find_polymesh_dir"]


class MeshError(Exception):
    """网格读取失败。"""


# 文件头的结束行: // * * * * * * * * * //
_SEP_RE = re.compile(rb"^[ \t]*//\s*(?:\*\s*)+//[ \t]*$", re.M)


@dataclass
class Patch:
    """一个边界补片(boundary patch)。"""

    name: str
    type: str = "patch"
    physical_type: str = ""
    n_faces: int = 0
    start_face: int = 0
    index: int = 0
    in_groups: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.type in ("empty", "wedge")

    @property
    def is_wall(self) -> bool:
        return self.type == "wall"

    @property
    def face_range(self) -> range:
        return range(self.start_face, self.start_face + self.n_faces)

    def describe(self) -> str:
        return f"{self.name}: {self.type}, {self.n_faces} 个面"


class PolyMesh:
    """OpenFOAM 多面体网格。

    属性
    ----
    points : (nPoints, 3) float64
    faces  : 每个面的点索引列表(list[np.ndarray])
    owner  : (nFaces,) int32            面的 owner 单元
    neighbour : (nInternalFaces,) int32
    patches: list[Patch]
    """

    def __init__(
        self,
        points: np.ndarray,
        faces: list[np.ndarray],
        owner: np.ndarray,
        neighbour: np.ndarray,
        patches: list[Patch],
        fmt: str = "ascii",
    ):
        self.points = np.ascontiguousarray(points, dtype=np.float64)
        self.faces = faces
        self.owner = np.ascontiguousarray(owner, dtype=np.int32)
        self.neighbour = np.ascontiguousarray(neighbour, dtype=np.int32)
        self.patches = patches
        self.path: Path | None = None
        self.header_format = fmt
        # boundary 文件的原始字典(重命名补片时要改它并写回)
        self.boundary_body: "dictfile.FoamDict | None" = None

        self.n_points = int(self.points.shape[0])
        self.n_faces = len(faces)
        self.n_internal_faces = int(self.neighbour.shape[0])
        self.n_cells = int(
            max(
                int(self.owner.max()) if self.owner.size else -1,
                int(self.neighbour.max()) if self.neighbour.size else -1,
            )
            + 1
        )

        self.face_sizes = np.array([len(f) for f in faces], dtype=np.int64)
        self.face_offsets = np.concatenate(([0], np.cumsum(self.face_sizes))).astype(np.int64)
        self.face_points = (
            np.concatenate(faces).astype(np.int32)
            if faces
            else np.zeros(0, dtype=np.int32)
        )

        self._cell_points: tuple[np.ndarray, np.ndarray] | None = None
        self._cell_faces: tuple[np.ndarray, np.ndarray] | None = None

    # -- 几何 ---------------------------------------------------------------
    @property
    def bounds(self) -> tuple[np.ndarray, np.ndarray]:
        if self.n_points == 0:
            return np.zeros(3), np.zeros(3)
        return self.points.min(axis=0), self.points.max(axis=0)

    @staticmethod
    def _group_average(
        indices: np.ndarray, offsets: np.ndarray, points: np.ndarray
    ) -> np.ndarray:
        """按 offsets 分组对点求平均(面心/单元中心)。"""
        n = len(offsets) - 1
        if n <= 0:
            return np.zeros((0, 3))
        sizes = np.diff(offsets)
        group = np.repeat(np.arange(n), sizes)
        out = np.zeros((n, 3), dtype=np.float64)
        for k in range(3):
            out[:, k] = np.bincount(group, weights=points[indices, k], minlength=n)
        out /= np.maximum(sizes, 1)[:, None]
        return out

    def face_centres(self) -> np.ndarray:
        return self._group_average(self.face_points, self.face_offsets, self.points)

    def cell_centres(self) -> np.ndarray:
        indptr, indices = self.cell_points()
        return self._group_average(indices, indptr, self.points)

    def cell_points(self) -> tuple[np.ndarray, np.ndarray]:
        """CSR 形式的 "单元 -> 去重后的点索引" ``(indptr, indices)``。"""
        if self._cell_points is None:
            self._cell_points = self._build_cell_points()
        return self._cell_points

    def _build_cell_points(self) -> tuple[np.ndarray, np.ndarray]:
        n_cells = self.n_cells
        if self.n_faces == 0 or n_cells == 0:
            return np.zeros(n_cells + 1, dtype=np.int64), np.zeros(0, dtype=np.int32)

        owner = self.owner.astype(np.int64)
        neigh = self.neighbour.astype(np.int64)
        entry_cell = np.concatenate([owner, neigh])
        entry_face = np.concatenate(
            [
                np.arange(self.n_faces, dtype=np.int64),
                np.arange(self.n_internal_faces, dtype=np.int64),
            ]
        )
        face_sizes = self.face_sizes[entry_face]
        total = int(face_sizes.sum())
        starts = self.face_offsets[entry_face]
        rep_starts = np.repeat(starts, face_sizes)
        # 每个 (单元,面) 条目内部从 0 开始的序号
        inner = np.arange(total, dtype=np.int64) - np.repeat(
            np.concatenate(([0], np.cumsum(face_sizes)))[:-1], face_sizes
        )
        point_ids = self.face_points[rep_starts + inner]
        cell_ids = np.repeat(entry_cell, face_sizes)

        key = np.unique(cell_ids * self.n_points + point_ids.astype(np.int64))
        cells = (key // self.n_points).astype(np.int64)
        pts = (key % self.n_points).astype(np.int32)
        counts = np.bincount(cells, minlength=n_cells)
        indptr = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
        return indptr, pts

    def cell_faces(self) -> tuple[np.ndarray, np.ndarray]:
        """CSR 形式的 "单元 -> 面索引" ``(indptr, indices)``。"""
        if self._cell_faces is None:
            cells = np.concatenate([self.owner.astype(np.int64), self.neighbour.astype(np.int64)])
            findex = np.concatenate(
                [
                    np.arange(self.n_faces, dtype=np.int64),
                    np.arange(self.n_internal_faces, dtype=np.int64),
                ]
            )
            order = np.argsort(cells, kind="stable")
            counts = np.bincount(cells[order], minlength=self.n_cells)
            indptr = np.concatenate(([0], np.cumsum(counts))).astype(np.int64)
            self._cell_faces = (indptr, findex[order])
        return self._cell_faces

    def cell_volumes(self) -> np.ndarray:
        """用散度定理向量化计算所有单元的体积。"""
        n_cells = self.n_cells
        if self.n_faces == 0 or n_cells == 0:
            return np.zeros(n_cells)
        # 每个面的三角扇(以面的第一个点为扇心)
        tri_per_face = np.maximum(self.face_sizes - 2, 0)
        tri_off = np.concatenate(([0], np.cumsum(tri_per_face))).astype(np.int64)
        total_tri = int(tri_off[-1])

        entry_face = np.concatenate(
            [
                np.arange(self.n_faces, dtype=np.int64),
                np.arange(self.n_internal_faces, dtype=np.int64),
            ]
        )
        entry_cell = np.concatenate([self.owner.astype(np.int64), self.neighbour.astype(np.int64)])
        # owner 侧的面点序给出外法向, neighbour 侧要反向, 否则单元体积符号会错乱
        entry_sign = np.concatenate(
            [
                np.ones(self.n_faces, dtype=np.float64),
                -np.ones(self.n_internal_faces, dtype=np.float64),
            ]
        )
        per_entry = tri_per_face[entry_face]
        n_tri = int(per_entry.sum())

        # 每个三角在该面内的局部编号
        local = np.arange(n_tri, dtype=np.int64) - np.repeat(
            np.concatenate(([0], np.cumsum(per_entry)))[:-1], per_entry
        )
        base = self.face_offsets[entry_face]
        base = np.repeat(base, per_entry)
        p0 = self.face_points[base]
        p1 = self.face_points[base + local + 1]
        p2 = self.face_points[base + local + 2]
        v0 = self.points[p0]
        v1 = self.points[p1]
        v2 = self.points[p2]
        tet = np.einsum("ij,ij->i", v0, np.cross(v1, v2)) / 6.0
        tet *= np.repeat(entry_sign, per_entry)
        cell_of_tri = np.repeat(entry_cell, per_entry)
        vols = np.bincount(cell_of_tri, weights=tet, minlength=n_cells)
        return np.abs(vols)

    # -- 补片 ---------------------------------------------------------------
    def boundary_face_indices(self, patch: Patch) -> np.ndarray:
        return np.arange(patch.start_face, patch.start_face + patch.n_faces, dtype=np.int64)

    def patch_by_name(self, name: str) -> Patch | None:
        for p in self.patches:
            if p.name == name:
                return p
        return None

    @property
    def patch_names(self) -> list[str]:
        return [p.name for p in self.patches]

    def summary(self) -> dict:
        lo, hi = self.bounds
        return {
            "n_points": self.n_points,
            "n_faces": self.n_faces,
            "n_internal_faces": self.n_internal_faces,
            "n_cells": self.n_cells,
            "n_patches": len(self.patches),
            "bounds_min": lo,
            "bounds_max": hi,
            "format": self.header_format,
        }


# ---------------------------------------------------------------------------
# 文件级读取
# ---------------------------------------------------------------------------
def find_polymesh_dir(case_dir: str | Path) -> Path:
    """定位 ``constant/polyMesh`` 目录(兼容多区域 region 目录)。"""
    case = Path(case_dir)
    poly = case / "constant" / "polyMesh"
    if (poly / "points").exists():
        return poly
    const = case / "constant"
    if const.is_dir():
        for sub in sorted(const.iterdir()):
            cand = sub / "polyMesh"
            if (cand / "points").exists():
                return cand
    raise MeshError(f"在 {case} 下找不到 constant/polyMesh")


def _split_header(raw: bytes) -> tuple[str, bytes]:
    m = _SEP_RE.search(raw)
    if not m:
        raise MeshError("文件缺少 OpenFOAM 文件头分隔行(// * * * //)")
    header = raw[: m.end()].decode("utf-8", errors="replace")
    return header, raw[m.end() :]


def _header_info(header_text: str) -> tuple[str, str]:
    """从文件头里取出 ``(format, class)``。"""
    try:
        header, _ = dictfile.parse_file(header_text)
    except Exception:  # pragma: no cover - 容错
        header = dictfile.FoamDict()
    fmt = (dictfile.get_atom(header, "format", "ascii") or "ascii").strip()
    cls = (dictfile.get_atom(header, "class", "") or "").strip()
    return fmt, cls


class _AsciiScanner:
    """面向大文件的轻量 ASCII 扫描器。"""

    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def skip_ws(self) -> None:
        t, i, n = self.text, self.pos, len(self.text)
        while i < n and t[i] in " \t\r\n":
            i += 1
        self.pos = i

    def peek(self) -> str:
        return self.text[self.pos : self.pos + 1]

    def read_count(self) -> int:
        self.skip_ws()
        t, i, n = self.text, self.pos, len(self.text)
        j = i
        while j < n and (t[j].isdigit() or t[j] in "+-"):
            j += 1
        if j == i:
            raise MeshError(f"期望一个整数, 实际读到 {t[i:i+20]!r}")
        self.pos = j
        return int(t[i:j])

    def expect(self, ch: str) -> None:
        self.skip_ws()
        if self.peek() != ch:
            raise MeshError(f"期望 {ch!r}, 实际读到 {self.text[self.pos:self.pos+20]!r}")
        self.pos += 1

    def read_list_content(self) -> str:
        """读取一个 ``( ... )``, 返回括号内文本(支持嵌套括号)。"""
        self.expect("(")
        start = self.pos
        depth = 1
        t = self.text
        i, n = start, len(t)
        while i < n:
            c = t[i]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    self.pos = i + 1
                    return t[start:i]
            i += 1
        raise MeshError("列表缺少 ')'")


def _ints_from_text(text: str) -> np.ndarray:
    parts = text.split()
    return np.array(parts, dtype=np.int64) if parts else np.zeros(0, dtype=np.int64)


def _read_points_ascii(sc: "_AsciiScanner", count: int) -> np.ndarray:
    content = sc.read_list_content()
    flat = np.array(content.replace("(", " ").replace(")", " ").split(), dtype=np.float64)
    if flat.size != count * 3:
        raise MeshError(f"points 数量不符: 头声明 {count}, 实际 {flat.size // 3}")
    return flat.reshape(count, 3)


def _read_faces_ascii(sc: "_AsciiScanner", count: int) -> list[np.ndarray]:
    content = sc.read_list_content()
    matches = re.findall(r"(\d+)\s*\(([^)]*)\)", content)
    if len(matches) == count:
        faces = []
        for m in matches:
            n = int(m[0])
            ids = np.array(m[1].split(), dtype=np.int32)
            if ids.size != n:
                raise MeshError("faces 条目长度与声明数量不一致")
            faces.append(ids)
        return faces
    # 紧凑写法: 第一段是偏移, 第二段是展平的点标签
    offsets = _ints_from_text(content)
    sc.skip_ws()
    if offsets.size >= 2 and sc.peek().isdigit():
        n2 = sc.read_count()
        labels = _ints_from_text(sc.read_list_content())
        if labels.size != n2:
            raise MeshError("faces 紧凑格式的标签数量不符")
        n_faces = offsets.size - 1
        if int(offsets[-1]) != labels.size:
            raise MeshError("faces 紧凑格式的偏移数组与标签数量不符")
        return [labels[offsets[i] : offsets[i + 1]].astype(np.int32) for i in range(n_faces)]
    raise MeshError("无法解析 faces 列表")


def _read_label_list_ascii(sc: "_AsciiScanner", count: int) -> np.ndarray:
    content = sc.read_list_content()
    arr = _ints_from_text(content)
    if arr.size == count:
        return arr
    flat = np.array(content.replace("(", " ").replace(")", " ").split(), dtype=np.int64)
    if flat.size == count:
        return flat
    raise MeshError(f"列表数量不符: 头声明 {count}, 实际 {arr.size}")


# ------------------------------ 二进制 -------------------------------------
def _scan_count_paren_bytes(raw: bytes, pos: int) -> tuple[int, int]:
    n = len(raw)
    while pos < n and raw[pos : pos + 1].isspace():
        pos += 1
    j = pos
    while j < n and 48 <= raw[j] <= 57:
        j += 1
    if j == pos:
        raise MeshError("二进制段: 期望一个 ASCII 整数")
    count = int(raw[pos:j])
    k = raw.index(b"(", j)
    return count, k + 1


def _skip_close_paren(raw: bytes, pos: int) -> int:
    """跳过一段二进制数据后面的 `)` 与空白。"""
    n = len(raw)
    while pos < n and raw[pos : pos + 1].isspace():
        pos += 1
    if pos < n and raw[pos : pos + 1] == b")":
        pos += 1
    return pos


def _detect_label_size(raw: bytes, offset: int, count: int) -> tuple[int, str]:
    for size, dt in ((4, "<i4"), (8, "<i8")):
        if raw[offset + count * size : offset + count * size + 1] == b")":
            return size, dt
    return 4, "<i4"


def _read_binary_labels(raw: bytes, start: int, count: int) -> tuple[np.ndarray, int]:
    size, dt = _detect_label_size(raw, start, count)
    if start + count * size > len(raw):
        raise MeshError("二进制标签数据长度不足")
    arr = np.frombuffer(raw, dtype=dt, count=count, offset=start)
    return arr.astype(np.int64), start + count * size


def _read_binary_points(raw: bytes, start: int, count: int) -> np.ndarray:
    for dt, size in (("<f8", 8), ("<f4", 4)):
        end = start + count * 3 * size
        if end <= len(raw) and raw[end : end + 1] == b")":
            return (
                np.frombuffer(raw, dtype=dt, count=count * 3, offset=start)
                .reshape(count, 3)
                .astype(np.float64)
            )
    raise MeshError("无法识别 points 的二进制数据(float32/float64 均不匹配)")


def _read_mesh_files(mesh_dir: Path) -> tuple[np.ndarray, list[np.ndarray], np.ndarray, np.ndarray, str]:
    result: dict[str, object] = {}
    fmt_all = "ascii"
    for name in ("points", "faces", "owner", "neighbour"):
        raw = (mesh_dir / name).read_bytes()
        header_text, body = _split_header(raw)
        fmt, _cls = _header_info(header_text)
        binary = fmt.startswith("binary")
        if binary:
            fmt_all = "binary"
            count, start = _scan_count_paren_bytes(body, 0)
            if name == "points":
                result[name] = _read_binary_points(body, start, count)
            elif name == "faces":
                offsets, after = _read_binary_labels(body, start, count)
                # 偏移数组之后是一个 ASCII 的 ')' , 然后是标签段的 "个数 + ("
                n2, start2 = _scan_count_paren_bytes(body, _skip_close_paren(body, after))
                labels, _ = _read_binary_labels(body, start2, n2)
                if int(offsets[-1]) != labels.size:
                    raise MeshError("二进制 faces: 偏移数组与标签数量不符")
                result[name] = [
                    labels[offsets[i] : offsets[i + 1]].astype(np.int32)
                    for i in range(len(offsets) - 1)
                ]
            else:
                arr, _ = _read_binary_labels(body, start, count)
                result[name] = arr
        else:
            sc = _AsciiScanner(body.decode("utf-8", errors="replace"))
            count = sc.read_count()
            if name == "points":
                result[name] = _read_points_ascii(sc, count)
            elif name == "faces":
                result[name] = _read_faces_ascii(sc, count)
            else:
                result[name] = _read_label_list_ascii(sc, count)
    points = result["points"]  # type: ignore[assignment]
    faces = result["faces"]  # type: ignore[assignment]
    owner = np.asarray(result["owner"], dtype=np.int32)
    neighbour = np.asarray(result["neighbour"], dtype=np.int32)
    return points, faces, owner, neighbour, fmt_all


# ---------------------------------------------------------------------------
# boundary 文件
# ---------------------------------------------------------------------------
def read_boundary(path: Path, with_body: bool = False):
    """读取 ``polyMesh/boundary``。

    文件正体形如 ``4 ( inlet { ... } outlet { ... } )``, 解析后是一个列表,
    其中补片名是字符串、紧随其后的是该补片的字典。

    ``with_body=True`` 时返回 ``(patches, body)``, ``body`` 是原始字典,
    保留下来是为了补片重命名时能原样写回(不丢失未知条目)。
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    _header, body = dictfile.parse_file(text)

    entries: list[object] | None = None
    for _key, value in body.items:
        if isinstance(value, dictfile.FoamList):
            entries = list(value)
            break
    if entries is None:
        raise MeshError("boundary 文件格式无法识别")

    patches: list[Patch] = []
    pending_name: str | None = None
    for item in entries:
        if isinstance(item, str):
            pending_name = item
            continue
        if isinstance(item, dictfile.FoamDict):
            name = pending_name or f"patch{len(patches)}"
            pending_name = None
            ptype = dictfile.get_atom(item, "type", "patch") or "patch"
            physical = dictfile.get_atom(item, "physicalType", "") or ""
            n_faces = dictfile.as_int(dictfile.get_atom(item, "nFaces", "0"), 0) or 0
            start = dictfile.as_int(dictfile.get_atom(item, "startFace", "0"), 0) or 0
            in_groups = [
                a
                for a in dictfile.atoms(item.get("inGroups"))
            ]
            patches.append(
                Patch(
                    name=name,
                    type=ptype,
                    physical_type=physical,
                    n_faces=n_faces,
                    start_face=start,
                    index=len(patches),
                    in_groups=in_groups,
                )
            )
    if with_body:
        return patches, body
    return patches


def _validate(mesh: PolyMesh) -> None:
    if mesh.n_faces != len(mesh.owner):
        raise MeshError(f"faces({mesh.n_faces}) 与 owner({len(mesh.owner)}) 数量不一致")
    if len(mesh.neighbour) > mesh.n_faces:
        raise MeshError("neighbour 数量大于 faces 数量")
    for p in mesh.patches:
        if p.start_face + p.n_faces > mesh.n_faces:
            raise MeshError(f"补片 {p.name} 的面范围超出网格范围")
    if mesh.n_points and mesh.face_points.size and int(mesh.face_points.max()) >= mesh.n_points:
        raise MeshError("面的点索引超出 points 范围")


def read_polymesh(mesh_dir: str | Path) -> PolyMesh:
    """读取一个 polyMesh 目录(自动识别 ascii / binary)。"""
    mesh_dir = Path(mesh_dir)
    for name in ("points", "faces", "owner", "neighbour", "boundary"):
        if not (mesh_dir / name).exists():
            raise MeshError(f"缺少网格文件: {mesh_dir / name}")
    points, faces, owner, neighbour, fmt = _read_mesh_files(mesh_dir)
    patches, boundary_body = read_boundary(mesh_dir / "boundary", with_body=True)
    mesh = PolyMesh(points, faces, owner, neighbour, patches, fmt=fmt)
    mesh.boundary_body = boundary_body
    mesh.path = mesh_dir
    _validate(mesh)
    return mesh
