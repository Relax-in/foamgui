# vendor/lib — Qt xcb 平台插件的兜底依赖

## 这是什么

`vendor/lib/libxcb-cursor.so.0` 是从 Ubuntu noble(universe)的
`libxcb-cursor0 0.1.4-1build1` 包里抽出来的一个很小的系统库
(`libxcb-cursor.so.0.0.0` + 指向它的软链接 `libxcb-cursor.so.0`)。

## 为什么需要它

PyQt6 自带的 Qt 从 6.5 起, xcb 平台插件 `libqxcb.so` 多了一个依赖
`libxcb-cursor.so.0`, 而 Linux Mint / Ubuntu 默认不安装这个包, 于是启动时报:

```
qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed to load the Qt xcb platform plugin.
qt.qpa.plugin: Could not load the Qt platform plugin "xcb" in "" even though it was found.
This application failed to start because no Qt platform plugin could be initialized.
Aborted (core dumped)
```

## 本项目的处理方式

两条路都能自动兜底:

1. **`run_foamgui.sh`**：用 `ldd libqxcb.so` 检查是否缺这个库, 缺了就把
   `vendor/lib` 加进 `LD_LIBRARY_PATH`;
2. **`foamgui/qtfix.py`**：在 `import PyQt6` 之前用 `ctypes.CDLL(..., RTLD_GLOBAL)`
   预加载它(glibc 对已加载的同 SONAME 库会直接复用), 所以直接
   `python -m foamgui` 也能起来。

## 推荐做法: 装系统包, 然后删掉这个目录

```bash
sudo apt install -y libxcb-cursor0
rm -rf vendor/
```

装好之后脚本与 `qtfix.py` 都会发现系统库已存在, 自动不再使用这里的副本。
