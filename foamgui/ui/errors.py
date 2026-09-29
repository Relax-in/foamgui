"""未处理异常的兜底: 让界面里的错误变成提示, 而不是整个程序闪退。

**为什么必须要有这个**
PyQt6 默认会把「Qt 槽函数里抛出的未捕获 Python 异常」升级成致命错误并直接
``abort()`` —— 表现就是用户看到的"点了某个控件, 窗口一下子全没了(闪退)",
而且终端里往往只留下一行 traceback 甚至什么都没有。

装一个自己的 ``sys.excepthook``(以及线程版)之后, 异常会被打印并以对话框
形式提示, 程序继续运行, 用户也能把错误信息发给我们。
"""

from __future__ import annotations

import sys
import traceback

__all__ = ["install_excepthook"]

_installed = False


def install_excepthook() -> None:
    """安装全局异常钩子(重复调用无副作用)。"""
    global _installed
    if _installed:
        return
    _installed = True

    def _hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        print(text, file=sys.stderr, flush=True)
        try:
            from PyQt6 import QtWidgets

            app = QtWidgets.QApplication.instance()
            if app is None:
                return
            box = QtWidgets.QMessageBox()
            box.setIcon(QtWidgets.QMessageBox.Icon.Warning)
            box.setWindowTitle("程序内部错误")
            box.setText("发生了一个未预期的错误, 程序会继续运行。")
            box.setInformativeText("可以把下面的内容发给我以便定位:")
            box.setDetailedText(text)
            box.exec()
        except Exception:  # pragma: no cover - 提示失败就只打印
            pass

    sys.excepthook = _hook

    try:  # 子线程里的异常也走同样处理
        import threading

        def _thread_hook(args) -> None:
            _hook(args.exc_type, args.exc_value, args.exc_traceback)

        threading.excepthook = _thread_hook
    except Exception:  # pragma: no cover
        pass
