"""案例(case)模型: 读取案例目录、持有各字典、生成并写出 OpenFOAM 字典文件。

关键点
------------------------------------------------------------------
所有字典都以 :class:`~foamgui.foam.dictfile.FoamDict` 的形式保存在内存中,
GUI 直接修改这些对象, 写出时再统一序列化。这样:
1. 用户手写但 GUI 不认识的条目会被完整保留;
2. 支持"预览"——写出之前就能看到每个文件的确切内容。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from . import dictfile, fields as fields_mod, polymesh
from .fields import apply_params
from .dictfile import FoamDict

__all__ = ["FieldFile", "FoamCase", "BANNER", "render_file", "CASE_SYSTEM_FILES", "CASE_CONSTANT_FILES"]


BANNER = r"""/*--------------------------------*- C++ -*----------------------------------*\
  =========                 |
  \\      /  F ield         | OpenFOAM: The Open Source CFD Toolbox
   \\    /   O peration     | Website:  https://openfoam.org
    \\  /    A nd           | Version:  13
     \\/     M anipulation  |
\*---------------------------------------------------------------------------*/
"""

CLOSING = "// ************************************************************************* //\n"

# (文件名, class)
CASE_SYSTEM_FILES: list[tuple[str, str]] = [
    ("controlDict", "dictionary"),
    ("fvSchemes", "dictionary"),
    ("fvSolution", "dictionary"),
]
CASE_CONSTANT_FILES: list[tuple[str, str]] = [
    ("momentumTransport", "dictionary"),
    ("physicalProperties", "dictionary"),
    ("transportProperties", "dictionary"),
    ("thermophysicalProperties", "dictionary"),
    ("g", "uniformDimensionedVectorField"),
]


def render_file(
    body: FoamDict,
    cls: str,
    obj: str,
    location: str = "",
    fmt: str = "ascii",
    note: str | None = None,
) -> str:
    """把字典渲染成完整的 OpenFOAM 文件文本。"""
    header = BANNER + "FoamFile\n{\n"
    header += f"    format      {fmt};\n"
    header += f"    class       {cls};\n"
    if location:
        header += f'    location    "{location}";\n'
    header += f"    object      {obj};\n"
    header += "}\n"
    header += "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"
    if note:
        header += note.rstrip() + "\n\n"
    body_text = dictfile.format_body(body, 0)
    return header + body_text + "\n\n" + CLOSING


def render_boundary_file(body: FoamDict) -> str:
    """渲染 ``constant/polyMesh/boundary``。

    结构与普通字典不同, 正体是 ``N ( 名字 { ... } 名字 { ... } )``,
    所以单独写一个渲染函数(不要末尾的分号)。
    """
    header = BANNER + "FoamFile\n{\n"
    header += "    format      ascii;\n"
    header += "    class       polyBoundaryMesh;\n"
    header += '    location    "constant/polyMesh";\n'
    header += "    object      boundary;\n"
    header += "}\n"
    header += "// * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * * //\n\n"

    lines: list[str] = []
    if body.items:
        count, lst = body.items[0]
        lines.append(str(count))
        lines.append("(")
        items = list(lst)
        i = 0
        while i + 1 < len(items):
            name = items[i]
            d = items[i + 1]
            lines.append("    " + str(name))
            if isinstance(d, FoamDict):
                lines.append(dictfile.format_dict(d, 4))
            else:
                lines.append("    " + dictfile.format_value(d, 4))
            i += 2
        lines.append(")")
    return header + "\n".join(lines) + "\n\n" + CLOSING


@dataclass
class FieldFile:
    """``0/`` 下的一个场文件。"""

    name: str
    path: Path | None
    time_dir: str
    header: FoamDict
    body: FoamDict
    cls: str = "volScalarField"
    kind: str = "scalar"
    category: str = "scalar"
    is_new: bool = False

    @property
    def dimensions(self) -> list[str]:
        v = self.body.get("dimensions")
        if isinstance(v, dictfile.Dimensioned):
            return v.dims
        return dictfile.atoms(v)

    @property
    def internal(self) -> tuple[str, list[str]]:
        return fields_mod.internal_field_value(self.body)

    def patch_dict(self, patch: str, create: bool = True) -> FoamDict | None:
        bf = self.body.get("boundaryField")
        if not isinstance(bf, FoamDict):
            if not create:
                return None
            bf = FoamDict()
            self.body.set("boundaryField", bf)
        d = bf.get(patch)
        if not isinstance(d, FoamDict):
            if not create:
                return None
            d = FoamDict()
            d.set("type", "zeroGradient")
            bf.set(patch, d)
        return d

    def patch_type(self, patch: str) -> str:
        d = self.patch_dict(patch, create=False)
        if d is None:
            return ""
        return dictfile.get_atom(d, "type", "") or ""

    def render(self) -> str:
        loc = self.time_dir
        return render_file(self.body, self.cls, self.name, loc)


class FoamCase:
    """一个 OpenFOAM 案例。"""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.mesh: polymesh.PolyMesh | None = None
        self.mesh_dir: Path | None = None
        self.time_dir: str = "0"
        self.fields: dict[str, FieldFile] = {}
        self.system: dict[str, tuple[FoamDict, str]] = {}  # name -> (dict, class)
        self.constant: dict[str, tuple[FoamDict, str]] = {}
        self.warnings: list[str] = []
        self._loaded = False
        # 网格(boundary)是否被改过(例如补片重命名), 改过才需要写出 boundary 文件
        self.mesh_modified = False

    # -- 补片操作 -----------------------------------------------------------
    def rename_patch(self, old: str, new: str) -> list[str]:
        """重命名补片: 同步改网格 boundary 与所有场的 boundaryField。

        返回被改动的场名列表。只改名字, 不动几何, 所以对求解没有影响。
        """
        new = (new or "").strip()
        if not new:
            raise ValueError("补片名不能为空")
        if self.mesh is None:
            raise ValueError("尚未读取网格")
        patch = self.mesh.patch_by_name(old)
        if patch is None:
            raise ValueError(f"找不到补片 {old}")
        if new == old:
            return []
        if self.mesh.patch_by_name(new) is not None:
            raise ValueError(f"补片名 {new} 已存在")

        patch.name = new
        # 1) 网格 boundary 文件里的名字
        body = self.mesh.boundary_body
        if body is not None and body.items:
            lst = body.items[0][1]
            for i, item in enumerate(lst):
                if isinstance(item, str) and item == old:
                    lst[i] = new
                    break
        # 2) 每个场的 boundaryField 键(保持原有顺序与内容)
        touched: list[str] = []
        for name, ff in self.fields.items():
            bf = ff.body.get("boundaryField")
            if not isinstance(bf, FoamDict) or old not in bf:
                continue
            rebuilt = FoamDict()
            for k, v in bf.items:
                rebuilt.set(new if k == old else k, v)
            ff.body.set("boundaryField", rebuilt)
            touched.append(name)
        self.mesh_modified = True
        return touched

    # -- 基本信息 -----------------------------------------------------------
    @property
    def name(self) -> str:
        return self.root.name

    def exists(self) -> bool:
        return self.root.is_dir()

    # -- 读取 ---------------------------------------------------------------
    def load(self) -> None:
        """读取案例(不含网格, 网格请调用 :meth:`load_mesh`)。"""
        self.warnings = []
        if not self.root.is_dir():
            raise FileNotFoundError(f"案例目录不存在: {self.root}")

        # system / constant
        for fname, cls in CASE_SYSTEM_FILES:
            p = self.root / "system" / fname
            d, c = self._read_dict(p, cls)
            if d is not None:
                self.system[fname] = (d, c)
        for fname, cls in CASE_CONSTANT_FILES:
            p = self.root / "constant" / fname
            d, c = self._read_dict(p, cls)
            if d is not None:
                self.constant[fname] = (d, c)

        self.time_dir = self._detect_time_dir()
        self.fields = self._load_fields(self.time_dir)
        self._ensure_system_defaults()
        self._loaded = True

    def _read_dict(self, path: Path, default_cls: str) -> tuple[FoamDict | None, str]:
        if not path.exists():
            return None, default_cls
        try:
            header, body = dictfile.parse_file(path.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:  # pragma: no cover
            self.warnings.append(f"解析 {path.name} 失败: {exc}")
            return None, default_cls
        cls = dictfile.get_atom(header, "class", default_cls) or default_cls
        return body, cls

    def _detect_time_dir(self) -> str:
        """优先用 controlDict 里的 startTime, 否则找 0 / 0.orig / 最小数字目录。"""
        cd = self.system.get("controlDict", (None, ""))[0]
        start = dictfile.get_atom(cd, "startTime", None) if cd else None
        if start:
            try:
                start = str(int(float(start)))
            except ValueError:
                pass
            if (self.root / start).is_dir() and self._looks_like_time_dir(self.root / start):
                return start
        for cand in ("0", "0.orig"):
            if (self.root / cand).is_dir():
                if self._looks_like_time_dir(self.root / cand) or not (self.root / "0").is_dir():
                    return cand
        numeric = []
        for p in self.root.iterdir():
            if p.is_dir():
                try:
                    numeric.append((float(p.name), p.name))
                except ValueError:
                    continue
        if numeric:
            return min(numeric)[1]
        return "0"

    @staticmethod
    def _looks_like_time_dir(p: Path) -> bool:
        for f in p.iterdir():
            if f.is_file():
                try:
                    head = f.open("r", encoding="utf-8", errors="replace").read(600)
                except OSError:
                    continue
                if "volScalarField" in head or "volVectorField" in head:
                    return True
        return False

    def _load_fields(self, time_dir: str) -> dict[str, FieldFile]:
        out: dict[str, FieldFile] = {}
        d = self.root / time_dir
        if not d.is_dir():
            return out
        for p in sorted(d.iterdir()):
            if not p.is_file():
                continue
            if p.name.startswith(".") or p.suffix in (".orig", ".bak"):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "FoamFile" not in text[:400] and "FoamFile" not in text[:2000]:
                continue
            try:
                header, body = dictfile.parse_file(text)
            except Exception as exc:
                self.warnings.append(f"跳过 {p.name}: 解析失败({exc})")
                continue
            cls = dictfile.get_atom(header, "class", "") or ""
            if not cls.startswith(("vol", "point", "surface")):
                continue
            kind = fields_mod.field_kind_of_class(cls)
            name = dictfile.get_atom(header, "object", p.name) or p.name
            out[name] = FieldFile(
                name=name,
                path=p,
                time_dir=time_dir,
                header=header,
                body=body,
                cls=cls,
                kind=kind,
                category=fields_mod.category_of(name, cls),
            )
        return out

    # -- 默认字典 -----------------------------------------------------------
    def _ensure_system_defaults(self) -> None:
        """缺少 system/constant 文件时给出 OpenFOAM 13 的合理默认值。"""
        if "controlDict" not in self.system:
            self.system["controlDict"] = (_default_control_dict(), "dictionary")
            self.warnings.append("未找到 system/controlDict, 已使用默认值")
        if "fvSchemes" not in self.system:
            self.system["fvSchemes"] = (_default_fv_schemes(), "dictionary")
            self.warnings.append("未找到 system/fvSchemes, 已使用默认值")
        if "fvSolution" not in self.system:
            self.system["fvSolution"] = (_default_fv_solution(self.field_categories()), "dictionary")
            self.warnings.append("未找到 system/fvSolution, 已使用默认值")
        if "momentumTransport" not in self.constant:
            self.constant["momentumTransport"] = (
                _default_momentum_transport(self.fields),
                "dictionary",
            )
            sim = dictfile.get_atom(self.constant["momentumTransport"][0], "simulationType", "")
            if sim:
                self.warnings.append(f"未找到 constant/momentumTransport, 已按案例里的场选择 {sim}")
        if "physicalProperties" not in self.constant:
            self.constant["physicalProperties"] = (_default_physical_properties(), "dictionary")

    def field_categories(self) -> dict[str, tuple[str, str]]:
        """``{场名: (类别, 类型kind)}``。"""
        return {n: (f.category, f.kind) for n, f in self.fields.items()}

    # -- 网格 ---------------------------------------------------------------
    def load_mesh(self) -> polymesh.PolyMesh:
        if self.mesh is None:
            self.mesh_dir = polymesh.find_polymesh_dir(self.root)
            self.mesh = polymesh.read_polymesh(self.mesh_dir)
        return self.mesh

    @property
    def patch_names(self) -> list[str]:
        return self.mesh.patch_names if self.mesh else []

    def patch_info(self) -> list[tuple[str, str, int]]:
        """``[(补片名, 类型, 面数)]``。"""
        if not self.mesh:
            return []
        return [(p.name, p.type, p.n_faces) for p in self.mesh.patches]

    # -- 场操作 -------------------------------------------------------------
    def add_field(self, name: str) -> FieldFile:
        spec = fields_mod.FIELD_CATALOG.get(name)
        if spec is None:
            spec = fields_mod.FieldSpec(name, "scalar", "volScalarField", "[0 0 0 0 0 0 0]", "scalar", name)
        body = fields_mod.make_field_dict(spec, self.patch_names)
        # 如果是已知场, 用推荐 BC 初始化
        if self.mesh:
            for p in self.mesh.patches:
                bc, params = fields_mod.recommend_bc(spec.category, name, p.name, p.type)
                d = body.get("boundaryField").get(p.name)
                d.set("type", bc)
                apply_params(d, spec.kind, params)
        ff = FieldFile(
            name=name,
            path=None,
            time_dir=self.time_dir,
            header=FoamDict(),
            body=body,
            cls=spec.cls,
            kind=spec.kind,
            category=spec.category,
            is_new=True,
        )
        self.fields[name] = ff
        return ff

    def remove_field(self, name: str) -> None:
        self.fields.pop(name, None)

    def sync_patches(self) -> list[str]:
        """让每个场的 boundaryField 与网格补片保持一致, 返回新增的补片名。"""
        added: list[str] = []
        for ff in self.fields.values():
            bf = ff.body.get("boundaryField")
            if not isinstance(bf, FoamDict):
                bf = FoamDict()
                ff.body.set("boundaryField", bf)
            for pname, ptype, _n in self.patch_info():
                if pname in bf:
                    continue
                d = FoamDict()
                bc, params = fields_mod.recommend_bc(ff.category, ff.name, pname, ptype)
                d.set("type", bc)
                apply_params(d, ff.kind, params)
                bf.set(pname, d)
                added.append(f"{ff.name}/{pname}")
        return added

    # -- 输出 ---------------------------------------------------------------
    def render_all(self) -> dict[str, str]:
        """渲染所有将要写出的文件, 返回 ``{相对路径: 文本}``。"""
        out: dict[str, str] = {}
        for name, ff in self.fields.items():
            out[f"{self.time_dir}/{name}"] = ff.render()
        for fname, (body, cls) in self.system.items():
            out[f"system/{fname}"] = render_file(body, cls, fname, "system")
        for fname, (body, cls) in self.constant.items():
            out[f"constant/{fname}"] = render_file(body, cls, fname, "constant")
        if self.mesh_modified and self.mesh is not None and self.mesh.boundary_body is not None:
            out["constant/polyMesh/boundary"] = render_boundary_file(self.mesh.boundary_body)
        return out

    def write(
        self,
        out_dir: str | Path | None = None,
        backup: bool = True,
        only_changed: bool = False,
    ) -> tuple[list[Path], Path | None]:
        """写出所有字典文件。

        返回 ``(写出的文件列表, 备份目录)``。
        """
        target = Path(out_dir).expanduser().resolve() if out_dir else self.root
        rendered = self.render_all()
        backup_dir: Path | None = None
        written: list[Path] = []
        if backup and target == self.root:
            backup_dir = self._make_backup()
        for rel, text in rendered.items():
            dst = target / rel
            if only_changed and dst.exists() and dst.read_text(encoding="utf-8", errors="replace") == text:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_text(text, encoding="utf-8")
            written.append(dst)
        return written, backup_dir

    def _make_backup(self) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        backup_dir = self.root / "foamgui_backup" / stamp
        files = list(self.system.keys()) + list(self.constant.keys())
        for fname in files:
            for sub in ("system", "constant"):
                src = self.root / sub / fname
                if src.exists():
                    dst = backup_dir / sub / fname
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        for name, ff in self.fields.items():
            if ff.path and ff.path.exists():
                dst = backup_dir / ff.time_dir / name
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ff.path, dst)
        return backup_dir

    # -- 便捷访问 -----------------------------------------------------------
    def get(self, group: str, name: str) -> FoamDict | None:
        table = self.system if group == "system" else self.constant
        item = table.get(name)
        return item[0] if item else None

    def control_dict(self) -> FoamDict:
        return self.system["controlDict"][0]

    def set_dict(self, group: str, name: str, body: FoamDict, cls: str = "dictionary") -> None:
        table = self.system if group == "system" else self.constant
        table[name] = (body, cls)


# ---------------------------------------------------------------------------
# 默认字典(OpenFOAM 13 / foamRun incompressibleFluid)
# ---------------------------------------------------------------------------
def _default_control_dict() -> FoamDict:
    d = FoamDict()
    d.set("solver", "incompressibleFluid")
    for k, v in [
        ("startFrom", "startTime"),
        ("startTime", "0"),
        ("stopAt", "endTime"),
        ("endTime", "500"),
        ("deltaT", "1"),
        ("writeControl", "timeStep"),
        ("writeInterval", "50"),
        ("purgeWrite", "0"),
        ("writeFormat", "ascii"),
        ("writePrecision", "6"),
        ("writeCompression", "off"),
        ("timeFormat", "general"),
        ("timePrecision", "6"),
        ("runTimeModifiable", "true"),
    ]:
        d.set(k, v)
    return d


def _default_fv_schemes() -> FoamDict:
    d = FoamDict()
    d.set("ddtSchemes", FoamDict([("default", "steadyState")]))
    d.set("gradSchemes", FoamDict([("default", "Gauss linear")]))
    div = FoamDict()
    div.set("default", "none")
    div.set("div(phi,U)", "bounded Gauss linearUpwind grad(U)")
    div.set("div((nuEff*dev2(T(grad(U)))))", "Gauss linear")
    d.set("divSchemes", div)
    d.set("laplacianSchemes", FoamDict([("default", "Gauss linear corrected")]))
    d.set("interpolationSchemes", FoamDict([("default", "linear")]))
    d.set("snGradSchemes", FoamDict([("default", "corrected")]))
    d.set("wallDist", FoamDict([("method", "meshWave")]))
    return d


def _default_fv_solution(categories: dict[str, tuple[str, str]]) -> FoamDict:
    d = FoamDict()
    solvers = FoamDict()
    for name, (cat, _kind) in categories.items():
        s = FoamDict()
        if name == "p" or name.startswith("p"):
            s.set("solver", "GAMG")
            s.set("smoother", "GaussSeidel")
            s.set("tolerance", "1e-06")
            s.set("relTol", "0.1")
        else:
            s.set("solver", "smoothSolver")
            s.set("smoother", "GaussSeidel")
            s.set("nSweeps", "2")
            s.set("tolerance", "1e-08")
            s.set("relTol", "0.1")
        solvers.set(name, s)
    d.set("solvers", solvers)
    simple = FoamDict()
    simple.set("nNonOrthogonalCorrectors", "0")
    simple.set("consistent", "yes")
    d.set("SIMPLE", simple)
    relax = FoamDict()
    f = FoamDict()
    e = FoamDict()
    for name, (cat, _kind) in categories.items():
        if name == "p" or name.startswith("p"):
            f.set(name, "0.3")
        else:
            e.set(name, "0.7")
    relax.set("fields", f)
    relax.set("equations", e)
    d.set("relaxationFactors", relax)
    return d


def _default_momentum_transport(fields: dict | None = None) -> FoamDict:
    """给缺少 constant/momentumTransport 的案例一个合理默认。

    关键: **按案例里实际存在的湍流场来选模型**。如果案例里没有 nuTilda/k/omega
    这些场, 就选 laminar —— 否则求解器会因为找不到模型需要的场而报错。
    """
    names = set(fields or {})
    d = FoamDict()
    if "nuTilda" in names:
        d.set("simulationType", "RAS")
        sub = FoamDict()
        sub.set("model", "SpalartAllmaras")
        d.set("RAS", sub)
    elif "epsilon" in names and "k" in names:
        d.set("simulationType", "RAS")
        sub = FoamDict()
        sub.set("model", "kEpsilon")
        d.set("RAS", sub)
    elif "omega" in names and "k" in names:
        d.set("simulationType", "RAS")
        sub = FoamDict()
        sub.set("model", "kOmegaSST")
        d.set("RAS", sub)
    else:
        d.set("simulationType", "laminar")
    return d


def _default_physical_properties() -> FoamDict:
    d = FoamDict()
    d.set("viscosityModel", "constant")
    d.set("rho", dictfile.Dimensioned(["1", "-3", "0", "0", "0", "0", "0"], "1"))
    d.set("nu", dictfile.Dimensioned(["0", "2", "-1", "0", "0", "0", "0"], "1e-05"))
    return d
