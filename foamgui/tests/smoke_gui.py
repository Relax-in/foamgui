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


def main(argv: list[str]) -> int:
    from PyQt6 import QtWidgets

    from foamgui.ui.main_window import MainWindow

    case_dir = argv[1] if len(argv) > 1 else "airFoil2D"
    out_dir = argv[2] if len(argv) > 2 else "_scratch/gui"
    os.makedirs(out_dir, exist_ok=True)

    app = QtWidgets.QApplication(sys.argv[:1])
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
    assert "uniform (30 0 0)" in rendered["0/U"], "internalField 未更新"
    print("[3] 内存字典修改生效", flush=True)

    # --- 交互路径: 切换场、切换 BC 类型、添加场、改初始条件 ---
    win.tabs.setCurrentWidget(win.bc_tab)
    assert win.bc_tab.cmb_field.count() == len(case.fields), "边界条件页的场列表不对"
    win.bc_tab.cmb_field.setCurrentText("p")
    app.processEvents()
    assert win.bc_tab.table.rowCount() == len(case.patch_info()), "切换场后补片表没有重建"
    # 把 p 的 inlet 改成 fixedValue, 参数表单应当重建
    row = 0
    combo = win.bc_tab.table.cellWidget(row, 1)
    combo.setCurrentText("fixedValue")
    app.processEvents()
    d_inlet = case.fields["p"].patch_dict("inlet")
    assert dictfile.get_atom(d_inlet, "type") == "fixedValue", "切换 BC 类型没有写进字典"
    assert "value" in d_inlet.keys(), "切换类型后没有生成默认参数"
    print("[3b] 边界条件页交互正常:", d_inlet.items, flush=True)

    # 添加湍流场 k
    win.tabs.setCurrentWidget(win.ic_tab)
    win.ic_tab.cmb_new_field.setCurrentText("k")
    win.ic_tab._add_field()
    app.processEvents()
    assert "k" in case.fields, "添加场 k 失败"
    assert case.fields["k"].patch_type("walls"), "新场没有生成边界条件"
    print(f"[3c] 已添加场 k, walls 的 BC = {case.fields['k'].patch_type('walls')}", flush=True)

    # 改初始条件(通过界面控件)
    win.ic_tab.table.selectRow([i for i, n in enumerate(win.ic_tab._rows) if n == "k"][0])
    app.processEvents()
    win.ic_tab.edit_value.form_combo.setCurrentIndex(0)
    win.ic_tab.edit_value.edits[0].setText("0.25")
    app.processEvents()
    assert "uniform 0.25" in case.render_all()["0/k"], "通过界面改初始条件失败"
    print("[3d] 初始条件界面交互正常", flush=True)

    # 求解设置页: 改 endTime
    win.tabs.setCurrentWidget(win.solver_tab)
    app.processEvents()
    print("[3e] 求解设置页控件数:", win.solver_tab.control_form.rowCount(), flush=True)

    # 写出到临时目录
    written, _ = case.write(out_dir=out_dir, backup=False)
    print(f"[4] 写出 {len(written)} 个文件到 {out_dir}", flush=True)
    for p in written:
        if not os.path.exists(p):
            failures.append(f"文件未写出: {p}")
    sample = open(os.path.join(out_dir, "0", "U"), encoding="utf-8").read()
    if "uniform (30 0 0)" not in sample:
        failures.append("写出的 0/U 内容不正确")

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
