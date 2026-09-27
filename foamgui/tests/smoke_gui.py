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

    # 模型树 -> 边界条件页 联动
    win.patch_panel.select("walls")
    app.processEvents()
    assert win.bc_tab.table.currentRow() >= 0, "模型树选中补片后, 边界条件页没有定位"
    bc_row_name = win.bc_tab.table.item(win.bc_tab.table.currentRow(), 0).text()
    assert bc_row_name == "walls", f"边界条件页定位到了 {bc_row_name}"
    print("[3e] 模型树 -> 边界条件联动正常(walls)", flush=True)

    # 边界条件页 -> 模型树 反向联动
    win.bc_tab.select_patch("inlet")
    win.bc_tab.patchActivated.emit("inlet")
    app.processEvents()
    assert win.patch_panel.selected_patch() == "inlet", "边界条件页选中补片后模型树没跟上"
    print("[3f] 边界条件 -> 模型树联动正常(inlet)", flush=True)

    # 三维窗口拾取: 用 VTK 拾取接口模拟点击(inlet 面上的一个点)
    scene = win.view_panel.view.scene
    if scene.mesh is not None:
        fc = scene.mesh.face_centres()
        patch = scene.mesh.patch_by_name("outlet")
        c = fc[patch.start_face]
        win.view_panel.view.scene.renderer.SetDisplayPoint(c[0], c[1], c[2])
        print("[3g] 三维拾取接口可用:", hasattr(scene, "pick_patch"), flush=True)

    # 补片重命名
    win._rename_patch("walls", "blade")
    app.processEvents()
    assert win.case.mesh.patch_by_name("blade") is not None, "重命名后网格里没有新名字"
    assert "blade" in win.case.fields["U"].body.get("boundaryField").keys(), "场文件没同步改名"
    assert "constant/polyMesh/boundary" in win.case.render_all(), "没有生成 boundary 文件"
    assert win.patch_panel.selected_patch() == "blade", "重命名后模型树没有选中新名字"
    print("[3h] 补片重命名 walls -> blade 正常(网格/场/boundary 都已同步)", flush=True)

    # 再走一次"模型树里改名字"的完整信号链(等价于双击单元格改名)
    item = win.patch_panel._items["outlet"]
    item.setText(1, "farfield")
    app.processEvents()
    assert win.case.mesh.patch_by_name("farfield") is not None, "模型树改名没生效"
    assert "farfield" in win.case.fields["p"].body.get("boundaryField").keys(), "改名没同步到场"
    scene_colors = win.view_panel.view.scene.patch_colors
    assert "farfield" in scene_colors and "outlet" not in scene_colors, "三维场景里的补片索引没同步改名"
    print("[3i] 模型树双击改名 outlet -> farfield 正常(场景索引也同步)", flush=True)

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
