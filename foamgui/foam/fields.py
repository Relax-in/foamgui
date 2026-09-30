"""场(field)与边界条件(BC)的模型: 常见场的目录、BC 类型目录、参数读写辅助。

设计原则
------------------------------------------------------------------
* 场的"值"以 OpenFOAM 的写法为准, 例如 ``uniform (25.75 3.62 0)``、
  ``$internalField``、``uniform 0.14``;
* BC 类型目录只是给 GUI 提供"下拉框 + 参数表单", 用户也可以直接编辑
  原始文本, 因此目录不需要覆盖 OpenFOAM 的全部类型。
"""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

from . import dictfile
from .dictfile import Compound, FoamDict, FoamList

__all__ = [
    "RAS_MODELS",
    "_CONSTRAINT_BC_NAMES",
    "_CONSTRAINT_PATCH_BCS",
    "LES_MODELS",
    "FieldSpec",
    "ParamSpec",
    "BCType",
    "FIELD_CATALOG",
    "make_field_dict",
    "category_of",
    "bc_types_for",
    "find_bc_type",
    "recommend_bc",
    "recommend_all",
    "get_param",
    "set_param_scalar",
    "set_param_vector",
    "set_param_word",
    "set_param_bool",
    "set_param_macro",
    "param_summary",
    "internal_field_value",
    "set_internal_field",
    "field_kind_of_class",
]


# ---------------------------------------------------------------------------
# 场的目录
# ---------------------------------------------------------------------------
@dataclass
class FieldSpec:
    name: str
    kind: str  # 'scalar' | 'vector' | 'tensor'
    cls: str
    dimensions: str
    category: str  # 'velocity' | 'pressure' | 'turbulence' | 'scalar'
    description: str = ""
    #: 新建这个场时的 internalField 默认值。
    #  湍流场必须给非零值: k=0 或 omega=0 会让 kOmegaSST 之类的模型除零直接崩
    internal: str = ""


FIELD_CATALOG: dict[str, FieldSpec] = {
    "U": FieldSpec("U", "vector", "volVectorField", "[0 1 -1 0 0 0 0]", "velocity", "速度"),
    "p": FieldSpec("p", "scalar", "volScalarField", "[0 2 -2 0 0 0 0]", "pressure", "运动学压力 p/rho"),
    "p_rgh": FieldSpec("p_rgh", "scalar", "volScalarField", "[0 2 -2 0 0 0 0]", "pressure", "扣除静压的压力"),
    "k": FieldSpec("k", "scalar", "volScalarField", "[0 2 -2 0 0 0 0]", "turbulence", "湍动能 k", "0.1"),
    "epsilon": FieldSpec("epsilon", "scalar", "volScalarField", "[0 2 -3 0 0 0 0]", "turbulence",
                         "湍流耗散率 ε", "0.01"),
    "omega": FieldSpec("omega", "scalar", "volScalarField", "[0 0 -1 0 0 0 0]", "turbulence", "比耗散率 ω", "1"),
    "nut": FieldSpec("nut", "scalar", "volScalarField", "[0 2 -1 0 0 0 0]", "turbulence", "湍流运动粘度 nut", "0"),
    "nuTilda": FieldSpec("nuTilda", "scalar", "volScalarField", "[0 2 -1 0 0 0 0]", "turbulence",
                         "SA 模型工作变量", "0"),
    "alphat": FieldSpec("alphat", "scalar", "volScalarField", "[1 -1 -1 0 0 0 0]", "turbulence",
                        "湍流热扩散率", "0"),
    "T": FieldSpec("T", "scalar", "volScalarField", "[0 0 0 1 0 0 0]", "scalar", "温度 T", "300"),
}

#: 常见 RAS / LES 模型名(界面下拉 + 自检都要用)
RAS_MODELS = [
    "SpalartAllmaras", "kEpsilon", "kOmega", "kOmegaSST", "realizableKE",
    "LaunderSharmaKE", "kkLOmega", "v2f", "kOmegaSSTLM", "kOmegaSSTSAS", "qZeta",
]
LES_MODELS = [
    "Smagorinsky", "kEqn", "WALE", "dynamicKEqn", "SpalartAllmarasDES",
    "kOmegaSSTDES", "dynamicLagrangian", "DeardorffDiffStress",
]

# 湍流模型 -> 需要哪些场(用于"添加场"提示)
TURBULENCE_FIELDS = {
    "SpalartAllmaras": ["nuTilda", "nut"],
    "kEpsilon": ["k", "epsilon", "nut"],
    "kOmega": ["k", "omega", "nut"],
    "kOmegaSST": ["k", "omega", "nut"],
    "realizableKE": ["k", "epsilon", "nut"],
    "laminar": [],
}

_CLASS_TO_KIND = {
    "volScalarField": "scalar",
    "volVectorField": "vector",
    "volTensorField": "tensor",
    "volSymmTensorField": "tensor",
    "volSphericalTensorField": "tensor",
    "pointScalarField": "scalar",
    "pointVectorField": "vector",
    "surfaceScalarField": "scalar",
}


def field_kind_of_class(cls: str) -> str:
    return _CLASS_TO_KIND.get(cls, "scalar")


def category_of(name: str, cls: str = "") -> str:
    """根据场名推断类别(决定用哪套 BC 目录)。"""
    spec = FIELD_CATALOG.get(name)
    if spec:
        return spec.category
    low = name.lower()
    if low in ("u", "v", "w") or low.startswith("u"):
        return "velocity"
    if low.startswith("p"):
        return "pressure"
    if low in ("k", "epsilon", "omega", "nut", "nutilda", "nuTilda".lower(), "alphat", "nu_sgs", "ksgs"):
        return "turbulence"
    return "scalar"


def make_field_dict(spec: FieldSpec, patches: list[str], locations: list[str] | None = None) -> FoamDict:
    """新建一个场文件的内容(internalField + 各补片的占位 BC)。"""
    body = FoamDict()
    body.set("dimensions", dictfile.Dimensioned(spec.dimensions.strip("[]").split(), Compound([])))
    if spec.internal:
        default = spec.internal.split()
    else:
        default = ["0"] if spec.kind == "scalar" else ["0", "0", "0"]
    body.set("internalField", dictfile.make_uniform(default))
    bf = FoamDict()
    for name in patches:
        d = FoamDict()
        d.set("type", "zeroGradient")
        bf.set(name, d)
    body.set("boundaryField", bf)
    return body


# ---------------------------------------------------------------------------
# 边界条件类型目录
# ---------------------------------------------------------------------------
@dataclass
class ParamSpec:
    key: str
    label: str
    kind: str = "scalar"  # scalar | vector | word | bool | int
    default: Any = None
    uniform: bool = True  # 是否写成 "uniform <值>"
    choices: list[str] = dc_field(default_factory=list)
    optional: bool = False
    help: str = ""


@dataclass
class BCType:
    name: str
    params: list[ParamSpec] = dc_field(default_factory=list)
    note: str = ""
    patches: str = "all"  # all | wall | nonwall


VALUE = ParamSpec("value", "场值", "auto", None, True, help="边界上的固定值")
VALUE_OPT = ParamSpec("value", "场值", "auto", None, True, optional=True)

# ---- 速度 ---------------------------------------------------------------
_U_TYPES: list[BCType] = [
    BCType("fixedValue", [VALUE], "固定速度"),
    BCType("noSlip", [], "无滑移(壁面)", "wall"),
    BCType("slip", [], "自由滑移", "wall"),
    BCType("zeroGradient", [], "零梯度"),
    BCType("freestreamVelocity", [ParamSpec("freestreamValue", "来流速度", "auto", "$internalField", False)],
           "来流速度(airFoil2D 默认)"),
    BCType("pressureInletOutletVelocity", [VALUE_OPT,
           ParamSpec("tangentialVelocity", "切向速度", "vector", None, True, optional=True)],
           "压力出口/入口速度"),
    BCType("inletOutlet", [ParamSpec("inletValue", "回流值", "auto", "$internalField", False), VALUE_OPT],
           "出流/回流"),
    BCType("movingWallVelocity", [VALUE], "运动壁面", "wall"),
    BCType("flowRateInletVelocity",
           [ParamSpec("volumetricFlowRate", "体积流量", "scalar", "0.1", False, optional=True),
            ParamSpec("massFlowRate", "质量流量", "scalar", None, False, optional=True),
            ParamSpec("meanVelocity", "平均速度", "scalar", None, False, optional=True)],
           "给定流量入口"),
    BCType("symmetry", [], "对称面"),
    BCType("empty", [], "空(2D 前后平面)"),
    BCType("wedge", [], "楔形(轴对称 2D)"),
    BCType("fixedNormalSlip", [ParamSpec("n", "法向", "vector", ["0", "0", "1"], True)], "固定法向滑移"),
    BCType("uniformFixedValue", [ParamSpec("uniformValue", "值", "auto", None, True)], "均匀时变值"),
    BCType("cylindricalInletVelocity",
           [ParamSpec("axis", "轴", "vector", ["0", "0", "1"], True),
            ParamSpec("centre", "中心", "vector", ["0", "0", "0"], True),
            ParamSpec("rpm", "转速", "scalar", "0", False, optional=True),
            ParamSpec("axialVelocity", "轴向速度", "scalar", "0", False, optional=True)],
           "柱坐标入口"),
]

# ---- 压力 ---------------------------------------------------------------
_P_TYPES: list[BCType] = [
    BCType("zeroGradient", [], "零梯度"),
    BCType("fixedValue", [VALUE], "固定压力"),
    BCType("freestreamPressure", [ParamSpec("freestreamValue", "来流压力", "auto", "$internalField", False)],
           "来流压力(airFoil2D 默认)"),
    BCType("totalPressure", [ParamSpec("p0", "总压", "scalar", "0", True)], "总压"),
    BCType("fixedFluxPressure", [VALUE_OPT], "固定通量(壁面常用)", "nonwall"),
    BCType("inletOutlet", [ParamSpec("inletValue", "回流值", "auto", "$internalField", False), VALUE_OPT], "出流/回流"),
    BCType("uniformFixedValue", [ParamSpec("uniformValue", "值", "auto", None, True)], "均匀时变值"),
    BCType("symmetry", [], "对称面"),
    BCType("empty", [], "空(2D 前后平面)"),
    BCType("wedge", [], "楔形(轴对称 2D)"),
]

# ---- 湍流量 -------------------------------------------------------------
_TURB_COMMON: list[BCType] = [
    BCType("fixedValue", [VALUE], "固定值"),
    BCType("zeroGradient", [], "零梯度"),
    BCType("inletOutlet", [ParamSpec("inletValue", "回流值", "auto", "$internalField", False), VALUE_OPT], "出流/回流"),
    BCType("freestream", [ParamSpec("freestreamValue", "来流值", "auto", "$internalField", False)], "来流值(airFoil2D 默认)"),
    BCType("calculated", [], "由模型计算得出"),
    BCType("symmetry", [], "对称面"),
    BCType("empty", [], "空(2D 前后平面)"),
    BCType("wedge", [], "楔形(轴对称 2D)"),
]

_TURB_WALL: list[BCType] = [
    BCType("nutkWallFunction", [VALUE], "nut 壁面函数(k-ε/k-ω)", "wall"),
    BCType("nutUSpaldingWallFunction", [VALUE], "nut Spalding 壁面函数(SA, airFoil2D 默认)", "wall"),
    BCType("nutUWallFunction", [VALUE], "nut 壁面函数", "wall"),
    BCType("kqRWallFunction", [VALUE], "k 壁面函数", "wall"),
    BCType("epsilonWallFunction", [VALUE], "ε 壁面函数", "wall"),
    BCType("omegaWallFunction", [VALUE], "ω 壁面函数", "wall"),
    BCType("alphatWallFunction", [VALUE], "alphat 壁面函数", "wall"),
]

# ---- 一般标量 -----------------------------------------------------------
_SCALAR_TYPES: list[BCType] = [
    BCType("fixedValue", [VALUE], "固定值"),
    BCType("zeroGradient", [], "零梯度"),
    BCType("inletOutlet", [ParamSpec("inletValue", "回流值", "auto", "$internalField", False), VALUE_OPT], "出流/回流"),
    BCType("totalTemperature", [ParamSpec("T0", "总温", "scalar", "300", True)], "总温"),
    BCType("uniformFixedValue", [ParamSpec("uniformValue", "值", "auto", None, True)], "均匀时变值"),
    BCType("symmetry", [], "对称面"),
    BCType("empty", [], "空(2D 前后平面)"),
    BCType("wedge", [], "楔形(轴对称 2D)"),
]

_CATALOG: dict[str, list[BCType]] = {
    "velocity": _U_TYPES,
    "pressure": _P_TYPES,
    "turbulence": _TURB_COMMON + _TURB_WALL,
    "scalar": _SCALAR_TYPES,
}


#: 网格里的"约束型"补片类型 -> 只能用的边界条件类型。
#  OpenFOAM 会检查 "patch type 'x' not constraint type 'y'": 约束型边界
#  (empty/wedge/symmetry/...) 只能加在网格里同样是该类型的补片上。
_CONSTRAINT_PATCH_BCS: dict[str, set[str]] = {
    "empty": {"empty"},
    "wedge": {"wedge"},
    "symmetry": {"symmetry"},
    "symmetryPlane": {"symmetryPlane"},
    "cyclic": {"cyclic"},
    "cyclicAMI": {"cyclicAMI"},
    "nonConformalCyclic": {"nonConformalCyclic"},
    "processor": {"processor"},
    "processorCyclic": {"processorCyclic"},
}
#: 所有约束型边界条件(不能用在 wall/patch 上)
_CONSTRAINT_BC_NAMES = {
    "empty", "wedge", "symmetry", "symmetryPlane", "cyclic", "cyclicAMI",
    "nonConformalCyclic", "processor", "processorCyclic", "jumpCyclic",
}


def bc_types_for(category: str, patch_type: str = "patch") -> list[BCType]:
    """按补片的**网格类型**过滤可用的边界条件类型。

    规则(和 OpenFOAM 的约束一致):
    * 网格是 empty/wedge/symmetry/cyclic 这类约束型补片 -> 只能用同名的约束型边界;
    * 网格是 wall/patch -> **不能**用 empty/wedge/symmetry 这些约束型边界
      (否则求解器会报 "patch type 'wall' not constraint type 'symmetry'")。
    """
    types = list(_CATALOG.get(category, _SCALAR_TYPES))
    allowed = _CONSTRAINT_PATCH_BCS.get(patch_type)
    if allowed is not None:
        return [t for t in types if t.name in allowed]
    # 非约束型补片: 先把所有约束型边界排除掉
    types = [t for t in types if t.name not in _CONSTRAINT_BC_NAMES]
    if patch_type == "wall":
        return [t for t in types if t.patches in ("all", "wall")] + [
            t for t in types if t.patches == "nonwall" and t.name in ("fixedFluxPressure",)
        ]
    return [t for t in types if t.patches in ("all", "nonwall")]


def find_bc_type(category: str, name: str) -> BCType | None:
    for t in _CATALOG.get(category, []):
        if t.name == name:
            return t
    # 未知类型: 给出一个"通用"壳子, 用户可自行添加参数
    return None


# ---------------------------------------------------------------------------
# 推荐边界条件
# ---------------------------------------------------------------------------
_INLET_HINTS = ("inlet", "in", "inflow", "entry")
_OUTLET_HINTS = ("outlet", "out", "outflow", "exit", "pressureoutlet")
#: 外流边界(远场/来流): 用 freestreamVelocity/freestreamPressure/freestream
_FARFIELD_HINTS = ("farfield", "far", "freestream", "atm", "atmosphere", "free", "external")
#: 壁面类: 名字里带这些词就按壁面给 noSlip(而不是靠 type wall, 因为有的工具全导成 wall)
_WALL_HINTS = (
    "wall", "walls", "foil", "blade", "wing", "hub", "shroud", "surface", "body",
    "top", "bottom", "side", "left", "right", "ceiling", "ground", "floor",
)
#: 运动壁面(例如 cavity 的 movingWall / 顶盖驱动): 速度要用 movingWallVelocity
_MOVING_WALL_HINTS = ("moving", "lid", "sliding", "rotating", "belt")


def _tokens(name: str) -> list[str]:
    """把补片名切成词: 支持下划线/点/中划线/驼峰(Patch.InletWall -> patch, inlet, wall)。"""
    import re

    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def _hit(tokens: list[str], hints: tuple[str, ...]) -> bool:
    return any(t == h or t.startswith(h) or t.endswith(h) for t in tokens for h in hints)


def _classify_patch(name: str, ptype: str) -> str:
    """按"类型 + 名字"判断补片属于哪一类。

    名字比类型更可靠: 有的前处理工具(例如 ANSA)导出时把所有补片都写成
    ``type wall``, 但名字仍然叫 ``Block.inlet``、``Block.outlet``。
    """
    toks = _tokens(name)
    if ptype in ("empty", "wedge"):
        return "empty"
    if ptype == "symmetry" or _hit(toks, ("symmetry", "sym", "symm")):
        return "symmetry"
    # 名字里明确带 inlet/outlet 时优先按名字判断(否则会被 type wall 吞掉)
    if _hit(toks, _OUTLET_HINTS):
        return "outlet"
    if _hit(toks, _INLET_HINTS):
        return "inlet"
    if _hit(toks, _FARFIELD_HINTS):
        return "farfield"
    if _hit(toks, _WALL_HINTS):
        return "wall"
    if ptype == "wall":
        return "wall"
    if ptype == "patch":
        return "farfield"
    return "other"


#: 湍流场在"入口/出口/壁面"上最常用的边界条件。
#  统计自 OpenFOAM 13 ``tutorials/incompressibleFluid`` 的 53 个案例(出现次数最多的):
#    k:       inlet fixedValue / outlet inletOutlet / wall kqRWallFunction
#    epsilon: inlet fixedValue / outlet inletOutlet / wall epsilonWallFunction
#    omega:   inlet fixedValue / outlet inletOutlet / wall omegaWallFunction
#    nuTilda: inlet fixedValue / outlet zeroGradient / wall zeroGradient   <- 不是 nut 的壁面函数!
#    nut:     inlet/outlet calculated                            / wall nutkWallFunction
_TURBULENCE_BCS: dict[str, dict[str, str]] = {
    "k": {"inlet": "fixedValue", "outlet": "inletOutlet", "wall": "kqRWallFunction"},
    "epsilon": {"inlet": "fixedValue", "outlet": "inletOutlet", "wall": "epsilonWallFunction"},
    "omega": {"inlet": "fixedValue", "outlet": "inletOutlet", "wall": "omegaWallFunction"},
    "nuTilda": {"inlet": "fixedValue", "outlet": "zeroGradient", "wall": "zeroGradient"},
    "nut": {"inlet": "calculated", "outlet": "calculated", "wall": "nutkWallFunction"},
    "alphat": {"inlet": "calculated", "outlet": "calculated", "wall": "alphatWallFunction"},
}


def _bc_params_for(bc: str, category: str) -> dict[str, Any]:
    """给出某个边界条件"跑起来"所需的最小参数(值取 $internalField, 后续可改)。"""
    if bc in ("fixedValue", "uniformFixedValue"):
        key = "uniformValue" if bc == "uniformFixedValue" else "value"
        return {key: "$internalField"}
    if bc in ("inletOutlet", "outletInlet"):
        return {"inletValue": "$internalField", "value": "$internalField"}
    if bc == "freestream":
        return {"freestreamValue": "$internalField"}
    if bc == "calculated":
        # OpenFOAM 里 calculated 派生自 fixedValue, 同样需要 value 条目,
        # 否则报 "Essential entry 'value' missing"
        return {"value": "$internalField"}
    if bc in ("freestreamVelocity", "freestreamPressure"):
        key = "freestreamValue"
        return {key: "$internalField"}
    if bc in ("kqRWallFunction", "epsilonWallFunction", "omegaWallFunction",
              "nutkWallFunction", "nutUSpaldingWallFunction", "nutUWallFunction",
              "alphatWallFunction"):
        return {"value": "$internalField"}
    return {}


def recommend_bc(category: str, field_name: str, patch_name: str, patch_type: str) -> tuple[str, dict[str, Any]]:
    """给出推荐边界条件, 返回 ``(类型, {参数: 值})``。

    推荐值按 OpenFOAM 13 官方教程(``tutorials/incompressibleFluid``)里的主流用法来定,
    并且**区分入口/出口/壁面/远场**:

    * 速度:   入口 fixedValue, 出口 zeroGradient, 壁面 noSlip, 远场 freestreamVelocity;
    * 压力:   入口 zeroGradient, 出口 fixedValue, 壁面 zeroGradient, 远场 freestreamPressure
              (出口固定压力同时充当"压力参考", 这样封闭域就不需要 pRefCell 了);
    * 湍流场: 见 ``_TURBULENCE_BCS``(k/epsilon/omega 用同名壁面函数, nuTilda 用 zeroGradient,
              nut 在非壁面用 calculated)。
    """
    cls = _classify_patch(patch_name, patch_type)
    if cls == "empty":
        return ("wedge" if patch_type == "wedge" else "empty", {})
    if cls == "symmetry":
        return ("symmetry", {})

    if category == "velocity":
        if cls == "wall":
            if _hit(_tokens(patch_name), _MOVING_WALL_HINTS):
                # 运动壁面(顶盖驱动/皮带/转子): 用 movingWallVelocity, 值留给用户改
                return ("movingWallVelocity", _bc_params_for("fixedValue", category))
            return ("noSlip", {})
        if cls == "inlet":
            return ("fixedValue", _bc_params_for("fixedValue", category))
        if cls == "outlet":
            return ("zeroGradient", {})
        if cls == "farfield":
            return ("freestreamVelocity", _bc_params_for("freestreamVelocity", category))
        return ("zeroGradient", {})

    if category == "pressure":
        if cls == "wall":
            return ("zeroGradient", {})
        if cls == "inlet":
            return ("zeroGradient", {})
        if cls == "outlet":
            return ("fixedValue", {"value": ["0"]})
        if cls == "farfield":
            return ("freestreamPressure", _bc_params_for("freestreamPressure", category))
        return ("zeroGradient", {})

    if category == "turbulence":
        low = field_name.lower()
        table = None
        for key, val in _TURBULENCE_BCS.items():
            if low == key.lower() or low.startswith(key.lower()):
                table = val
                break
        if table is None:
            table = {"inlet": "fixedValue", "outlet": "inletOutlet", "wall": "zeroGradient"}
        if cls == "wall":
            bc = table["wall"]
        elif cls == "inlet":
            bc = table["inlet"]
        elif cls == "outlet":
            bc = table["outlet"]
        elif cls == "farfield":
            bc = "freestream"          # 外流: k/omega/nuTilda 常用 freestream
        else:
            bc = "calculated" if low.startswith("nut") or low.startswith("alphat") else "zeroGradient"
        return (bc, _bc_params_for(bc, category))

    # 一般标量
    if cls == "wall":
        return ("zeroGradient", {})
    if cls == "inlet":
        return ("fixedValue", _bc_params_for("fixedValue", category))
    if cls == "outlet":
        return ("inletOutlet", _bc_params_for("inletOutlet", category))
    if cls == "farfield":
        return ("inletOutlet", _bc_params_for("inletOutlet", category))
    return ("zeroGradient", {})


def recommend_all(category: str, field_name: str, patches: list[tuple[str, str]]) -> list[tuple[str, str, dict]]:
    out = []
    for name, ptype in patches:
        bc, params = recommend_bc(category, field_name, name, ptype)
        out.append((name, bc, params))
    return out


# ---------------------------------------------------------------------------
# 参数的读写
# ---------------------------------------------------------------------------
def get_param(d: FoamDict, key: str) -> tuple[str, list[str]]:
    """读取参数。

    返回 ``(形式, token)``, 形式为 ``'uniform'`` / ``'macro'`` / ``'raw'`` / ``'missing'``。
    """
    v = d.get(key, None)
    if v is None:
        return ("missing", [])
    if isinstance(v, str) and v.startswith("$"):
        return ("macro", [v])
    if isinstance(v, Compound) and not isinstance(v, FoamList) and v and v[0] == "uniform":
        return ("uniform", dictfile.atoms(Compound(list(v[1:]))))
    if isinstance(v, str):
        # 纯数字/单词的简写形式, 视作 uniform
        return ("uniform", [v])
    return ("raw", dictfile.atoms(v))


def _value_from_tokens(tokens: list[str], uniform: bool) -> Any:
    toks = [str(t) for t in tokens]
    if not uniform:
        return toks[0] if len(toks) == 1 else Compound(toks)
    return dictfile.make_uniform(toks)


def set_param_scalar(d: FoamDict, key: str, text: str, uniform: bool = True) -> None:
    text = (text or "0").strip()
    d.set(key, _value_from_tokens([text], uniform))


def set_param_vector(d: FoamDict, key: str, xyz: list[str], uniform: bool = True) -> None:
    d.set(key, dictfile.make_uniform([str(x).strip() for x in xyz]) if uniform else FoamList([str(x).strip() for x in xyz]))


def set_param_word(d: FoamDict, key: str, word: str) -> None:
    d.set(key, word.strip())


def set_param_bool(d: FoamDict, key: str, flag: bool) -> None:
    d.set(key, "true" if flag else "false")


def set_param_macro(d: FoamDict, key: str, macro: str = "$internalField") -> None:
    d.set(key, macro if macro.startswith("$") else "$" + macro)


def param_summary(d: FoamDict) -> str:
    """给表格用的一行摘要文本。"""
    parts = []
    for k, v in d.items:
        if k == "type":
            continue
        parts.append(f"{k}={dictfile.format_value(v).replace(chr(10), ' ')}")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# internalField
# ---------------------------------------------------------------------------
def internal_field_value(d: FoamDict) -> tuple[str, list[str]]:
    """读取 ``internalField``, 返回 ``(形式, token)``。"""
    return get_param(d, "internalField")


def set_internal_field(d: FoamDict, tokens: list[str], uniform: bool = True) -> None:
    d.set("internalField", _value_from_tokens(tokens, uniform))


# ---------------------------------------------------------------------------
# 推荐值写回
# ---------------------------------------------------------------------------
def apply_params(d: FoamDict, kind: str, params: dict) -> None:
    """把 :func:`recommend_bc` 返回的参数字典写进 BC 字典 ``d``。"""
    for key, val in params.items():
        if isinstance(val, str) and val.startswith("$"):
            set_param_macro(d, key, val)
        elif isinstance(val, (list, tuple)):
            set_param_vector(d, key, list(val))
        elif isinstance(val, bool):
            set_param_bool(d, key, val)
        elif isinstance(val, (int, float)):
            set_param_scalar(d, key, str(val))
        elif isinstance(val, str):
            if dictfile.as_float(val) is not None:
                set_param_scalar(d, key, val)
            else:
                set_param_word(d, key, val)
