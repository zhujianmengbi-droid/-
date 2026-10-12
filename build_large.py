# -*- coding: utf-8 -*-
"""构建可扩展的大 GUI Windows 可执行程序。"""
import shutil
import subprocess
import sys
import importlib
from pathlib import Path


ROOT = Path(__file__).resolve().parent
ENGINE_ROOT = ROOT / 'flashback_engine'
ENGINE_EXE = ENGINE_ROOT / 'bin' / 'ZJHUB.Recorder.Engine.exe'


def check_build_dependencies():
    """Fail before PyInstaller when a runtime plugin was omitted from CI."""
    required = ('PySide6', 'psutil', 'uiautomation', 'comtypes', 'PIL')
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
        'RecordedSkins.cs', 'RecorderButton.cs', 'RuntimeSupport.cs', 'app.manifest',
        'runtime/runtime-recorder-cli.jar',
        'runtime/runtime-recorder-agent-v12.jar',
        'runtime/flashback-protocol-adapter-v5.jar',
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
        if folder.exists():
            shutil.rmtree(folder)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        '电脑优化中心.spec',
    ], check=True, cwd=ROOT)
    print(f'构建完成：{ROOT / "dist" / "ZJ HUB.exe"}')


if __name__ == '__main__':
    main()
