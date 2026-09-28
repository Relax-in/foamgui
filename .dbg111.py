import faulthandler, sys, os, resource
faulthandler.enable()
sys.path.insert(0, "/home/in/Agent/003")
from foamgui.qtfix import ensure_qt_deps
ensure_qt_deps()
from PyQt6 import QtWidgets, QtCore
from foamgui.ui.main_window import MainWindow
from foamgui.ui.mesh_scene import MeshScene

app = QtWidgets.QApplication(sys.argv); app.setStyle("Fusion")
win = MainWindow(); win.resize(1400, 900); win.show()

def mem(tag):
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(f"    [{tag}] 峰值内存 {kb/1024:.0f} MB", flush=True)

def go():
    mem("启动")
    win.load_case("111")
    mem("加载完成")
    win.view_panel.view.render()
    mem("首次渲染")
    QtCore.QTimer.singleShot(2500, dbg)

def dbg():
    scene = win.view_panel.view.scene
    w = win.view_panel.view.vtk_widget
    rw = win.view_panel.view.render_window
    print("  渲染窗口:", rw.GetSize(), "| tets:", scene.grid.GetNumberOfCells() if scene.grid else None, flush=True)
    for p in scene.mesh.patches:
        c = scene.mesh.face_centres()[p.start_face]
        scene.renderer.SetWorldPoint(float(c[0]), float(c[1]), float(c[2]), 1.0)
        scene.renderer.WorldToDisplay(); d = scene.renderer.GetDisplayPoint()
        x = int(d[0]); y = int(w.height() - d[1] - 1)
        scene._ensure_pick_cache()
        # 只做射线求交(不吸附)
        import numpy as np
        ren = scene.renderer
        ren.SetDisplayPoint(float(d[0]), float(d[1]), 0.0); ren.DisplayToWorld(); a = list(ren.GetWorldPoint())
        ren.SetDisplayPoint(float(d[0]), float(d[1]), 1.0); ren.DisplayToWorld(); b = list(ren.GetWorldPoint())
        o = np.array(a[:3]); dv = np.array(b[:3]) - o; dv /= np.linalg.norm(dv)
        hit_name, t = ray_hit(scene, o, dv)
        got = scene.pick_patch(x, y)
        print(f"    {p.name:24s} 面心世界={np.round(c,2)} 屏幕=({x},{y}) 射线命中={hit_name}(t={t:.2f}) pick={got}", flush=True)
    mem("拾取后")
    # 旋转相机再渲染, 看看是否崩
    scene.reset_camera("iso"); win.view_panel.view.render(); mem("等轴测渲染")
    scene.set_clip(True, "x", 0.5); win.view_panel.view.render(); mem("剖切渲染")
    win._dirty = False; win.close(); app.quit(); print("完成", flush=True)

def ray_hit(scene, o, dv):
    import numpy as np
    tris = scene._pick_tris
    if tris is None or not len(tris): return None, 0
    v0, v1, v2 = tris[:,0], tris[:,1], tris[:,2]
    e1 = v1-v0; e2 = v2-v0
    pv = np.cross(dv, e2); det = np.einsum("ij,ij->i", e1, pv)
    ok = np.abs(det) > 1e-14
    inv = np.zeros_like(det); inv[ok] = 1.0/det[ok]
    tv = o - v0; u = np.einsum("ij,ij->i", tv, pv)*inv
    qv = np.cross(tv, e1); v = np.einsum("j,ij->i", dv, qv)*inv
    t = np.einsum("ij,ij->i", e2, qv)*inv
    hit = ok & (u>=-1e-9) & (v>=-1e-9) & (u+v<=1+1e-9) & (t>1e-9)
    if not hit.any(): return None, 0
    i = int(np.argmin(np.where(hit, t, np.inf)))
    return scene._pick_names[i], float(t[i])

QtCore.QTimer.singleShot(1200, go)
QtCore.QTimer.singleShot(90000, app.quit)
app.exec()
