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
    datas=[('app.ico', '.'), (str(engine_exe), 'flashback_engine'),
           ('companions/ZJ-HUB-modpack.zip', 'companions')] + uiautomation_data,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Pillow 仍用于构建图标；删除液态玻璃后，运行时无需图像处理依赖。
    # UI Automation 的窗口枚举/切换也不使用可选截图转换库。
    excludes=['numpy', 'PIL'],
    noarchive=False,
    optimize=0,
)

# Windows 自带这些 API-set / CRT 转发 DLL。避免 onefile 临时目录中的版本
# 遮蔽系统版本，同时保留 OpenSSL 3 (libcrypto/libssl) 等非系统自带运行库，
# 确保全新 Windows 环境下的 HTTPS 请求、大厅 WebSocket 和 Qt 国际化组件正常可用。
a.binaries = [
    entry for entry in a.binaries
    if not os.path.basename(entry[0]).lower().startswith('api-ms-')
    and os.path.basename(entry[0]).lower() != 'ucrtbase.dll'
]

# This pinned Windows Qt build imports the OS ICU's unsuffixed entry points.
# Do not silently package a same-named Poppler/Conda ICU from the builder PATH.
# build_large.py isolates that search path; keep this check for direct spec use.
foreign_icu = [entry for entry in a.binaries
               if os.path.basename(entry[0]).lower() in {'icuuc.dll', 'icuin.dll', 'icudt.dll'}
               and not Path(entry[1]).resolve().is_relative_to(Path(os.environ.get('SystemRoot', r'C:\Windows')).resolve())]
if foreign_icu:
    raise RuntimeError('检测到与 Windows Qt 不兼容的外部 ICU；请使用 python build_large.py 隔离构建环境。')

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
