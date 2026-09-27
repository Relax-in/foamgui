"""端到端验证: 用 GUI 的内存模型生成字典, 再让 OpenFOAM 真正跑起来。

步骤:
1. 把案例复制到临时目录;
2. 用 :class:`~foamgui.foam.case.FoamCase` 读取, 用"推荐边界条件"重排所有场的 BC;
3. 修改 controlDict 让算例只跑几步;
4. 写出字典;
5. 依次用 foamDictionary 校验、用 foamRun 真跑, 检查是否生成了结果时间目录。

用法::

    python -m foamgui.tests.e2e_openfoam airFoil2D
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import time
from pathlib import Path

from ..foam import ofenv
from ..foam.case import FoamCase
from ..foam.fields import apply_params, recommend_bc


def main(argv: list[str]) -> int:
    src = Path(argv[1] if len(argv) > 1 else "airFoil2D").resolve()
    if not src.is_dir():
        print(f"案例目录不存在: {src}")
        return 2

    work = Path(tempfile.mkdtemp(prefix="foamgui_e2e_"))
    case_dir = work / src.name
    shutil.copytree(src, case_dir)
    print(f"[1] 已复制案例到 {case_dir}", flush=True)

    case = FoamCase(case_dir)
    case.load()
    case.load_mesh()
    print(f"[2] 读取完成: 场={list(case.fields)} 补片={case.patch_names}", flush=True)

    # 用推荐边界条件重排所有场(相当于在 GUI 里点"按补片名推荐边界条件")
    for name, ff in case.fields.items():
        for pname, ptype, _n in case.patch_info():
            bctype, params = recommend_bc(ff.category, name, pname, ptype)
            d = ff.patch_dict(pname)
            for key in list(d.keys()):
                del d[key]
            d.set("type", bctype)
            apply_params(d, ff.kind, params)
    print("[3] 已套用推荐边界条件", flush=True)

    # 只跑 5 步, 便于快速验证
    cd = case.control_dict()
    cd.set("endTime", "5")
    cd.set("writeInterval", "5")
    cd.set("writeControl", "timeStep")
    cd.set("purgeWrite", "0")

    written, backup = case.write(backup=True)
    print(f"[4] 写出 {len(written)} 个文件, 备份目录 {backup}", flush=True)

    # foamDictionary 校验
    bad = 0
    for rel in case.render_all():
        p = case_dir / rel
        rc, out = ofenv.run_foam_tool(["foamDictionary", str(p), "-expand"], timeout=60)
        if rc != 0:
            bad += 1
            print(f"    [失败] {rel}\n{out[:400]}", flush=True)
    print(f"[5] foamDictionary 校验: {'全部通过' if bad == 0 else f'{bad} 个失败'}", flush=True)
    if bad:
        print(f"    工作目录保留在 {work}")
        return 1

    # 真正跑一下
    t0 = time.time()
    rc, out = ofenv.run_foam_tool(["foamRun", "-case", str(case_dir)], timeout=900)
    print(f"[6] foamRun 返回码 {rc}, 用时 {time.time() - t0:.1f}s", flush=True)
    tail = "\n".join(out.strip().splitlines()[-25:])
    print("---- foamRun 输出(末 25 行) ----\n" + tail + "\n------------------------------", flush=True)

    result_dir = case_dir / "5"
    ok = rc == 0 and result_dir.is_dir() and (result_dir / "U").exists()
    if ok:
        print("[OK] 端到端验证通过: 生成的字典可以被 OpenFOAM 13 正常求解", flush=True)
        shutil.rmtree(work, ignore_errors=True)
        return 0
    print(f"[失败] 没有生成结果目录 {result_dir}; 工作目录保留在 {work}", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
