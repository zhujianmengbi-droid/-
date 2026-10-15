# -*- coding: utf-8 -*-
"""构建可扩展的大 GUI Windows 可执行程序。"""
import shutil
import subprocess
import sys
import importlib
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = ROOT / 'flashback_engine'
ENGINE_EXE = ENGINE_ROOT / 'bin' / 'ZJHUB.Recorder.Engine.exe'


def check_build_dependencies():
    """Fail before PyInstaller when a runtime plugin was omitted from CI."""
    required = ('PySide6.QtCore', 'PySide6.QtWidgets', 'psutil', 'uiautomation', 'comtypes', 'PIL')
    missing = []
    for module_name in required:
        try:
            importlib.import_module(module_name)
        except Exception as error:
            missing.append(f'{module_name} ({error})')
    if missing:
        raise RuntimeError(
            '构建依赖缺失：' + ', '.join(missing) +
            '。请先执行 python -m pip install -r requirements.txt')


def build_recorder_engine():
    """Embed a self-contained recorder, independent of a desktop installation."""
    dotnet = shutil.which('dotnet')
    if not dotnet:
        raise RuntimeError('构建内置录制引擎需要 .NET 8 SDK，请安装后重试。运行成品不需要安装 .NET。')
    required = (
        'FlashbackRecorder.csproj', 'Program.cs', 'FlashbackZip.cs',
        'CaptureQuality.cs', 'RecordedSkins.cs', 'RecorderButton.cs',
        'RuntimeSupport.cs', 'ChildProcess.cs', 'NativeProcessModules.cs', 'JavaRuntimeProbe.cs', 'app.manifest',
        'runtime/runtime-recorder-cli.jar',
        'runtime/runtime-recorder-agent-v13.jar',
        'runtime/runtime-recorder-agent-1.21.10-v2.jar',
        'runtime/flashback-protocol-adapter-v6.jar',
    )
    missing = [name for name in required if not (ENGINE_ROOT / name).is_file()]
    if missing:
        raise RuntimeError('内置录制引擎源码或资源缺失：' + ', '.join(missing))
    subprocess.run([
        dotnet, 'publish', str(ENGINE_ROOT / 'FlashbackRecorder.csproj'),
        '--configuration', 'Release', '--runtime', 'win-x64',
        '--self-contained', 'true', '--output', str(ENGINE_EXE.parent),
        '-p:AssemblyName=ZJHUB.Recorder.Engine', '-p:PublishSingleFile=true',
        '-p:IncludeNativeLibrariesForSelfExtract=true',
        '-p:EnableCompressionInSingleFile=true', '-p:PublishTrimmed=false',
        '-p:DebugType=None', '-p:DebugSymbols=false',
    ], check=True, cwd=ENGINE_ROOT)
    if not ENGINE_EXE.is_file():
        raise RuntimeError(f'内置录制引擎构建未生成预期文件：{ENGINE_EXE}')


def main():
    check_build_dependencies()
    build_recorder_engine()
    subprocess.run([sys.executable, str(ROOT / 'make_icon.py')], check=True, cwd=ROOT)
    for folder in (ROOT / 'build', ROOT / 'dist'):
        if not folder.resolve().is_relative_to(ROOT.resolve()):
            raise RuntimeError('构建输出目录超出源码目录，已停止清理。')
        if folder.exists():
            shutil.rmtree(folder)
    # PyInstaller resolves native dependencies using PATH. A toolchain-provided
    # Poppler/Conda ICU can share Windows' icuuc.dll filename while exporting
    # only version-suffixed symbols. Bundling it breaks QtCore at startup even
    # though importing QtCore in the build interpreter succeeds. Restrict only
    # the packaging child to Python and Windows; the user's environment stays
    # untouched, and hooks still discover DLLs inside their Python packages.
    package_env = os.environ.copy()
    python_root = Path(sys.executable).resolve().parent
    windows_root = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    package_env['PATH'] = os.pathsep.join(str(path) for path in (
        python_root, python_root / 'DLLs', windows_root / 'System32', windows_root,
    ) if path.is_dir())
    for name in ('QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH', 'QML2_IMPORT_PATH'):
        package_env.pop(name, None)
    command = [
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
    ]
    # Optional tools need not be exposed through the DLL search PATH.
    upx_dir = os.environ.get('ZJ_HUB_UPX_DIR')
    if upx_dir:
        tool_dir = Path(upx_dir).expanduser().resolve()
        if not (tool_dir / 'upx.exe').is_file():
            raise RuntimeError('ZJ_HUB_UPX_DIR 目录中未找到 upx.exe。')
        command.extend(['--upx-dir', str(tool_dir)])
    command.append(str(ROOT / 'zj_hub.spec'))
    subprocess.run(command, check=True, cwd=ROOT, env=package_env)
    print(f'构建完成：{ROOT / "dist" / "ZJ HUB.exe"}')


if __name__ == '__main__':
    main()
