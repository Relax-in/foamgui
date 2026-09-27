"""程序入口: 创建 QApplication 与主窗口。"""

from __future__ import annotations

import sys

from .qtfix import ensure_qt_deps

# 必须在导入 PyQt6 之前处理 Qt 平台插件依赖(见 qtfix.py)
ensure_qt_deps()

from PyQt6 import QtCore, QtWidgets  # noqa: E402

from .ui.main_window import MainWindow  # noqa: E402

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    # 高 DPI 屏幕下按小数缩放
    try:
        QtWidgets.QApplication.setHighDpiScaleFactorRoundingPolicy(
            QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:  # pragma: no cover
        pass
    app = QtWidgets.QApplication(argv)
    app.setApplicationName("OpenFOAM 前处理助手")
    app.setApplicationDisplayName("OpenFOAM 前处理助手")
    app.setStyle("Fusion")

    win = MainWindow()
    win.show()
    for arg in argv[1:]:
        if not arg.startswith("-"):
            win.load_case(arg)
            break
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
