import sys
sys.path.insert(0, "/home/in/Agent/003")
from foamgui.qtfix import ensure_qt_deps
ensure_qt_deps()
from PyQt6 import QtWidgets, QtCore
from foamgui.ui.main_window import MainWindow
app = QtWidgets.QApplication(sys.argv); app.setStyle("Fusion")
win = MainWindow(); win.resize(1400, 900); win.show()
def go():
    win.load_case("111"); win.view_panel.view.reset_view("iso"); win.view_panel.view.render()
    QtCore.QTimer.singleShot(2500, dbg)
def dbg():
    scene = win.view_panel.view.scene; w = win.view_panel.view.vtk_widget
    print("控件高:", w.height(), "宽:", w.width(), "| 渲染窗口:", win.view_panel.view.render_window.GetSize(),
          "| dpr:", w.devicePixelRatio(), flush=True)
    c = scene.mesh.face_centres()[scene.mesh.patch_by_name("Block.outlet").start_face]
    scene.renderer.SetWorldPoint(float(c[0]), float(c[1]), float(c[2]), 1.0)
    scene.renderer.WorldToDisplay(); d = scene.renderer.GetDisplayPoint()
    print("显示坐标 d =", d, flush=True)
    x_qt = int(round(d[0])); y_qt = int(round(w.height() - d[1] - 1))
    print("换算到控件:", (x_qt, y_qt), flush=True)
    # 用 eventFilter 同样的函数反算
    from PyQt6.QtCore import QPointF
    back = win.view_panel.view._to_vtk_coords(QPointF(x_qt, y_qt))
    print("再换算回 VTK:", back, "(应约等于 d 的整数部分)", flush=True)
    print("pick(显示坐标):", scene.pick_patch(int(d[0]), int(d[1])), flush=True)
    print("pick(回算坐标):", scene.pick_patch(back[0], back[1]), flush=True)
    win._dirty=False; win.close(); app.quit()
QtCore.QTimer.singleShot(1200, go)
QtCore.QTimer.singleShot(60000, app.quit)
app.exec()
