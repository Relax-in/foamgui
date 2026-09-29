"""案例自检: 在写出字典之前, 把"OpenFOAM 一跑就报错"的设置问题找出来。

这些都是实测踩过的坑:

* 在 ``type wall`` 的补片上用了 ``symmetry``/``empty``/``wedge`` —— 求解器报
  ``patch type 'wall' not constraint type 'symmetry'``;
* 压力边界全是零梯度/对称(封闭域), 而 ``fvSolution`` 里没有 ``pRefCell``/``pRefValue``
  —— 求解器报 ``Unable to set reference cell for field p``;
* ``momentumTransport`` 里 ``RAS { model laminar; }`` 这种非法组合, 或者选了
  需要 ``k``/``epsilon``/``omega``/``nuTilda`` 的模型但 ``0/`` 里没有这些场;
* ``controlDict`` 里只有老式的 ``application`` 而没有 ``solver``(OpenFOAM 13 不认)。

每个问题都给出"界面上该去哪里改"的提示。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import dictfile
from .dictfile import FoamDict
from .fields import (
    _CONSTRAINT_BC_NAMES,
    _CONSTRAINT_PATCH_BCS,
    LES_MODELS,
    RAS_MODELS,
    TURBULENCE_FIELDS,
)

__all__ = ["Issue", "check_case", "looks_like_solver", "pressure_needs_reference"]


@dataclass
class Issue:
    level: str  # 'error' | 'warning'
    where: str
    message: str
    hint: str = ""

    def text(self) -> str:
        tag = "错误" if self.level == "error" else "警告"
        line = f"[{tag}] {self.where}: {self.message}"
        return line + (f"\n          → {self.hint}" if self.hint else "")


#: 会把压力"钉住"(Dirichlet)的边界条件 —— 有一个就不需要 pRefCell
_PRESSURE_FIXING_BCS = {
    "fixedValue", "totalPressure", "prghTotalPressure", "uniformFixedValue",
    "totalPressureCompressible", "fixedPressure", "prghPressure", "atmosphericPressure",
    "uniformDensityHydrostaticPressure", "fixedMeanOutletInlet",
    "freestreamPressure",              # 外流常用的来流压力(会钉住压力)
    "uniformTotalPressure",
}

#: 不需要线性求解器条目的场(由模型直接算出)
_NO_SOLVER_FIELDS = {"nut", "alphat", "mut", "muEff", "rho", "alphaRhoPhi"}

#: OpenFOAM 13 的求解器模块 / 常见老求解器名
_SOLVER_HINTS = (
    "incompressible", "compressible", "multiphase", "isothermal", "buoyant",
    "multicomponent", "shock", "solid", "potential", "shallow",
)


def looks_like_solver(name: str) -> bool:
    """名字像不像一个 OpenFOAM 求解器(用来识别 ANSA 的 UserSolver 占位名)。"""
    low = (name or "").strip().lower()
    if not low or low in {"usersolver", "none", "unknown", "solver", "application"}:
        return False
    if low.endswith("foam"):
        return True
    return any(low.startswith(p) for p in _SOLVER_HINTS)


def check_case(case) -> list[Issue]:
    """检查整个案例, 返回问题列表(先错误后警告)。"""
    issues: list[Issue] = []
    if case.mesh is not None:
        issues.extend(_check_bc_vs_patch_type(case))
    issues.extend(_check_turbulence(case))
    issues.extend(_check_control_dict(case))
    issues.extend(_check_pressure_reference(case))
    issues.extend(_check_solvers(case))
    issues.sort(key=lambda i: 0 if i.level == "error" else 1)
    return issues


# ---------------------------------------------------------------------------
def _check_bc_vs_patch_type(case) -> list[Issue]:
    """边界条件类型必须和补片的网格类型匹配(约束型边界尤其)。"""
    out: list[Issue] = []
    for field in case.fields.values():
        for patch in case.mesh.patches:
            bc = field.patch_type(patch.name)
            if not bc:
                continue
            allowed = _CONSTRAINT_PATCH_BCS.get(patch.type)
            if allowed is not None:
                if bc not in allowed:
                    out.append(Issue(
                        "error", f"0/{field.name} · {patch.name}",
                        f"网格里这个补片是 {patch.type} 类型, 但边界条件写的是 {bc}",
                        f"{patch.type} 补片只能用 {'/'.join(sorted(allowed))};"
                        " 到『边界条件』页改掉, 或者到『补片』列表把网格类型改对",
                    ))
            elif bc in _CONSTRAINT_BC_NAMES:
                out.append(Issue(
                    "error", f"0/{field.name} · {patch.name}",
                    f"网格里这个补片是 {patch.type} 类型, 不能用约束型边界 {bc}",
                    "约束型边界(empty/wedge/symmetry/cyclic)只能加在网格里同样是"
                    "该类型的补片上; 想要自由滑移可以用 slip",
                ))
    return out


def _check_turbulence(case) -> list[Issue]:
    out: list[Issue] = []
    mt = case.get("constant", "momentumTransport")
    if mt is None:
        return out
    sim = (dictfile.get_atom(mt, "simulationType", "laminar") or "laminar").strip()
    if sim.lower() == "laminar":
        return out
    sub = dictfile.get_dict(mt, sim)
    if sub is None:
        out.append(Issue(
            "error", "constant/momentumTransport",
            f"simulationType 是 {sim}, 但没有 {sim} 子字典",
            "到『求解设置 → 湍流模型』重新选一次",
        ))
        return out
    model = (dictfile.get_atom(sub, "model", "") or "").strip()
    valid = RAS_MODELS if sim == "RAS" else LES_MODELS
    if model not in valid:
        out.append(Issue(
            "error", "constant/momentumTransport",
            f"{sim} {{ model {model}; }} 里的 {model} 不是 {sim} 模型"
            + ("(laminar 不是模型名)" if model.lower() == "laminar" else ""),
            "要算层流请把 simulationType 改成 laminar; 否则在『求解设置 → 湍流模型』"
            "的模型下拉里选一个",
        ))
        return out
    needed = TURBULENCE_FIELDS.get(model, [])
    missing = [n for n in needed if n not in case.fields]
    if missing:
        out.append(Issue(
            "error", "constant/momentumTransport",
            f"模型 {model} 需要场 {'、'.join(needed)}, 但 0/ 里缺少 {'、'.join(missing)}",
            "到『初始条件』页添加这些场, 或者改用 laminar",
        ))
    return out


def _check_control_dict(case) -> list[Issue]:
    out: list[Issue] = []
    cd = case.control_dict()
    solver = (dictfile.get_atom(cd, "solver", "") or "").strip()
    legacy = (dictfile.get_atom(cd, "application", "") or "").strip()
    if not solver:
        out.append(Issue(
            "error", "system/controlDict",
            "没有 solver 条目" + (f"(只有老式的 application {legacy})" if legacy else ""),
            "OpenFOAM 13 必须写 `solver incompressibleFluid;` 这样;"
            "到『求解设置』的求解器栏选一个即可(会自动写成 solver)",
        ))
    elif not looks_like_solver(solver):
        out.append(Issue(
            "warning", "system/controlDict",
            f"solver {solver} 看起来不是标准的求解器模块名",
            "OpenFOAM 13 的模块名形如 incompressibleFluid / fluid / incompressibleVoF",
        ))
    if legacy and solver:
        out.append(Issue(
            "warning", "system/controlDict",
            f"还留着老式的 application {legacy} 条目",
            "OpenFOAM 13 只用 solver, 这条是导出工具留下的占位; 可以手工删掉",
        ))
    return out


def pressure_needs_reference(case) -> bool:
    """压力是否需要 pRefCell/pRefValue: 边界里没有任何"固定压力"的边界条件。

    封闭域(例如四周都是壁面/对称面)的压力方程是纯 Neumann 的, 必须给参考单元;
    有 fixedValue/totalPressure 这类边界时不需要。
    """
    if case.mesh is None or not case.fields:
        return False
    for pname in ("p", "p_rgh"):
        pf = case.fields.get(pname)
        if pf is None:
            continue
        for patch in case.mesh.patches:
            if pf.patch_type(patch.name) in _PRESSURE_FIXING_BCS:
                return False
        return True
    return False


def _check_pressure_reference(case) -> list[Issue]:
    """封闭域(压力边界全是零梯度/对称)必须给压力参考点。"""
    out: list[Issue] = []
    fvs = case.get("system", "fvSolution")
    if fvs is None:
        return out
    simple = dictfile.get_dict(fvs, "SIMPLE") or dictfile.get_dict(fvs, "PIMPLE")
    if simple is None:
        return out
    has_ref = any(k in simple for k in ("pRefCell", "pRefPoint"))
    if has_ref:
        return out
    # 只检查求解器真正使用的那个压力场: VoF/多相/浮力类用 p_rgh, 其余用 p
    cd = case.control_dict()
    solver = (dictfile.get_atom(cd, "solver", "") or dictfile.get_atom(cd, "application", "") or "").lower()
    prefers_rgh = any(k in solver for k in ("vof", "multiphase", "buoyant", "interface", "interfoam"))
    order = ("p_rgh", "p") if prefers_rgh else ("p", "p_rgh")
    for pname in order[:1]:
        pf = case.fields.get(pname)
        if pf is None:
            continue
        fixing = [
            p.name for p in case.mesh.patches
            if pf.patch_type(p.name) in _PRESSURE_FIXING_BCS
        ] if case.mesh else []
        if not fixing:
            out.append(Issue(
                "error", "system/fvSolution",
                f"{pname} 的边界条件里没有固定压力的(fixedValue/totalPressure 等),"
                " 而 SIMPLE 里又没有 pRefCell/pRefValue —— 求解器会报 "
                "\"Unable to set reference cell for field p\"",
                "封闭域必须给压力参考: 到『求解设置 → 线性求解器与松弛』把 pRefCell/pRefValue "
                "填好, 然后点上面的『把当前显示的值写入字典』; 或者把某个边界改成 fixedValue 压力",
            ))
    return out


def _check_solvers(case) -> list[Issue]:
    out: list[Issue] = []
    fvs = case.get("system", "fvSolution")
    if fvs is None:
        return out
    solvers = dictfile.get_dict(fvs, "solvers")
    if solvers is None:
        return out
    keys = set(solvers.keys())
    missing = [
        n for n in case.fields
        if n not in keys and n not in _NO_SOLVER_FIELDS and not n.startswith("nuTilda")
    ]
    if missing:
        out.append(Issue(
            "warning", "system/fvSolution",
            f"solvers 里没有 {'、'.join(missing)} 的设置",
            "求解器会报找不到这些场的线性求解器; 到『求解设置 → 线性求解器与松弛』补上,"
            " 或点『把当前显示的值写入字典』",
        ))
    return out
