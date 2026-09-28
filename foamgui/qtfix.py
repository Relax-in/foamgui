"""Qt 运行环境的自动修复(必须在 import PyQt6 之前调用)。

本文件解决两个在 Linux Mint / Ubuntu 上很常见的坑:

坑 1: 缺少 libxcb-cursor.so.0
------------------------------------------------------------------
Qt 6.5 起, xcb 平台插件 `libqxcb.so` 多了一个依赖 `libxcb-cursor.so.0`,
系统默认不装, 于是启动时报::

    qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed ...
    qt.qpa.plugin: Could not load the Qt platform plugin "xcb" in "" ...
    This application failed to start because no Qt platform plugin could be initialized.

坑 2: 系统 Qt6 与 PyQt6 自带的 Qt6 混用(更隐蔽)
------------------------------------------------------------------
如果 `LD_LIBRARY_PATH` 里含有别的 Qt6(典型来源: 在 `~/.bashrc` 里
`source .../OpenFOAM-13/etc/bashrc`, 它会把 `/usr/lib/x86_64-linux-gnu`
加进 `LD_LIBRARY_PATH`), 由于 `LD_LIBRARY_PATH` 的优先级高于 PyQt6 自带的
RPATH, 加载到的会是系统那套 Qt6(例如 6.4.2), 与 PyQt6 自带的 Qt6(例如 6.11)
版本不一致, 报::

    ImportError: /usr/lib/x86_64-linux-gnu/libQt6DBus.so.6:
        undefined symbol: _ZN14QObjectPrivateC2Ei, version Qt_6_PRIVATE_API

这里的做法是: 一旦发现 `LD_LIBRARY_PATH` 里混进了别的 Qt6, 就把那些目录剔除,
把 PyQt6 自带的 Qt 目录放到最前面, 然后**带着干净的环境重新 exec 自己**。
这样不管是 `./run_foamgui.sh` 还是直接 `python -m foamgui` 都能正常启动。
"""

from __future__ import annotations

import ctypes
import glob
import importlib.util
import os
import sys
from pathlib import Path

__all__ = ["ensure_qt_deps", "xcb_cursor_available", "bundled_qt_lib_dir"]

_LIB = "libxcb-cursor.so.0"
_FIXED_FLAG = "FOAMGUI_QT_ENV_FIXED"

_state: dict[str, object] = {}


# ---------------------------------------------------------------------------
# 路径工具
# ---------------------------------------------------------------------------
def _pyqt6_dir() -> Path | None:
    """不导入 PyQt6, 只定位它的安装目录。"""
    try:
        spec = importlib.util.find_spec("PyQt6")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.submodule_search_locations:
        return None
    return Path(list(spec.submodule_search_locations)[0])


def bundled_qt_lib_dir() -> Path | None:
    """PyQt6 自带的 Qt6 库目录(.../PyQt6/Qt6/lib)。"""
    if "qt_lib" in _state:
        return _state["qt_lib"]  # type: ignore[return-value]
    d = _pyqt6_dir()
    out = (d / "Qt6" / "lib") if d else None
    _state["qt_lib"] = out if (out and out.is_dir()) else None
    return _state["qt_lib"]  # type: ignore[return-value]


def _vendor_lib_dir() -> Path | None:
    d = Path(__file__).resolve().parent.parent / "vendor" / "lib"
    return d if d.is_dir() else None


def _dir_has_other_qt6(d: str) -> bool:
    """该目录里是否有别的 Qt6 运行库(会和 PyQt6 自带的打架)。"""
    if not d:
        return False
    qt_lib = bundled_qt_lib_dir()
    if qt_lib is not None and os.path.realpath(d) == os.path.realpath(str(qt_lib)):
        return False
    return os.path.exists(os.path.join(d, "libQt6Core.so.6"))


# ---------------------------------------------------------------------------
# 坑 2: 环境里混进了别的 Qt6 -> 干净重启
# ---------------------------------------------------------------------------
def _conflicting_qt_dirs() -> list[str]:
    ld = os.environ.get("LD_LIBRARY_PATH", "")
    return [d for d in ld.split(os.pathsep) if _dir_has_other_qt6(d)]


def _reexec_with_clean_env(verbose: bool) -> None:
    """把污染 LD_LIBRARY_PATH 的目录剔除, 把 PyQt6 的 Qt6 放最前, 重启进程。"""
    qt_lib = bundled_qt_lib_dir()
    parts: list[str] = []
    if qt_lib is not None:
        parts.append(str(qt_lib))
    vendor = _vendor_lib_dir()
    if vendor is not None:
        parts.append(str(vendor))
    kept = [
        d
        for d in os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep)
        if d and not _dir_has_other_qt6(d) and not (qt_lib and d == str(qt_lib)) and not (vendor and d == str(vendor))
    ]
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(parts + kept)
    env[_FIXED_FLAG] = "1"
    if verbose:
        print(
            "[信息] 检测到 LD_LIBRARY_PATH 里混入了系统 Qt6(常见原因: 终端里 source 过 "
            "OpenFOAM 的 etc/bashrc),\n"
            "       为避免与 PyQt6 自带的 Qt6 版本冲突, 已用干净的库搜索路径重新启动界面。",
            file=sys.stderr,
        )
    # 尽量保持原来的启动方式:
    #   python -m foamgui ...        -> -m foamgui
    #   python -m 包.模块 ...         -> 原样 -m 包.模块
    #   python 脚本.py ... / -c ...   -> 原样
    main_mod = sys.modules.get("__main__")
    spec = getattr(main_mod, "__spec__", None)
    mod_name = getattr(spec, "name", None) if spec is not None else None
    if mod_name and mod_name != "__main__":
        argv = [sys.executable, "-m", mod_name] + list(sys.argv[1:])
    else:
        argv = [sys.executable] + list(sys.argv)
    os.execve(sys.executable, argv, env)


# ---------------------------------------------------------------------------
# 坑 1: libxcb-cursor
# ---------------------------------------------------------------------------
def xcb_cursor_available() -> bool:
    """当前进程能否加载 libxcb-cursor.so.0。"""
    if "system" in _state:
        return bool(_state["system"])
    try:
        ctypes.CDLL(_LIB, mode=ctypes.RTLD_GLOBAL)
        ok = True
    except OSError:
        ok = False
    _state["system"] = ok
    return ok


def _preload_vendored_cursor() -> bool:
    if "vendor" in _state:
        return bool(_state["vendor"])
    vendor = _vendor_lib_dir()
    ok = False
    if vendor is not None:
        for name in (f"{vendor}/{_LIB}", f"{vendor}/libxcb-cursor.so.0.0.0"):
            if os.path.exists(name):
                try:
                    ctypes.CDLL(name, mode=ctypes.RTLD_GLOBAL)
                    ok = True
                    break
                except OSError:
                    ok = False
    _state["vendor"] = ok
    return ok


# ---------------------------------------------------------------------------
# 平台插件选择
# ---------------------------------------------------------------------------
def _maybe_pick_platform() -> None:
    """纯 Wayland 会话下, 用户没指定平台插件时用 wayland。"""
    if os.environ.get("QT_QPA_PLATFORM"):
        return
    if not os.environ.get("DISPLAY") and os.environ.get("WAYLAND_DISPLAY"):
        os.environ["QT_QPA_PLATFORM"] = "wayland"


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def ensure_qt_deps(verbose: bool = True) -> bool:
    """确保 Qt 能正常加载平台插件。返回是否(看起来)没问题。"""
    _maybe_pick_platform()

    if not sys.platform.startswith("linux"):
        return True

    # 1) 环境里混进了别的 Qt6 -> 干净重启(该函数不会返回)
    #    注意: 这一步和平台插件无关, 因为混用会在 import PyQt6 时就炸
    if os.environ.get(_FIXED_FLAG) != "1" and bundled_qt_lib_dir() is not None:
        if _conflicting_qt_dirs():
            _reexec_with_clean_env(verbose)

    # 2) libxcb-cursor(只有 xcb 平台插件需要)
    platform = os.environ.get("QT_QPA_PLATFORM", "")
    if platform.startswith(("offscreen", "minimal", "vnc", "linuxfb")):
        return True
    if xcb_cursor_available():
        return True
    if _preload_vendored_cursor():
        if verbose:
            print(
                "[信息] 系统缺少 libxcb-cursor.so.0, 已使用项目自带的 vendor/lib 兜底。\n"
                "       永久修复(装完可删掉 vendor/): sudo apt install -y libxcb-cursor0",
                file=sys.stderr,
            )
        return True
    if verbose:
        print(
            "[警告] Qt 的 xcb 平台插件缺少 libxcb-cursor.so.0, 界面可能无法启动。\n"
            "       请执行: sudo apt install -y libxcb-cursor0\n"
            "       (或使用无界面模式: QT_QPA_PLATFORM=offscreen python -m foamgui)",
            file=sys.stderr,
        )
    return False
