"""生成/预览/写出字典文件页。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

from ..foam import ofenv
from .widgets import mono_font

__all__ = ["OutputTab"]


class OutputTab(QtWidgets.QWidget):
    """预览将要写出的每个字典文件, 确认后再写盘。"""

    statusMessage = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.case = None
        self.files: dict[str, str] = {}

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        self.lbl_target = QtWidgets.QLabel("目标目录: -")
        self.lbl_target.setTextInteractionFlags(QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(self.lbl_target, 1)
        btn_refresh = QtWidgets.QPushButton("刷新预览")
        btn_refresh.clicked.connect(lambda: self.refresh(force=True))
        top.addWidget(btn_refresh)
        lay.addLayout(top)

        split = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        self.list = QtWidgets.QListWidget()
        self.list.setMinimumWidth(240)
        self.list.currentTextChanged.connect(self._show_selected)
        split.addWidget(self.list)

        self.text = QtWidgets.QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setFont(mono_font())
        self.text.setLineWrapMode(QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap)
        split.addWidget(self.text)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([260, 760])
        lay.addWidget(split, 1)

        bottom = QtWidgets.QHBoxLayout()
        self.lbl_summary = QtWidgets.QLabel("")
        bottom.addWidget(self.lbl_summary, 1)
        btn_validate = QtWidgets.QPushButton("用 foamDictionary 校验")
        btn_validate.setToolTip("把生成结果写到临时目录并用 OpenFOAM 自带工具解析一遍")
        btn_validate.clicked.connect(self._validate)
        btn_new = QtWidgets.QPushButton("另存到新目录…")
        btn_new.clicked.connect(self._write_as)
        btn_write = QtWidgets.QPushButton("写入案例目录")
        btn_write.setDefault(True)
        btn_write.clicked.connect(self._write)
        bottom.addWidget(btn_validate)
        bottom.addWidget(btn_new)
        bottom.addWidget(btn_write)
        lay.addLayout(bottom)

    # ------------------------------------------------------------------
    def set_case(self, case) -> None:
        self.case = case
        self.lbl_target.setText(f"目标目录: {case.root if case else '-'}")
        self.refresh(force=True)

    def mark_stale(self) -> None:
        self.lbl_summary.setText("设置已修改, 点“刷新预览”查看最新的字典内容")

    def refresh(self, force: bool = False) -> None:
        if self.case is None:
            self.files = {}
            self.list.clear()
            self.text.setPlainText("")
            return
        current = self.list.currentItem().text() if self.list.currentItem() else None
        self.files = self.case.render_all()
        self.list.blockSignals(True)
        self.list.clear()
        changed = 0
        for rel, text_ in self.files.items():
            disk = self.case.root / rel
            different = True
            if disk.exists():
                try:
                    different = disk.read_text(encoding="utf-8", errors="replace") != text_
                except OSError:
                    different = True
            if different:
                changed += 1
            item = QtWidgets.QListWidgetItem(("* " if different else "  ") + rel)
            item.setData(QtCore.Qt.ItemDataRole.UserRole, rel)
            if different:
                f = item.font()
                f.setBold(True)
                item.setFont(f)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.lbl_summary.setText(
            f"共 {len(self.files)} 个文件, 其中 {changed} 个与磁盘上不同(带 * 号)"
        )
        # 恢复选中
        target = 0
        for i in range(self.list.count()):
            if self.list.item(i).data(QtCore.Qt.ItemDataRole.UserRole) == current:
                target = i
                break
        if self.list.count():
            self.list.setCurrentRow(target)
        self.statusMessage.emit(f"已生成 {len(self.files)} 个字典文件预览")

    def _show_selected(self, _text: str) -> None:
        item = self.list.currentItem()
        if item is None:
            self.text.setPlainText("")
            return
        rel = item.data(QtCore.Qt.ItemDataRole.UserRole)
        self.text.setPlainText(self.files.get(rel, ""))

    # ------------------------------------------------------------------
    def _write(self) -> None:
        if self.case is None:
            return
        n = len(self.files)
        ok = QtWidgets.QMessageBox.question(
            self,
            "确认写入",
            f"将把 {n} 个字典文件写入:\n{self.case.root}\n\n"
            "原有文件会先备份到 foamgui_backup/<时间戳>/ 下。是否继续?",
        )
        if ok != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            written, backup = self.case.write(backup=True)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "写入失败", str(exc))
            return
        msg = f"已写入 {len(written)} 个文件。"
        if backup:
            msg += f"\n备份目录: {backup}"
        self.statusMessage.emit(msg.replace("\n", " "))
        QtWidgets.QMessageBox.information(self, "完成", msg)
        self.refresh(force=True)

    def _write_as(self) -> None:
        if self.case is None:
            return
        path = QtWidgets.QFileDialog.getExistingDirectory(self, "选择输出目录")
        if not path:
            return
        try:
            written, _ = self.case.write(out_dir=path, backup=False)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "写入失败", str(exc))
            return
        QtWidgets.QMessageBox.information(self, "完成", f"已写入 {len(written)} 个文件到\n{path}")
        self.statusMessage.emit(f"已另存 {len(written)} 个文件到 {path}")

    # ------------------------------------------------------------------
    def _validate(self) -> None:
        if self.case is None:
            return
        if not ofenv.foam_available():
            QtWidgets.QMessageBox.warning(
                self, "未找到 OpenFOAM", "没有找到 foamDictionary, 无法校验。\n请确认已安装 OpenFOAM 13。"
            )
            return
        self.refresh(force=True)
        tmp = Path(tempfile.mkdtemp(prefix="foamgui_check_"))
        lines: list[str] = []
        bad = 0
        try:
            for rel, text_ in self.files.items():
                p = tmp / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text_, encoding="utf-8")
                rc, out = ofenv.run_foam_tool(["foamDictionary", str(p), "-expand"], timeout=60)
                if rc == 0:
                    lines.append(f"[OK]   {rel}")
                else:
                    bad += 1
                    detail = "\n".join(out.strip().splitlines()[:6])
                    lines.append(f"[失败] {rel}\n{detail}")
        finally:
            import shutil

            shutil.rmtree(tmp, ignore_errors=True)
        title = "校验通过" if bad == 0 else f"有 {bad} 个文件解析失败"
        dlg = QtWidgets.QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(820, 520)
        v = QtWidgets.QVBoxLayout(dlg)
        te = QtWidgets.QPlainTextEdit("\n".join(lines))
        te.setReadOnly(True)
        te.setFont(mono_font())
        v.addWidget(te)
        b = QtWidgets.QPushButton("关闭")
        b.clicked.connect(dlg.accept)
        v.addWidget(b)
        dlg.exec()
        self.statusMessage.emit(title)
