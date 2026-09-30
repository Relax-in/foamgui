"""不依赖 GUI 的单元测试。

直接运行::

    python -m foamgui.tests.test_foam [case_dir]
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from ..foam import dictfile, fields, polymesh
from ..foam.case import FoamCase, render_file

CASE = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("airFoil2D")
_failures: list[str] = []
_passed = 0


def check(cond: bool, message: str) -> None:
    global _passed
    if cond:
        _passed += 1
    else:
        _failures.append(message)
        print(f"  [失败] {message}", flush=True)


def section(title: str) -> None:
    print(f"\n=== {title} ===", flush=True)


# ---------------------------------------------------------------------------
def test_dictfile() -> None:
    section("字典解析 / 序列化")
    text = (CASE / "0" / "U").read_text()
    header, body = dictfile.parse_file(text)
    check(dictfile.get_atom(header, "class") == "volVectorField", "U 的 class 解析错误")
    check(body.keys() == ["dimensions", "internalField", "boundaryField"], "U 顶层条目顺序不对")
    form, tokens = fields.internal_field_value(body)
    check(form == "uniform" and tokens == ["25.75", "3.62", "0"], f"internalField 解析错误: {form} {tokens}")
    bf = body.get("boundaryField")
    check(bf.keys() == ["inlet", "outlet", "walls", "frontAndBack"], "boundaryField 补片列表不对")
    check(dictfile.get_atom(bf.get("walls"), "type") == "noSlip", "walls 的 type 不对")
    check(dictfile.get_atom(bf.get("inlet"), "freestreamValue") == "$internalField", "宏引用没有保留")

    # 函数式 key(div(phi,U)) 必须原样保留
    _h, fs = dictfile.parse_file((CASE / "system" / "fvSchemes").read_text())
    check("div(phi,U)" in fs.get("divSchemes").keys(), "div(phi,U) 这类 key 解析错误")
    check(dictfile.get_atom(fs.get("divSchemes"), "div((nuEff*dev2(T(grad(U)))))") == "Gauss linear",
          "带括号的复杂 key 解析错误")
    rendered = dictfile.format_body(fs)
    check("div(phi,U) bounded Gauss linearUpwind grad(U);" in rendered, "div 项序列化错误")
    check("div((nuEff*dev2(T(grad(U))))) Gauss linear;" in rendered, "复杂 div 项序列化错误")

    # 再解析一遍序列化结果, 内容必须一致
    again = dictfile.parse_dict(rendered)
    check(again.keys() == fs.keys(), "序列化后再解析的顶层条目不一致")

    # 带量纲的值
    _h2, pp = dictfile.parse_file((CASE / "constant" / "physicalProperties").read_text())
    nu = pp.get("nu")
    check(isinstance(nu, dictfile.Dimensioned), "nu 不是带量纲的值")
    check(nu.dims == ["0", "2", "-1", "0", "0", "0", "0"] and dictfile.atom(nu.value) == "1e-05",
          "nu 的量纲/数值解析错误")
    check("nu [0 2 -1 0 0 0 0] 1e-05;" in dictfile.format_body(pp), "nu 序列化错误")


def test_polymesh() -> None:
    section("网格读取")
    mesh = polymesh.read_polymesh(CASE / "constant" / "polyMesh")
    check(mesh.n_cells > 0 and mesh.n_points > 0, "网格为空")
    check(mesh.n_faces == len(mesh.owner), "owner 数量与 faces 不一致")
    check(mesh.face_sizes.min() >= 3, "存在点数少于 3 的面")
    check(mesh.patches and mesh.patches[0].name == "inlet", "补片读取错误")
    check(sum(p.n_faces for p in mesh.patches) == mesh.n_faces - mesh.n_internal_faces,
          "补片面数之和 != 边界面数")

    vols = mesh.cell_volumes()
    check(vols.size == mesh.n_cells, "单元体积数量不对")
    check((vols > 0).all(), "存在非正体积的单元")
    lo, hi = mesh.bounds
    bbox = float((hi - lo).prod())
    ratio = vols.sum() / bbox
    check(0.5 < ratio <= 1.0001, f"单元体积之和与包围盒体积比例异常: {ratio:.3f}")

    indptr, indices = mesh.cell_points()
    check(indptr.size == mesh.n_cells + 1, "cell_points 的 indptr 长度不对")
    check((indices < mesh.n_points).all(), "cell_points 出现越界点号")

    # 面心/单元中心应当落在包围盒内
    fc = mesh.face_centres()
    check(((fc >= lo - 1e-9) & (fc <= hi + 1e-9)).all(), "面心超出包围盒")


def test_binary_mesh() -> None:
    section("二进制网格(需要 OpenFOAM)")
    from ..foam import ofenv

    if ofenv.find_foam_bin("foamFormatConvert") is None:
        print("  跳过: 未找到 foamFormatConvert")
        return
    work = Path(tempfile.mkdtemp(prefix="foamgui_bin_"))
    try:
        case_dir = work / CASE.name
        shutil.copytree(CASE, case_dir)
        rc, out = ofenv.run_foam_tool(
            ["foamDictionary", "-entry", "writeFormat", "-set", "binary", "system/controlDict"],
            cwd=case_dir,
        )
        if rc != 0:
            print(f"  跳过: 写入 writeFormat 失败\n{out[:200]}")
            return
        rc, out = ofenv.run_foam_tool(["foamFormatConvert", "-constant", "-noZero"], cwd=case_dir, timeout=300)
        if rc != 0:
            print(f"  跳过: foamFormatConvert 失败\n{out[:200]}")
            return
        a = polymesh.read_polymesh(CASE / "constant" / "polyMesh")
        b = polymesh.read_polymesh(case_dir / "constant" / "polyMesh")
        check(b.header_format == "binary", "没有识别出二进制格式")
        check(a.n_cells == b.n_cells and a.n_points == b.n_points, "二进制网格规模不一致")
        check((a.points == b.points).all(), "二进制与 ascii 的点坐标不一致")
        check((a.owner == b.owner).all() and (a.neighbour == b.neighbour).all(), "二进制与 ascii 的 owner/neighbour 不一致")
        check(all((x == y).all() for x, y in zip(a.faces, b.faces)), "二进制与 ascii 的面不一致")
        check(abs(a.cell_volumes().sum() - b.cell_volumes().sum()) < 1e-6 * max(1.0, a.cell_volumes().sum()),
              "二进制与 ascii 的单元体积和不一致")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_dict_syntax() -> None:
    """OpenFOAM 13 里容易踩的字典写法, 必须原样往返(否则写回会写坏用户文件)。"""
    section("字典语法兼容(量纲/指令行)")

    def roundtrip(text: str) -> str:
        return dictfile.format_body(dictfile.parse_dict(text)).strip()

    # 1) "值在前、量纲在后"的量纲写法(OpenFOAM 10+ 的单位形式)
    for text in (
        "nu 1e-05 [m^2/s];",
        "rho 1 [kg/m^3];",
        "mu 1.8e-05 [kg/m/s];",
        "nu [0 2 -1 0 0 0 0] 1e-05;",          # 量纲在前
        "dimensions [0 2 -1 0 0 0 0];",        # 只有量纲(场文件里的 dimensions)
    ):
        out = roundtrip(text)
        check(out == text, f"量纲写法没有原样保留: {text!r} -> {out!r}")

    # 2) 预处理指令行(#include/#includeEtc) 不能把后面的条目吞掉
    text = (
        '#includeEtc "caseDicts/setConstraintTypes"\n\n'
        "inlet\n{\n    type fixedValue;\n    value uniform (1 0 0);\n}"
    )
    body = dictfile.parse_dict(text)
    check("inlet" in body.keys(), "指令行把后面的条目吞掉了")
    d = body.get("inlet")
    check(isinstance(d, dictfile.FoamDict) and d.get("type") == "fixedValue",
          "指令行后面的条目解析错误")
    check('#includeEtc "caseDicts/setConstraintTypes"' in dictfile.format_body(body),
          "指令行没有被原样写出")

    # 3) 指令当值 / 指令在列表里
    check(roundtrip('wheelSpeed #calc "$Uinlet / $wheelRadius";')
          == 'wheelSpeed #calc "$Uinlet / $wheelRadius";', "#calc 当值没有往返")
    check(roundtrip("internalField uniform (#neg $UMean 0 0);")
          == "internalField uniform (#neg $UMean 0 0);", "列表里的 #neg 没有往返")
    check(roundtrip('x #calc "sqrt(a)";') == 'x #calc "sqrt(a)";',
          "#calc 字符串里的括号干扰了指令行截断")

    # 4) #ifeq/#else/#endif 分支要整段保留
    text = "#ifeq $x 1\nfoo bar;\n#else\nfoo baz;\n#endif"
    out = roundtrip(text)
    for piece in ("#ifeq $x 1", "#else", "#endif"):
        check(piece in out, f"{piece} 没有保留")


def test_exporter_compat() -> None:
    """兼容第三方前处理工具导出的网格(ANSA 风格)。

    两个坑:
    1. 文件头没有 OpenFOAM 标准的 ``// * * * * //`` 分隔行, 而是额外两行注释;
    2. ``neighbour`` 按"每个面一项"写, 边界面用 -1 占位(标准 OpenFOAM 只写内部面)。
    """
    section("第三方导出格式兼容(ANSA 风格)")
    from foamgui.foam import polymesh as pm

    # 1) 头部: 没有标准分隔行, FoamFile 之后还有注释
    fake = (
        b"/*------------------------*\\\n"
        b"|    ANSA_VERSION: 25.0.0   |\n"
        b"\\*------------------------*/\n\n"
        b"FoamFile\n{\n\tversion 2.0;\n\tformat ascii;\n\tclass vectorField;\n"
        b'\tlocation "";\n\tobject points;\n}\n'
        b"/*-------------------------------*/\n"
        b"/*-------------------------------*/\n\n\n"
        b"3\n(\n(0 0 0)\n(1 0 0)\n(0 1 0)\n)\n"
    )
    header, rest = pm._split_header(fake)
    check("FoamFile" in header, "没有分隔行时应当能靠 FoamFile 定位头部")
    body = pm._skip_leading_noise(rest)
    check(body.startswith(b"3"), f"正文起点不对: {body[:12]!r}")

    # 2) neighbour 的 -1 占位
    import numpy as np

    padded = np.array([3, 5, 7, -1, -1, -1], dtype=np.int32)
    check(list(pm._strip_padded_neighbour(padded)) == [3, 5, 7], "-1 占位的 neighbour 没有清理")
    clean = np.array([1, 2, 3], dtype=np.int32)
    check(list(pm._strip_padded_neighbour(clean)) == [1, 2, 3], "标准 neighbour 被误改")

    # 3) 端到端: 造一个"ANSA 风格"的单位立方体单胞网格
    work = Path(tempfile.mkdtemp(prefix="foamgui_ansa_"))
    try:
        mesh_dir = work / "constant" / "polyMesh"
        mesh_dir.mkdir(parents=True)
        banner = (
            "/*------------------------*\\\n"
            "|    ANSA_VERSION: 25.0.0   |\n"
            "\\*------------------------*/\n\n"
        )
        tail = "/*-------------------------------*/\n/*-------------------------------*/\n\n\n"

        def write(name, cls, payload):
            text = (
                banner
                + "FoamFile\n{\n\tversion 2.0;\n\tformat ascii;\n"
                + f"\tclass {cls};\n\tlocation \"\";\n\tobject {name};\n}}\n"
                + tail
                + payload
            )
            (mesh_dir / name).write_text(text)

        pts = [(x, y, z) for z in (0, 1) for y in (0, 1) for x in (0, 1)]
        write("points", "vectorField",
              f"{len(pts)}\n(\n" + "".join(f"({x} {y} {z})\n" for x, y, z in pts) + ")\n")
        # 面的点序必须让法向朝外(OpenFOAM 的约定), 否则算出来的体积会错
        cube_faces = [
            (0, 2, 3, 1),  # z=0, 外法向 -z
            (4, 5, 7, 6),  # z=1, 外法向 +z
            (0, 1, 5, 4),  # y=0, 外法向 -y
            (2, 6, 7, 3),  # y=1, 外法向 +y
            (0, 4, 6, 2),  # x=0, 外法向 -x
            (1, 3, 7, 5),  # x=1, 外法向 +x
        ]
        write("faces", "faceList",
              f"{len(cube_faces)}\n(\n" + "".join("4(" + " ".join(map(str, f)) + ")\n" for f in cube_faces) + ")\n")
        write("owner", "labelList", f"{len(cube_faces)}\n(\n" + "0\n" * len(cube_faces) + ")\n")
        # 关键: 边界面也占一行, 填 -1
        write("neighbour", "labelList", f"{len(cube_faces)}\n(\n" + "-1\n" * len(cube_faces) + ")\n")
        write("boundary", "polyBoundaryMesh",
              "2\n(\n\tbottom\n\t{\n\t\ttype patch;\n\t\tnFaces 1;\n\t\tstartFace 0;\n\t}\n"
              "\trest\n\t{\n\t\ttype wall;\n\t\tnFaces 5;\n\t\tstartFace 1;\n\t}\n)\n")

        mesh = pm.read_polymesh(mesh_dir)
        check(mesh.n_points == 8, f"点数不对: {mesh.n_points}")
        check(mesh.n_faces == 6, f"面数不对: {mesh.n_faces}")
        check(mesh.n_internal_faces == 0, f"内部面数应为 0, 实际 {mesh.n_internal_faces}")
        check(mesh.n_cells == 1, f"单元数不对: {mesh.n_cells}")
        check(mesh.patch_names == ["bottom", "rest"], f"补片不对: {mesh.patch_names}")
        vol = mesh.cell_volumes()
        check(abs(float(vol.sum()) - 1.0) < 1e-9, f"单胞体积应为 1, 实际 {vol.sum()}")
        check(mesh.face_centres().shape == (6, 3), "面心计算异常")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_fields() -> None:
    section("边界条件推荐")
    # 推荐值按 OpenFOAM 13 教程(incompressibleFluid)里的主流用法, 并区分入口/出口/壁面/远场
    bc, params = fields.recommend_bc("velocity", "U", "inlet", "patch")
    check(bc == "fixedValue", f"U/inlet 应推荐 fixedValue, 实际 {bc}")
    bc, _p = fields.recommend_bc("velocity", "U", "outlet", "patch")
    check(bc == "zeroGradient", f"U/outlet 应推荐 zeroGradient, 实际 {bc}")
    bc, _p = fields.recommend_bc("velocity", "U", "walls", "wall")
    check(bc == "noSlip", "U/walls 推荐错误")
    bc, _p = fields.recommend_bc("velocity", "U", "frontAndBack", "empty")
    check(bc == "empty", "U/frontAndBack 推荐错误")
    bc, _p = fields.recommend_bc("velocity", "U", "farfield", "patch")
    check(bc == "freestreamVelocity", f"U/farfield 应推荐 freestreamVelocity, 实际 {bc}")
    # 出口固定压力同时充当压力参考, 封闭域才需要 pRefCell
    bc, params = fields.recommend_bc("pressure", "p", "outlet", "patch")
    check(bc == "fixedValue" and params["value"] == ["0"], f"p/outlet 推荐错误: {bc}")
    bc, _p = fields.recommend_bc("pressure", "p", "inlet", "patch")
    check(bc == "zeroGradient", f"p/inlet 应推荐 zeroGradient, 实际 {bc}")

    # 湍流场: 各场用各自的壁面函数, nut 在非壁面用 calculated
    for fld, expected in (("k", "kqRWallFunction"), ("epsilon", "epsilonWallFunction"),
                          ("omega", "omegaWallFunction"), ("nut", "nutkWallFunction")):
        bc, params = fields.recommend_bc("turbulence", fld, "walls", "wall")
        check(bc == expected, f"{fld}/walls 应推荐 {expected}, 实际 {bc}")
        check("value" in params, f"{fld}/walls 的壁面函数必须有 value")
    bc, _p = fields.recommend_bc("turbulence", "nuTilda", "walls", "wall")
    check(bc == "zeroGradient", f"nuTilda/walls 应当是 zeroGradient(不是 nut 的壁面函数), 实际 {bc}")
    bc, params = fields.recommend_bc("turbulence", "nut", "inlet", "patch")
    check(bc == "calculated" and params.get("value"), "nut 在入口应当是 calculated 且带 value")
    bc, _p = fields.recommend_bc("turbulence", "k", "outlet", "patch")
    check(bc == "inletOutlet", f"k/outlet 应推荐 inletOutlet, 实际 {bc}")

    names = [t.name for t in fields.bc_types_for("velocity", "empty")]
    check("empty" in names and "noSlip" not in names, "empty 补片的可用类型过滤错误")
    names = [t.name for t in fields.bc_types_for("velocity", "wall")]
    check("noSlip" in names and "symmetry" not in names, "wall 补片不应给出约束型边界 symmetry")


def test_case_roundtrip() -> None:
    section("案例读写")
    work = Path(tempfile.mkdtemp(prefix="foamgui_case_"))
    try:
        case = FoamCase(CASE)
        case.load()
        case.load_mesh()
        check(set(case.fields) >= {"U", "p"}, "没有读到基本的场")
        check(case.time_dir == "0", f"时间目录识别错误: {case.time_dir}")

        # 改一批值
        case.control_dict().set("endTime", "77")
        case.fields["U"].body.set("internalField", "uniform (1 2 3)")
        d = case.fields["U"].patch_dict("walls")
        d.set("type", "fixedValue")
        fields.set_param_vector(d, "value", ["1", "0", "0"])
        added = case.sync_patches()

        rendered = case.render_all()
        check("endTime 77;" in rendered["system/controlDict"], "controlDict 修改没生效")
        check("internalField uniform (1 2 3);" in rendered["0/U"], "internalField 修改没生效")
        check("value uniform (1 0 0);" in rendered["0/U"], "BC 参数修改没生效")

        out = work / "out"
        written, _backup = case.write(out_dir=out, backup=False)
        check(len(written) == len(rendered), "写出的文件数量与预览不一致")
        text = (out / "0" / "U").read_text()
        check("uniform (1 2 3)" in text, "写盘内容不正确")

        # 写出的文件必须还能被解析, 且值一致
        _h, body = dictfile.parse_file(text)
        form, tokens = fields.internal_field_value(body)
        check(form == "uniform" and tokens == ["1", "2", "3"], "写盘后再解析的值不一致")

        # 未知条目要保留: 手动加一个生僻条目再往返
        case.control_dict().set("myCustomEntry", "someWord 12");
        text2 = case.render_all()["system/controlDict"]
        check("myCustomEntry someWord 12;" in text2, "自定义条目没有保留")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_render_header() -> None:
    section("文件头")
    body = dictfile.FoamDict()
    body.set("foo", "bar")
    text = render_file(body, "dictionary", "testDict", "system")
    check("FoamFile" in text and "object      testDict;" in text, "文件头缺少 object")
    check("class       dictionary;" in text, "文件头缺少 class")
    check(text.rstrip().endswith("// ************************************************************************* //"),
          "文件结尾缺少分隔行")
    check("foo bar;" in text, "正文缺失")


def main() -> int:
    print(f"案例目录: {CASE.resolve()}")
    for fn in (
        test_dictfile,
        test_polymesh,
        test_binary_mesh,
        test_dict_syntax,
        test_exporter_compat,
        test_fields,
        test_case_roundtrip,
        test_render_header,
    ):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            _failures.append(f"{fn.__name__} 抛出异常: {exc}")
    print(f"\n通过 {_passed} 项检查, 失败 {len(_failures)} 项")
    if _failures:
        for f in _failures:
            print("  -", f)
        return 1
    print("[OK] 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
