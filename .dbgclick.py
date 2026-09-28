import sys, numpy as np
sys.path.insert(0, "/home/in/Agent/003")
from foamgui.qtfix import ensure_qt_deps
ensure_qt_deps()
from PyQt6 import QtWidgets, QtCore, QtTest
from foamgui.ui.main_window import MainWindow
app = QtWidgets.QApplication(sys.argv); app.setStyle("Fusion")
win = MainWindow(); win.resize(1400, 900); win.show()
hits = []
def go():
    win.load_case("111"); win.view_panel.cmb_projection.setCurrentIndex(0)
    win.view_panel.view.reset_view("iso"); win.view_panel.view.render()
    win.view_panel.view.patchClicked.connect(lambda n: hits.append(n))
    # 记录事件过滤器收到的坐标
    view = win.view_panel.view
    orig = view._to_vtk_coords
    def spy(pos):
        r = orig(pos)
        print(f"      [事件] Qt坐标=({pos.x():.0f},{pos.y():.0f}) -> VTK={r}", flush=True)
        return r
    view._to_vtk_coords = spy
    QtCore.QTimer.singleShot(2500, dbg)
def dbg():
    scene = win.view_panel.view.scene; w = win.view_panel.view.vtk_widget
    for name in ("Block.Block_Surface", "Block.inlet", "Block.outlet"):
        p = scene.mesh.patch_by_name(name)
        centres = scene.mesh.face_centres()[p.start_face:p.start_face+p.n_faces]
        pts = scene.mesh.points; cam = np.array(scene.renderer.GetActiveCamera().GetPosition())
        normals = np.empty_like(centres)
        for i, f in enumerate(scene.mesh.faces[p.start_face:p.start_face+p.n_faces]):
            p0,p1,p2 = pts[f[0]],pts[f[1]],pts[f[2]]
            n = np.cross(p1-p0,p2-p0); ln=np.linalg.norm(n); normals[i] = n/ln if ln>1e-30 else 0
        view_vec = cam - centres; view_vec /= np.linalg.norm(view_vec,axis=1,keepdims=True)+1e-30
        best = int(np.argmax(np.einsum("ij,ij->i", normals, view_vec)))
        c = centres[best]
        scene.renderer.SetWorldPoint(float(c[0]),float(c[1]),float(c[2]),1.0)
        scene.renderer.WorldToDisplay(); d = scene.renderer.GetDisplayPoint()
        x = int(round(d[0])); y = int(round(w.height()-d[1]-1))
        n_before = len(hits)
        ray = scene.pick_patch(x, y, snap_px=0)
        direct = scene.pick_patch(x, y)
        print(f"  {name:22s} 控件({x},{y}) 射线={ray} 直接pick={direct}", flush=True)
        QtTest.QTest.mouseClick(w, QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier, QtCore.QPoint(x,y))
        app.processEvents()
        print(f"     点击后 hits 增加 {len(hits)-n_before}, 最新={hits[-1] if hits else None}", flush=True)
    win._dirty=False; win.close(); app.quit()
QtCore.QTimer.singleShot(1200, go)
QtCore.QTimer.singleShot(60000, app.quit)
app.exec()
