#!/usr/bin/env bash
# OpenFOAM 前处理助手 启动脚本
#
# 用法:
#   ./run_foamgui.sh                启动图形界面
#   ./run_foamgui.sh airFoil2D      启动并直接打开指定案例
#
# 脚本会自动:
#   1. 使用 ~/python/.venv 虚拟环境(也就是终端里 `py` 之后的那个环境);
#   2. 检查 PyQt6 / VTK;
#   3. 加载 OpenFOAM 的 etc/bashrc, 让界面里的校验/求解功能可用;
#   4. 隔离 Qt 运行环境(见第 4 节注释), 这是本机最容易踩的两个坑;
#   5. 启动 foamgui。

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------------------------------- #
# 1) 选择 Python 解释器
# --------------------------------------------------------------------------- #
PY="${FOAMGUI_PYTHON:-${HOME}/python/.venv/bin/python}"
if [[ ! -x "${PY}" ]]; then
    if command -v python3 >/dev/null 2>&1; then
        PY="$(command -v python3)"
        echo "[提示] 未找到 ~/python/.venv, 改用 ${PY}" >&2
    else
        echo "[错误] 找不到 Python 3 解释器。" >&2
        exit 1
    fi
fi

# --------------------------------------------------------------------------- #
# 2) 检查 PyQt6 / VTK, 并找到 PyQt6 自带的 Qt6 库目录
# --------------------------------------------------------------------------- #
if ! "${PY}" -c "import PyQt6" >/dev/null 2>&1; then
    echo "[错误] ${PY} 缺少 PyQt6, 请先安装: ${PY} -m pip install PyQt6" >&2
    exit 1
fi
if ! "${PY}" -c "import vtkmodules" >/dev/null 2>&1; then
    echo "[提示] 未安装 VTK, 三维网格显示将不可用(${PY} -m pip install vtk)。" >&2
fi

QT_LIB_DIR="$("${PY}" - <<'PYEOF' 2>/dev/null || true
import importlib.util, os
spec = importlib.util.find_spec("PyQt6")
if spec and spec.submodule_search_locations:
    p = os.path.join(list(spec.submodule_search_locations)[0], "Qt6", "lib")
    if os.path.isdir(p):
        print(p)
PYEOF
)"

PLUGIN_DIR=""
if [[ -n "${QT_LIB_DIR}" ]]; then
    PLUGIN_DIR="$(dirname "${QT_LIB_DIR}")/plugins/platforms"
fi

# --------------------------------------------------------------------------- #
# 3) 加载 OpenFOAM 环境(可选)
#
#    foamgui 自己也能找到 foamDictionary / foamRun, 这里先 source 一下更稳。
#    注意: OpenFOAM 的 bashrc 与 set -e / set -u 不兼容, 加载期间先关掉。
# --------------------------------------------------------------------------- #
if [[ -z "${WM_PROJECT_VERSION:-}" ]]; then
    for candidate in \
        "${HOME}"/OpenFOAM/OpenFOAM-13/etc/bashrc \
        "${HOME}"/OpenFOAM/*/etc/bashrc \
        "/opt/openfoam13/etc/bashrc" \
        "/usr/lib/openfoam/openfoam13/etc/bashrc"
    do
        if [[ -f "${candidate}" ]]; then
            set +e +u
            # shellcheck disable=SC1090
            source "${candidate}" >/dev/null 2>&1
            set -e -u
            if [[ -n "${WM_PROJECT_VERSION:-}" ]]; then
                echo "[信息] 已加载 OpenFOAM 环境: ${candidate} (v${WM_PROJECT_VERSION})" >&2
            fi
            break
        fi
    done
fi

# --------------------------------------------------------------------------- #
# 4) 隔离 Qt 运行环境(两件事)
#
#    (a) 系统 Qt6 串味:
#        OpenFOAM 的 bashrc 会把 /usr/lib/x86_64-linux-gnu 放进 LD_LIBRARY_PATH,
#        而 LD_LIBRARY_PATH 的优先级高于 PyQt6 自带的 RPATH, 于是系统那套 Qt6
#        (Mint 22.3 是 6.4.2)会盖住 PyQt6 自带的 Qt6(6.11), 报:
#          ImportError: /usr/lib/x86_64-linux-gnu/libQt6DBus.so.6:
#              undefined symbol: _ZN14QObjectPrivateC2Ei, version Qt_6_PRIVATE_API
#        做法: 把 LD_LIBRARY_PATH 里含 libQt6Core.so.6 的目录剔除(它们在
#        ld.so.cache 里本来就能找到, 剔掉不影响别的库), 再把 PyQt6 自带的
#        Qt6 目录放到最前面。
#
#    (b) 缺少 libxcb-cursor.so.0:
#        Qt 6.5 起 xcb 平台插件需要它, 系统默认没有。这里把项目自带的
#        vendor/lib 加进搜索路径兜底(装了系统包后可以删掉 vendor/)。
# --------------------------------------------------------------------------- #
if [[ -n "${QT_LIB_DIR}" ]]; then
    _kept=""
    IFS=':' read -r -a _dirs <<< "${LD_LIBRARY_PATH:-}"
    for _d in "${_dirs[@]}"; do
        [[ -z "${_d}" ]] && continue
        [[ "${_d}" == "${QT_LIB_DIR}" ]] && continue
        [[ "${_d}" == "${HERE}/vendor/lib" ]] && continue
        if [[ -e "${_d}/libQt6Core.so.6" ]]; then
            echo "[信息] 从 LD_LIBRARY_PATH 中剔除会与 PyQt6 冲突的 Qt6 目录: ${_d}" >&2
            continue
        fi
        _kept="${_kept:+${_kept}:}${_d}"
    done
    if [[ -d "${HERE}/vendor/lib" ]]; then
        export LD_LIBRARY_PATH="${QT_LIB_DIR}:${HERE}/vendor/lib${_kept:+:${_kept}}"
    else
        export LD_LIBRARY_PATH="${QT_LIB_DIR}${_kept:+:${_kept}}"
    fi
    unset _kept _dirs _d
fi

# xcb 平台插件依赖检查(上面已经把 vendor/lib 加进搜索路径)
if [[ -n "${PLUGIN_DIR}" && -f "${PLUGIN_DIR}/libqxcb.so" ]] && \
   ldd "${PLUGIN_DIR}/libqxcb.so" 2>/dev/null | grep -q "libxcb-cursor.so.0 => not found"; then
    cat >&2 <<'MSG'
[错误] Qt 的 xcb 平台插件缺少 libxcb-cursor.so.0, 图形界面无法启动。
       请执行任意一条:

         sudo apt install -y libxcb-cursor0          # Ubuntu / Linux Mint, 推荐

         QT_QPA_PLATFORM=offscreen ./run_foamgui.sh  # 无界面模式(不显示窗口)
MSG
    exit 1
fi

# 纯 Wayland 会话下 xcb 可能不可用, 改用 wayland 插件
if [[ -n "${WAYLAND_DISPLAY:-}" && -z "${DISPLAY:-}" ]]; then
    export QT_QPA_PLATFORM="${QT_QPA_PLATFORM:-wayland}"
fi

# --------------------------------------------------------------------------- #
# 5) 启动
# --------------------------------------------------------------------------- #
cd "${HERE}"
exec "${PY}" -m foamgui "$@"
