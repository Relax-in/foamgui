"""端到端: 只给"网格 + 0/", 由工具生成整套不可压缩字典, 再用 OpenFOAM 真跑。

这是最贴近实际使用的场景: 用户从 ANSA/其它前处理工具导出网格, 手上只有
``constant/polyMesh`` 和 ``0/``(甚至只有 U/p), 需要工具把 system/ 与 constant/
里的字典补齐到"能跑"的程度。

覆盖三种最常见的不可压缩设置:

* ``laminar``  —— 层流, 不需要额外的场;
* ``kOmegaSST``—— RAS, 需要 k/omega/nut(工具应能一键补齐);
* ``SpalartAllmaras`` —— RAS, 需要 nuTilda/nut。

用法::

    python -m foamgui.tests.e2e_generate airFoil2D
    python -m foamgui.tests.e2e_generate 111            # 自己的案例也行

检查点: 每个模式都要 1) 自检无错误; 2) foamRun 返回码 0 且跑到 End。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from foamgui.foam import dictfile, fields as F, validate  # noqa: E402
from foamgui.foam.case import FoamCase  # noqa: E402

#: 模式 -> (simulationType, 模型名)
MODES = {
    "laminar": ("laminar", ""),
    "kOmegaSST": ("RAS", "kOmegaSST"),
    "SpalartAllmaras": ("RAS", "SpalartAllmaras"),
}


def prepare(src: Path, dst: Path) -> None:
    """复制案例, 然后删掉所有"应该由工具生成"的字典(保留网格与 0/)。"""
    shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)
    shutil.rmtree(dst / "foamgui_backup", ignore_errors=True)
    shutil.rmtree(dst / "system", ignore_errors=True)
    for name in ("momentumTransport", "physicalProperties"):
        (dst / "constant" / name).unlink(missing_ok=True)
    for entry in list(dst.iterdir()):          # 结果时间目录(0/ 留着)
        if entry.name[0].isdigit() and entry.name != "0":
            shutil.rmtree(entry, ignore_errors=True) if entry.is_dir() else entry.unlink(missing_ok=True)
    # 0/ 里只留 U 和 p: 湍流场交给工具按模型补
    for entry in list((dst / "0").iterdir()):
        if entry.name not in ("U", "p"):
            entry.unlink(missing_ok=True)


def configure(case: FoamCase, sim: str, model: str) -> list[str]:
    """按模式设置求解器/湍流模型, 并让工具补齐缺的场与字典条目。"""
    cd = case.control_dict()
    if "application" in cd:
        del cd["application"]
    cd.set("solver", "incompressibleFluid")
    cd.move_to_front("solver")
    cd.set("endTime", "100")                   # 缩短, 只验证能不能跑起来

    mt = case.get("constant", "momentumTransport")
    mt.set("simulationType", sim)
    for key in ("RAS", "LES"):
        if key in mt:
            del mt[key]
    if model:
        sub = dictfile.FoamDict()
        sub.set("model", model)
        sub.set("turbulence", "on")
        mt.set(sim, sub)

    added: list[str] = []
    for name in F.TURBULENCE_FIELDS.get(model, []):
        if name not in case.fields:
            case.add_field(name)
            added.append(name)
    case.sync_patches()
    case.ensure_schemes_for_fields(F.TURBULENCE_FIELDS.get(model, []))
    # 边界条件全部用工具的推荐值; 入口给 1 m/s, 免得流场是静止的
    for ff in case.fields.values():
        for patch in case.mesh.patches:
            bc, params = F.recommend_bc(ff.category, ff.name, patch.name, patch.type)
            sub = ff.patch_dict(patch.name)
            for key in list(sub.keys()):
                del sub[key]
            sub.set("type", bc)
            F.apply_params(sub, ff.kind, params)
    if "U" in case.fields:
        F.set_internal_field(case.fields["U"].body, ["(1 0 0)"])
        for patch in case.mesh.patches:
            if F.recommend_bc("velocity", "U", patch.name, patch.type)[0] == "fixedValue":
                sub = case.fields["U"].patch_dict(patch.name)
                for key in list(sub.keys()):
                    del sub[key]
                sub.set("type", "fixedValue")
                F.apply_params(sub, "vector", {"value": ["(1 0 0)"]})
    return added


def run_solver(case_dir: Path) -> tuple[int, str]:
    cmd = (
        ". /home/in/OpenFOAM/OpenFOAM-13/etc/bashrc; "
        f"exec foamRun -case {case_dir}"
    )
    proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          timeout=600, cwd=str(case_dir))
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str]) -> int:
    src = Path(argv[1] if len(argv) > 1 else "airFoil2D").resolve()
    if not src.is_dir():
        print(f"[失败] 找不到案例目录 {src}")
        return 1
    work_root = Path(tempfile.mkdtemp(prefix="foamgui_gen_"))
    print(f"源案例: {src}\n工作目录: {work_root}\n", flush=True)

    failed: list[str] = []
    for mode, (sim, model) in MODES.items():
        dst = work_root / mode
        prepare(src, dst)
        case = FoamCase(dst)
        case.load()
        case.load_mesh()
        added = configure(case, sim, model)
        print(f"[{mode}] 网格 {case.mesh.summary()}", flush=True)
        if added:
            print(f"[{mode}] 工具补齐的场: {added}", flush=True)
        problems = validate.check_case(case)
        errors = [i for i in problems if i.level == "error"]
        print(f"[{mode}] 自检: " + ("未发现问题 ✓" if not problems else
              "; ".join(f"{i.level}:{i.where}" for i in problems)), flush=True)
        case.write(out_dir=dst, backup=False)
        rc, out = run_solver(dst)
        ok = rc == 0 and "End" in out
        tail = [l for l in out.splitlines() if "Solving for" in l or "bounding" in l]
        print(f"[{mode}] foamRun rc={rc} {'通过 ✓' if ok else '失败'}"
              + (f" | {tail[-1][:70]}" if tail else ""), flush=True)
        if not ok:
            print("\n".join(out.splitlines()[-12:]), flush=True)
            failed.append(mode)
        if errors:
            failed.append(f"{mode}(自检有错误)")
        print(flush=True)

    if failed:
        print(f"[失败] 未通过: {failed}\n工作目录保留在 {work_root}")
        return 1
    print("[OK] 三种不可压缩模式都能生成可运行字典(网格+0/ -> 生成 -> foamRun)")
    shutil.rmtree(work_root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    os.environ.setdefault("FOAMGUI_E2E_NO_RUN", "0")
    raise SystemExit(main(sys.argv))
