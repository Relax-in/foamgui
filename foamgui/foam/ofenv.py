"""OpenFOAM 环境探测: 找到 OpenFOAM 自带的命令行工具并运行。

GUI 通常从桌面快捷方式启动, 环境里不一定有 ``$WM_PROJECT_DIR``,
所以这里既查 PATH, 也查常见的安装位置, 并用 ``bash -lc`` 先 source
OpenFOAM 的 bashrc 再执行, 保证 ``foamDictionary`` 等工具能正常运行。
"""

from __future__ import annotations

import glob
import os
import shlex
import shutil
import subprocess
from pathlib import Path

__all__ = ["find_foam_bin", "find_foam_bashrc", "run_foam_tool", "foam_available"]


def _candidates(name: str) -> list[str]:
    home = os.path.expanduser("~")
    patterns = [
        f"{home}/OpenFOAM/*/platforms/*/bin/{name}",
        f"{home}/openfoam/*/platforms/*/bin/{name}",
        f"/opt/openfoam*/platforms/*/bin/{name}",
        f"/usr/lib/openfoam/*/platforms/*/bin/{name}",
        f"/opt/OpenFOAM*/*/platforms/*/bin/{name}",
        f"{home}/OpenFOAM/OpenFOAM-*/platforms/*/bin/{name}",
    ]
    hits: list[str] = []
    for pat in patterns:
        hits.extend(sorted(glob.glob(pat)))
    return hits


def find_foam_bin(name: str) -> str | None:
    """查找 OpenFOAM 可执行文件。"""
    found = shutil.which(name)
    if found:
        return found
    hits = _candidates(name)
    return hits[-1] if hits else None


def find_foam_bashrc() -> str | None:
    """找到 OpenFOAM 的 etc/bashrc(用于补齐环境变量)。"""
    home = os.path.expanduser("~")
    patterns = [
        f"{home}/OpenFOAM/*/etc/bashrc",
        f"{home}/openfoam/*/etc/bashrc",
        "/opt/openfoam*/etc/bashrc",
        "/usr/lib/openfoam/*/etc/bashrc",
        "/opt/OpenFOAM*/*/etc/bashrc",
    ]
    hits: list[str] = []
    for pat in patterns:
        hits.extend(sorted(glob.glob(pat)))
    return hits[-1] if hits else None


def foam_available() -> bool:
    return find_foam_bin("foamDictionary") is not None or shutil.which("foamRun") is not None


def run_foam_tool(args: list[str], cwd: str | Path | None = None, timeout: float = 120.0):
    """运行 OpenFOAM 工具。

    返回 ``(returncode, 输出文本)``; 找不到工具时返回 ``(None, 提示)``。
    """
    exe = args[0]
    path = find_foam_bin(exe)
    if path is None:
        return None, f"未找到 OpenFOAM 工具 {exe}"

    env = dict(os.environ)
    bin_dir = os.path.dirname(path)
    if bin_dir not in env.get("PATH", "").split(os.pathsep):
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")

    # 如果环境里没有 WM_PROJECT_DIR, 先用 bashrc 补一下再执行
    if not env.get("WM_PROJECT_DIR"):
        bashrc = find_foam_bashrc()
        if bashrc:
            cmd = ["bash", "-c", f". {shlex.quote(bashrc)} >/dev/null 2>&1; exec " + " ".join(shlex.quote(a) for a in args)]
        else:
            cmd = [path] + [str(a) for a in args[1:]]
            env["LD_LIBRARY_PATH"] = os.path.dirname(bin_dir) + "/lib:" + env.get("LD_LIBRARY_PATH", "")
    else:
        cmd = [path] + [str(a) for a in args[1:]]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            text=True,
        )
        return proc.returncode, proc.stdout
    except subprocess.TimeoutExpired:
        return -1, f"{exe} 执行超时(>{timeout}s)"
    except OSError as exc:
        return -1, f"{exe} 无法执行: {exc}"
