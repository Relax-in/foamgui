"""离屏渲染网格预览图(自检用, 不需要图形界面)。

用法::

    LIBGL_ALWAYS_SOFTWARE=1 QT_QPA_PLATFORM=offscreen \
        python -m foamgui.tests.render_preview <case_dir> <out_dir>
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("LIBGL_ALWAYS_SOFTWARE", "1")
os.environ.setdefault("GALLIUM_DRIVER", "llvmpipe")


def main(argv: list[str]) -> int:
    from foamgui.foam.case import FoamCase
    from foamgui.ui.mesh_scene import MeshScene

    from vtkmodules.vtkRenderingCore import vtkRenderWindow, vtkWindowToImageFilter
    from vtkmodules.vtkIOImage import vtkPNGWriter

    case_dir = argv[1] if len(argv) > 1 else "airFoil2D"
    out_dir = argv[2] if len(argv) > 2 else "_scratch"
    os.makedirs(out_dir, exist_ok=True)

    t0 = time.time()
    case = FoamCase(case_dir)
    case.load()
    mesh = case.load_mesh()
    print(f"[1] 读取网格完成 {time.time() - t0:.2f}s -> {mesh.summary()}", flush=True)

    t0 = time.time()
    scene = MeshScene()
    scene.set_mesh(mesh)
    print(f"[2] 建立 VTK 场景完成 {time.time() - t0:.2f}s", flush=True)

    rw = vtkRenderWindow()
    rw.SetOffScreenRendering(1)
    rw.AddRenderer(scene.renderer)
    rw.SetSize(1200, 900)

    def shot(name: str, direction: str) -> None:
        t = time.time()
        scene.reset_camera(direction)
        rw.Render()
        w2i = vtkWindowToImageFilter()
        w2i.SetInput(rw)
        w2i.Update()
        writer = vtkPNGWriter()
        path = os.path.abspath(os.path.join(out_dir, name))
        writer.SetFileName(path)
        writer.SetInputConnection(w2i.GetOutputPort())
        writer.Write()
        print(f"    保存 {path} ({os.path.getsize(path)} B, {time.time() - t:.2f}s)", flush=True)

    shot("preview_01_patches_z.png", "+z")
    shot("preview_02_patches_iso.png", "iso")

    for name in scene.patch_actors:
        scene.set_patch_visible(name, True)
    scene.set_edges_visible(True)
    shot("preview_03_wireframe.png", "+z")

    scene.set_edges_visible(False)
    scene.set_clip(True, "z", 0.5)
    shot("preview_04_clip.png", "iso")

    for name in scene.patch_actors:
        scene.set_patch_visible(name, name != "frontAndBack")
    scene.set_clip(False)
    scene.set_edges_visible(True)
    scene.apply_volume_colors()
    shot("preview_05_volume.png", "+z")
    print("[3] 完成", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
