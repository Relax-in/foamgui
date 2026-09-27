"""真实窗口交互测试(需要图形显示)。

覆盖那些离屏冒烟测试覆盖不到的东西(它们用的是占位控件, 不创建 VTK 窗口):

* 用 Qt 真实鼠标事件点击三维窗口, 验证补片拾取 -> 模型树 -> 边界条件页的联动;
* 验证"拖动旋转"不会被误判成"点击选中", 且相机确实被旋转了;
* 验证正交/透视切换;
* 验证补片重命名后拾取缓存会重建。

用法::

    DISPLAY=:0 python -m foamgui.tests.interaction_gui airFoil2D

环境不允许创建窗口时会打印"跳过"并以 0 退出。
"""

from __future__ import annotations

import sys

from PyQt6 import QtCore, QtTest, QtWidgets


def main(argv: list[str]) -> int:
    from foamgui.qtfix import ensure_qt_deps

    ensure_qt_deps()

    from foamgui.ui.main_window import MainWindow

    case_dir = argv[1] if len(argv) > 1 else "airFoil2D"
    app = QtWidgets.QApplication(argv[:1])
    app.setStyle("Fusion")
    win = MainWindow()
    win.setGeometry(20, 20, 1520, 950)
    win.show()

    state = {"hits": [], "result": 0}

    def fail(msg: str) -> None:
        print(f"  [失败] {msg}", flush=True)
        state["result"] = 1

    def widget_pos(scene, patch_name: str) -> tuple[int, int]:
        w = win.view_panel.view.vtk_widget
        patch = scene.mesh.patch_by_name(patch_name)
        c = scene.mesh.face_centres()[patch.start_face]
        scene.renderer.SetWorldPoint(float(c[0]), float(c[1]), float(c[2]), 1.0)
        scene.renderer.WorldToDisplay()
        d = scene.renderer.GetDisplayPoint()
        return int(round(d[0])), int(round(w.height() - d[1] - 1))

    def start() -> None:
        win.load_case(case_dir)
        view = win.view_panel.view
        if not view.available:
            print("[跳过] 当前环境没有可用的 VTK 渲染窗口(离屏模式)", flush=True)
            win._dirty = False
            win.close()
            app.quit()
            return
        view.render()
        view.patchClicked.connect(lambda n: state["hits"].append(n))
        QtCore.QTimer.singleShot(2500, test_pick)

    def test_pick() -> None:
        view = win.view_panel.view
        w = view.vtk_widget
        scene = view.scene
        print("[1] 真实鼠标点击拾取", flush=True)
        for name in ("walls", "inlet", "outlet"):
            x, y = widget_pos(scene, name)
            QtTest.QTest.mouseClick(
                w, QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(x, y),
            )
            app.processEvents()
            got = state["hits"][-1] if state["hits"] else None
            print(f"    点击 {name:8s} @({x},{y}) -> {got}", flush=True)
            if got != name:
                fail(f"点击 {name} 拾取到 {got}")
        if win.patch_panel.selected_patch() != "outlet":
            fail("模型树没有跟随三维点选")
        row = win.bc_tab.table.currentRow()
        bc_name = win.bc_tab.table.item(row, 0).text() if row >= 0 else None
        if bc_name != "outlet":
            fail(f"边界条件页没有跟随(当前 {bc_name})")
        QtCore.QTimer.singleShot(300, test_drag)

    def test_drag() -> None:
        view = win.view_panel.view
        w = view.vtk_widget
        cam = view.scene.renderer.GetActiveCamera()
        before_pos = cam.GetPosition()
        before_hits = len(state["hits"])
        print("[2] 拖动应当是旋转而不是选中", flush=True)
        QtTest.QTest.mousePress(
            w, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(480, 480),
        )
        for i in range(1, 6):
            QtTest.QTest.mouseMove(w, QtCore.QPoint(480 + i * 12, 480 + i * 8))
            app.processEvents()
        QtTest.QTest.mouseRelease(
            w, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(540, 520),
        )
        app.processEvents()
        if len(state["hits"]) != before_hits:
            fail("拖动被误判成了点击选中")
        if cam.GetPosition() == before_pos:
            fail("拖动没有旋转相机(交互可能被事件过滤器破坏)")
        else:
            print("    相机已旋转:", tuple(round(v, 1) for v in cam.GetPosition()), flush=True)
        QtCore.QTimer.singleShot(300, test_projection)

    def test_projection() -> None:
        panel = win.view_panel
        cam = panel.view.scene.renderer.GetActiveCamera()
        print("[3] 正交/透视切换", flush=True)
        panel.cmb_projection.setCurrentIndex(1)
        panel.view.render()
        if cam.GetParallelProjection() != 0:
            fail("切到透视后 ParallelProjection 仍为 1")
        panel.cmb_projection.setCurrentIndex(0)
        panel.view.render()
        if cam.GetParallelProjection() != 1:
            fail("切回正交失败")
        print("    透视 -> ParallelProjection=0, 正交 -> 1", flush=True)
        QtCore.QTimer.singleShot(300, test_rename)

    def test_rename() -> None:
        scene = win.view_panel.view.scene
        print("[4] 重命名后拾取缓存要重建", flush=True)
        win._rename_patch("walls", "blade")
        app.processEvents()
        if win.case.mesh.patch_by_name("blade") is None:
            fail("网格里没有新补片名")
        if "blade" not in scene.patch_colors or "walls" in scene.patch_colors:
            fail("三维场景的补片索引没有同步改名")
        state["hits"].clear()
        x, y = widget_pos(scene, "blade")
        QtTest.QTest.mouseClick(
            win.view_panel.view.vtk_widget, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(x, y),
        )
        app.processEvents()
        if not state["hits"] or state["hits"][-1] != "blade":
            fail(f"改名后拾取失败: {state['hits']}")
        else:
            print("    改名后仍能正确拾取 blade", flush=True)
        finish()

    def finish() -> None:
        win._dirty = False
        win.close()
        app.quit()
        print("[OK] 交互测试通过" if state["result"] == 0 else "[失败] 交互测试未通过", flush=True)

    QtCore.QTimer.singleShot(1200, start)
    QtCore.QTimer.singleShot(120000, app.quit)
    app.exec()
    return state["result"]


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
