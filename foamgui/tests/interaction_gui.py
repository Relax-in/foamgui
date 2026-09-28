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

# 必须在导入 PyQt6 之前修 Qt 环境(终端 source 过 OpenFOAM 时系统 Qt6 会串味)
from foamgui.qtfix import ensure_qt_deps  # noqa: E402

ensure_qt_deps()

from PyQt6 import QtCore, QtTest, QtWidgets  # noqa: E402


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
    from foamgui.ui.main_window import MainWindow

    case_dir = argv[1] if len(argv) > 1 else "airFoil2D"
    app = QtWidgets.QApplication(argv[:1])
    _silence_dialogs()
    app.setStyle("Fusion")
    win = MainWindow()
    win.setGeometry(20, 20, 1520, 950)
    win.show()

    state = {"hits": [], "result": 0}

    def fail(msg: str) -> None:
        print(f"  [失败] {msg}", flush=True)
        state["result"] = 1

    def widget_pos(scene, patch_name: str):
        """找一个补片上"最正对相机"的面。

        返回 ``((x_qt, y_qt), (x_vtk, y_vtk))``:
        Qt 控件坐标原点在左上(给 QTest 点击用), VTK 显示坐标原点在左下
        (给 pick_patch 用) —— 两者差一个 y 翻转, 混用会拾取到错误的位置。

        直接取第一个面可能取到背面或棱角上的面, 在三维算例里会打空。
        """
        import numpy as np

        w = win.view_panel.view.vtk_widget
        patch = scene.mesh.patch_by_name(patch_name)
        centres = scene.mesh.face_centres()[patch.start_face : patch.start_face + patch.n_faces]
        pts = scene.mesh.points
        cam = np.array(scene.renderer.GetActiveCamera().GetPosition())
        normals = np.empty_like(centres)
        for i, f in enumerate(scene.mesh.faces[patch.start_face : patch.start_face + patch.n_faces]):
            p0, p1, p2 = pts[f[0]], pts[f[1]], pts[f[2]]
            n = np.cross(p1 - p0, p2 - p0)
            ln = np.linalg.norm(n)
            normals[i] = n / ln if ln > 1e-30 else 0.0
        view = cam - centres
        view /= np.linalg.norm(view, axis=1, keepdims=True) + 1e-30
        best = int(np.argmax(np.einsum("ij,ij->i", normals, view)))
        c = centres[best]
        scene.renderer.SetWorldPoint(float(c[0]), float(c[1]), float(c[2]), 1.0)
        scene.renderer.WorldToDisplay()
        d = scene.renderer.GetDisplayPoint()
        return (
            (int(round(d[0])), int(round(w.height() - d[1] - 1))),
            (int(round(d[0])), int(round(d[1]))),
        )

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
        # 先用等轴测视角: 三维算例下正视图往往只能看到最外面一层补片
        win.view_panel.cmb_projection.setCurrentIndex(0)
        view.reset_view("iso")
        app.processEvents()
        names = [p.name for p in scene.mesh.patches if not p.is_empty][:4]
        state["names"] = names
        print(f"[1] 真实鼠标点击拾取({len(names)} 个补片: {names}, 等轴测视角)", flush=True)
        hits_ok = []
        skipped = []
        for name in names:
            (x, y), (vx, vy) = widget_pos(scene, name)
            if not (0 <= x < w.width() and 0 <= y < w.height()):
                skipped.append(name)
                print(f"    跳过 {name:24s}(投影不在窗口内: {x},{y})", flush=True)
                continue
            # 先用纯射线判断这个补片在当前视角下是否可见(参考值) —— 注意用 VTK 坐标
            ray = scene.pick_patch(vx, vy, snap_px=0)
            QtTest.QTest.mouseClick(
                w, QtCore.Qt.MouseButton.LeftButton,
                QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(x, y),
            )
            app.processEvents()
            got = state["hits"][-1] if state["hits"] else None
            print(f"    点击 {name:24s} @({x},{y}) 射线={ray} -> 选中 {got}", flush=True)
            if ray is None:
                # 视线上没有面(例如二维案例里垂直于视线的薄带), 必须靠吸附命中
                if got != name:
                    fail(f"{name} 视线上没有面, 吸附应当命中它, 实际 {got}")
                else:
                    hits_ok.append(name)
            elif ray == name:
                if got != name:
                    fail(f"射线已命中 {name}, 选中却变成 {got}")
                else:
                    hits_ok.append(name)
            else:
                skipped.append(name)
                print(f"        ({name} 在该视角被判遮挡, 跳过)", flush=True)
        print(f"    命中验证: {hits_ok} | 遮挡/越界跳过: {skipped}", flush=True)
        state["picked"] = hits_ok[0] if hits_ok else None
        if not hits_ok:
            fail("没有任何补片被成功拾取")
            return finish()
        last = hits_ok[-1]
        if win.patch_panel.selected_patch() != last:
            fail(f"模型树没有跟随三维点选({win.patch_panel.selected_patch()} != {last})")
        row = win.bc_tab.table.currentRow()
        bc_name = win.bc_tab.table.item(row, 0).text() if row >= 0 else None
        if bc_name != last:
            fail(f"边界条件页没有跟随(当前 {bc_name}, 期望 {last})")
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
        old = state.get("picked") or (state["names"][0] if state.get("names") else None)
        if not old:
            return finish()
        new = old + "_renamed"
        win._rename_patch(old, new)
        app.processEvents()
        if win.case.mesh.patch_by_name(new) is None:
            fail("网格里没有新补片名")
        if new not in scene.patch_colors or old in scene.patch_colors:
            fail("三维场景的补片索引没有同步改名")
        state["hits"].clear()
        (x, y), _vtk = widget_pos(scene, new)
        QtTest.QTest.mouseClick(
            win.view_panel.view.vtk_widget, QtCore.Qt.MouseButton.LeftButton,
            QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(x, y),
        )
        app.processEvents()
        if not state["hits"] or state["hits"][-1] != new:
            fail(f"改名后拾取失败: {state['hits']}")
        else:
            print(f"    改名后仍能正确拾取 {new}", flush=True)
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
