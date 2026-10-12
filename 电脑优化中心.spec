# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules


# TaskbarSwitcher 的窗口排列和 UI Automation 依赖；comtypes 通过动态
# COM 调用，显式收集子模块以保证 onefile EXE 内可以正常枚举和激活窗口。
uiautomation_data, uiautomation_binaries, uiautomation_hidden = collect_all('uiautomation')
comtypes_hidden = [
    module for module in collect_submodules('comtypes')
    if not module.startswith('comtypes.test')
]
hiddenimports = uiautomation_hidden + comtypes_hidden

engine_exe = Path(SPECPATH) / 'flashback_engine' / 'bin' / 'ZJHUB.Recorder.Engine.exe'
if not engine_exe.is_file():
    raise RuntimeError('内置录制引擎尚未构建，请使用 python build_large.py 完整构建。')


a = Analysis(
    ['main_large.py'],
    pathex=[],
    binaries=uiautomation_binaries,
    # The engine contains its .NET runtime and all recorder JARs. Keep it as a
    # single data payload so PyInstaller does not scan/rewrite its own runtime.
    datas=[('app.ico', '.'), (str(engine_exe), 'flashback_engine')] + uiautomation_data,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # UI Automation 的截图转换方法才需要这些可选图像库；窗口枚举/切换
    # 不使用它们，排除后可避免把 Pillow 和 NumPy 一并塞入单文件 EXE。
    excludes=['numpy', 'PIL'],
    noarchive=False,
    optimize=0,
)

# Windows 自带这些 API-set / CRT 转发 DLL。避免 onefile 临时目录中的版本
# 遮蔽系统版本，保持 PySide6.QtCore 在不同 Windows 11 环境下都能启动。
a.binaries = [
    entry for entry in a.binaries
    if not os.path.basename(entry[0]).lower().startswith('api-ms-')
    and os.path.basename(entry[0]).lower() != 'ucrtbase.dll'
    and os.path.basename(entry[0]).lower() not in {
        'icuuc.dll', 'icudt78.dll', 'libcrypto-3-x64.dll', 'libssl-3-x64.dll',
    }
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ZJ HUB',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['app.ico'],
)
