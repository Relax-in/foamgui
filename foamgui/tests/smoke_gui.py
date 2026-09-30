"""离屏 GUI 冒烟测试: 打开案例、切换每个页签、截图、试写字典。

用法::

    QT_QPA_PLATFORM=offscreen FOAMGUI_SKIP_VTK_WIDGET=1 \
        python -m foamgui.tests.smoke_gui <case_dir> [out_dir]
"""

from __future__ import annotations

import os
import sys
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("FOAMGUI_SKIP_VTK_WIDGET", "1")

# 与 app.py 一样先修 Qt 环境(终端 source 过 OpenFOAM 时系统 Qt6 会串味)
from foamgui.qtfix import ensure_qt_deps  # noqa: E402

ensure_qt_deps()



def _silence_dialogs() -> None:
    """把模态弹窗改成自动应答, 否则自动化测试会被没人点的对话框卡住。"""
    from PyQt6 import QtWidgets

    ok = QtWidgets.QMessageBox.StandardButton.Ok
    yes = QtWidgets.QMessageBox.StandardButton.Yes
    QtWidgets.QMessageBox.information = staticmethod(lambda *a, **k: ok)
    QtWidgets.QMessageBox.warning = staticmethod(lambda *a, **k: ok)
    QtWidgets.QMessageBox.critical = staticmethod(lambda *a, **k: ok)
    QtWidgets.QMessageBox.question = staticmethod(lambda *a, **k: yes)
    QtWidgets.QMessageBox.about = staticmethod(lambda *a, **k: None)


def main(argv: list[str]) -> int:
    from PyQt6 import QtWidgets

    from foamgui.ui.main_window import MainWindow

    case_dir = argv[1] if len(argv) > 1 else "airFoil2D"
    out_dir = argv[2] if len(argv) > 2 else "_scratch/gui"
    os.makedirs(out_dir, exist_ok=True)

    app = QtWidgets.QApplication(sys.argv[:1])
    _silence_dialogs()
    app.setStyle("Fusion")
    win = MainWindow()
    win.resize(1500, 950)
    win.show()
    app.processEvents()

    win.load_case(case_dir)
    app.processEvents()
    assert win.case is not None, "案例没有加载成功"
    print(f"[1] 案例加载完成: {win.case.root}", flush=True)
    print(f"    场: {list(win.case.fields)}", flush=True)
    print(f"    补片: {win.case.patch_info()}", flush=True)
    print(f"    网格: {win.case.mesh.summary() if win.case.mesh else '未读取'}", flush=True)

    failures: list[str] = []
    # 整窗截图(左边三维常驻 + 右上模型树 + 右下页签)
    for i in range(win.tabs.count()):
        win.tabs.setCurrentIndex(i)
        app.processEvents()
        name = win.tabs.tabText(i).replace(" ", "").replace(".", "_")
        path = os.path.abspath(os.path.join(out_dir, f"tab{i + 1}_{name}.png"))
        pix = win.grab()
        ok = pix.save(path)
        print(f"[2] 页签 {win.tabs.tabText(i)} -> {path} ({pix.width()}x{pix.height()}) ok={ok}", flush=True)
        if not ok:
            failures.append(f"截图失败: {name}")

    # 试着改一些设置再检查是否真的写进了内存字典
    from foamgui.foam import dictfile

    case = win.case
    case.control_dict().set("endTime", "123")
    case.fields["U"].patch_dict("walls").set("type", "noSlip")
    case.fields["U"].body.set("internalField", "uniform (30 0 0)")
    win.bc_tab.set_case(case)
    win.ic_tab.refresh()
    win.solver_tab.set_case(case)
    win.output_tab.refresh(force=True)
    app.processEvents()
    rendered = case.render_all()
    assert "endTime 123;" in rendered["system/controlDict"], "controlDict 未更新"
    assert "uniform (30 0 0)" in rendered[[k for k in rendered if k.endswith("/U")][0]], "internalField 未更新"
    print("[3] 内存字典修改生效", flush=True)

    # --- 交互路径: 切换场、切换 BC 类型、添加场、改初始条件 ---
    # 下面的补片名都从案例里现取, 这样任何算例(不限于 airFoil2D)都能跑这个测试
    patch_names = [p[0] for p in case.patch_info()]
    if len(patch_names) < 3:
        print(f"[跳过] 补片太少({patch_names}), 跳过联动/重命名相关检查", flush=True)
        patch_names = patch_names + [""] * (3 - len(patch_names))
    p0, p1, p2 = patch_names[0], patch_names[1], patch_names[2]
    field0 = "p" if "p" in case.fields else next(iter(case.fields), None)
    print(f"[3a] 本案例补片: {patch_names} | 用场 {field0} 做交互检查", flush=True)

    win.tabs.setCurrentWidget(win.bc_tab)
    assert win.bc_tab.cmb_field.count() == len(case.fields), "边界条件页的场列表不对"
    win.bc_tab.cmb_field.setCurrentText(field0)
    app.processEvents()
    assert win.bc_tab.table.rowCount() == len(case.patch_info()), "切换场后补片表没有重建"
    # 把第 0 个补片的 BC 改成 fixedValue, 参数表单应当重建
    combo = win.bc_tab.table.cellWidget(0, 1)
    combo.setCurrentText("fixedValue")
    app.processEvents()
    d0 = case.fields[field0].patch_dict(p0)
    assert dictfile.get_atom(d0, "type") == "fixedValue", f"切换 BC 类型没有写进字典({p0})"
    assert "value" in d0.keys(), "切换类型后没有生成默认参数"
    print(f"[3b] 边界条件页交互正常: {p0} -> {d0.items}", flush=True)

    # 添加湍流场 k
    win.tabs.setCurrentWidget(win.ic_tab)
    win.ic_tab.cmb_new_field.setCurrentText("k")
    win.ic_tab._add_field()
    app.processEvents()
    assert "k" in case.fields, "添加场 k 失败"
    assert case.fields["k"].patch_type(p1), "新场没有生成边界条件"
    print(f"[3c] 已添加场 k, {p1} 的 BC = {case.fields['k'].patch_type(p1)}", flush=True)

    # 改初始条件(通过界面控件)
    rows = [i for i, n in enumerate(win.ic_tab._rows) if n == "k"]
    if rows:
        win.ic_tab.table.selectRow(rows[0])
        app.processEvents()
        win.ic_tab.edit_value.form_combo.setCurrentIndex(0)
        win.ic_tab.edit_value.edits[0].setText("0.25")
        app.processEvents()
        assert "uniform 0.25" in case.render_all()["0/k"], "通过界面改初始条件失败"
        print("[3d] 初始条件界面交互正常", flush=True)

    # 模型树 -> 边界条件页 联动
    win.patch_panel.select(p0)
    app.processEvents()
    assert win.bc_tab.table.currentRow() >= 0, "模型树选中补片后, 边界条件页没有定位"
    bc_row_name = win.bc_tab.table.item(win.bc_tab.table.currentRow(), 0).text()
    assert bc_row_name == p0, f"边界条件页定位到了 {bc_row_name}, 期望 {p0}"
    print(f"[3e] 模型树 -> 边界条件联动正常({p0})", flush=True)

    # 边界条件页 -> 模型树 反向联动
    win.bc_tab.select_patch(p1)
    win.bc_tab.patchActivated.emit(p1)
    app.processEvents()
    assert win.patch_panel.selected_patch() == p1, "边界条件页选中补片后模型树没跟上"
    print(f"[3f] 边界条件 -> 模型树联动正常({p1})", flush=True)

    # 三维拾取接口(离屏模式没有渲染窗口, 只确认接口在)
    scene = win.view_panel.view.scene
    if scene.mesh is not None:
        print("[3g] 三维拾取接口可用:", hasattr(scene, "pick_patch"), flush=True)

    # 补片重命名: 网格 / 所有场 / boundary 文件 / 模型树 / 三维场景索引 都要同步
    new0 = p0 + "_renamed"
    win._rename_patch(p0, new0)
    app.processEvents()
    assert win.case.mesh.patch_by_name(new0) is not None, "重命名后网格里没有新名字"
    for fname, ff in win.case.fields.items():
        if new0 in ff.body.get("boundaryField").keys() or p0 == "":
            break
    assert new0 in win.case.fields[field0].body.get("boundaryField").keys(), "场文件没同步改名"
    assert "constant/polyMesh/boundary" in win.case.render_all(), "没有生成 boundary 文件"
    assert win.patch_panel.selected_patch() == new0, "重命名后模型树没有选中新名字"
    print(f"[3h] 补片重命名 {p0} -> {new0} 正常(网格/场/boundary 都已同步)", flush=True)

    # 再走一次"模型树里改名字"的完整信号链(等价于双击单元格改名)
    new1 = p1 + "_renamed"
    item = win.patch_panel._items[p1]
    item.setText(1, new1)
    app.processEvents()
    assert win.case.mesh.patch_by_name(new1) is not None, "模型树改名没生效"
    assert new1 in win.case.fields[field0].body.get("boundaryField").keys(), "改名没同步到场"
    scene_colors = win.view_panel.view.scene.patch_colors
    assert new1 in scene_colors and p1 not in scene_colors, "三维场景里的补片索引没同步改名"
    print(f"[3i] 模型树双击改名 {p1} -> {new1} 正常(场景索引也同步)", flush=True)

    # controlDict 的求解器: 老案例只有 application 时, 改动必须写到 solver
    from foamgui.foam import dictfile as _df

    cd = case.control_dict()
    if "solver" in cd:
        del cd["solver"]
    cd.set("application", "UserSolver")          # 造出"老式写法"的案例
    win.solver_tab.set_case(case)
    app.processEvents()
    form = win.solver_tab.control_form
    combo = None
    for row in range(form.rowCount()):
        item = form.itemAt(row, QtWidgets.QFormLayout.ItemRole.FieldRole)
        if item is not None and isinstance(item.widget(), QtWidgets.QComboBox):
            combo = item.widget()
            break
    assert combo is not None, f"求解设置页里找不到求解器下拉框(共 {form.rowCount()} 行)"
    combo.setCurrentText("incompressibleFluid")
    app.processEvents()
    text = case.render_all()["system/controlDict"]
    assert _df.get_atom(cd, "solver", "") == "incompressibleFluid", \
        f"改动后应当写入 solver, 实际 solver={_df.get_atom(cd, 'solver')!r}"
    assert "solver incompressibleFluid;" in text, "写出的 controlDict 里没有 solver incompressibleFluid;"
    assert "application" not in cd.keys(), "选了求解器后应当清掉老式 application 条目"
    assert text.index("solver incompressibleFluid;") < text.index("startFrom"), \
        "solver 应当写在 controlDict 靠前的位置"
    print("[3j] 求解器写入 solver, 并清掉老式 application、放到文件开头", flush=True)

    # 显式"把当前显示的值写入字典": 应当补齐 pRefCell/pRefValue 这类条目
    before_keys = [k for k, _ in _df.get_dict(case.render_all() and case.get("system", "fvSolution"), "SIMPLE").items] \
        if _df.get_dict(case.get("system", "fvSolution"), "SIMPLE") else []
    win.solver_tab._commit_all()
    app.processEvents()
    fvs = case.get("system", "fvSolution")
    algo = _df.get_dict(fvs, "SIMPLE") or _df.get_dict(fvs, "PIMPLE")
    assert algo is not None, "fvSolution 里既没有 SIMPLE 也没有 PIMPLE"
    assert "pRefCell" in algo.keys(), "提交默认值后算法段里应当有 pRefCell"
    assert "pRefValue" in algo.keys(), "提交默认值后算法段里应当有 pRefValue"
    print(f"[3k] 『把当前显示的值写入字典』正常(算法段现在有 {algo.keys()})", flush=True)

    # 自检 + 修改补片网格类型
    from foamgui.foam import validate as _val

    win.output_tab.refresh(force=True)
    app.processEvents()
    n_before = len(win.output_tab.issues)
    print(f"[3l] 自检在界面上可用(当前 {n_before} 条): {win.output_tab.lbl_check.text()[:60]}", flush=True)
    if p2:
        win._change_patch_type(p2, "symmetry")
        app.processEvents()
        assert win.case.mesh.patch_by_name(p2).type == "symmetry", "补片网格类型没改掉"
        avail = [t.name for t in __import__("foamgui.foam.fields", fromlist=["x"]).bc_types_for(
            case.fields[field0].category, "symmetry")]
        assert avail == ["symmetry"], f"symmetry 补片的候选应为 symmetry, 实际 {avail}"
        assert win.case.render_all().get("constant/polyMesh/boundary"), "改类型后应生成 boundary 文件"
        print(f"[3m] 补片网格类型 {p2} -> symmetry 正常(边界条件候选已跟着变)", flush=True)

    # 写出到临时目录
    written, _ = case.write(out_dir=out_dir, backup=False)
    print(f"[4] 写出 {len(written)} 个文件到 {out_dir}", flush=True)
    for p in written:
        if not os.path.exists(p):
            failures.append(f"文件未写出: {p}")
    u_path = os.path.join(out_dir, "0", "U")
    if os.path.exists(u_path):
        if "uniform (30 0 0)" not in open(u_path, encoding="utf-8").read():
            failures.append("写出的 0/U 内容不正确")
    else:
        failures.append("没有写出 0/U")

    # 再抓一张“生成字典”页
    win.tabs.setCurrentWidget(win.output_tab)
    app.processEvents()
    pix = win.grab()
    p = os.path.abspath(os.path.join(out_dir, "tab5_output_after.png"))
    pix.save(p)
    print(f"[5] 生成字典页截图 -> {p}", flush=True)

    win._dirty = False  # 避免关闭时弹出确认对话框(无人应答会卡住)
    win.close()
    app.processEvents()
    if failures:
        print("失败项:")
        for f in failures:
            print("   -", f)
        return 1
    print("[OK] GUI 冒烟测试通过", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except Exception:
        traceback.print_exc()
        raise SystemExit(2)
