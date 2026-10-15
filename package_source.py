# -*- coding: utf-8 -*-
"""Archive only the sources and embedded assets required to rebuild ZJ HUB."""
import argparse
import ast
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
SOURCE_FILES = (
    'main_large.py', 'main.py', 'onboarding.py', 'bugland_api.py',
    'match_data.py', 'optimizer.py', 'startup_motion.py', 'runtime_paths.py', 'new_pc_check.py',
    'ui_preferences.py', 'scroll_motion.py', 'ui_controls.py', 'theme_redesign.py',
    'taskbar_switcher_core.py', 'lobby_server.py', 'replay_catalog.py',
    'companion_pack.py', 'companions/ZJ-HUB-modpack.zip', 'companions/README.md',
    'build_large.py', 'package_source.py', 'zj_hub.spec',
    'make_icon.py', 'app.ico', 'zj_hub_icon.png',
    'requirements.txt', 'requirements-chat.txt', 'render.yaml',
    'README.md', 'README.txt',
    'flashback_engine/Program.cs', 'flashback_engine/FlashbackZip.cs',
    'flashback_engine/CaptureQuality.cs',
    'flashback_engine/RecordedSkins.cs', 'flashback_engine/RecorderButton.cs',
    'flashback_engine/RuntimeSupport.cs', 'flashback_engine/ChildProcess.cs',
    'flashback_engine/NativeProcessModules.cs', 'flashback_engine/app.manifest',
    'flashback_engine/JavaRuntimeProbe.cs',
    'flashback_engine/FlashbackRecorder.csproj', 'flashback_engine/README.md',
    'flashback_engine/runtime/runtime-recorder-cli.jar',
    'flashback_engine/runtime/runtime-recorder-agent-v13.jar',
    'flashback_engine/runtime/runtime-recorder-agent-1.21.10-v2.jar',
    'flashback_engine/runtime/flashback-protocol-adapter-v6.jar',
)

# Include the new runtime implementations and their focused checks, without
# compiler output, game files, recordings, or local dependency argument files.
SOURCE_TREES = (
    'flashback_engine/java-cli',
    'flashback_engine/java-modern-agent',
    'flashback_engine/java-modern-adapter',
    'tests/GameDiscovery',
    'tests/ExportCompatibility',
    'tests/LobbySecurity',
    'tests/BridgeLifecycle',
    'tests/NewComputer',
    'tests/UiPreferences',
)


def source_paths():
    paths = [(name, ROOT / name) for name in SOURCE_FILES]
    for directory in SOURCE_TREES:
        base = ROOT / directory
        if not base.is_dir():
            raise RuntimeError('缺少适配源码目录：' + directory)
        for path in sorted(base.rglob('*')):
            relative = path.relative_to(base)
            if any(part in {'bin', 'obj', 'build', '__pycache__'} for part in relative.parts):
                continue
            if path.is_file() and path.suffix in {'.java', '.ps1', '.cs', '.csproj', '.py', '.md'}:
                paths.append((path.relative_to(ROOT).as_posix(), path))
    return paths


def app_version():
    """Read the release version without importing Qt or application settings."""
    tree = ast.parse((ROOT / 'main_large.py').read_text(encoding='utf-8-sig'))
    for node in tree.body:
        is_version = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == 'APP_VERSION'
            for target in node.targets
        )
        is_version = is_version or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == 'APP_VERSION'
        )
        if is_version:
            version = ast.literal_eval(node.value)
            if isinstance(version, str) and version and all(
                character.isascii() and (character.isalnum() or character in '.-_')
                for character in version
            ):
                return version
    raise RuntimeError('main_large.py 中缺少有效的 APP_VERSION。')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='源码 ZIP 输出路径')
    parser.add_argument('--version', action='store_true', help='只输出 APP_VERSION')
    args = parser.parse_args()
    version = app_version()
    if args.version:
        print(version)
        return
    paths = source_paths()
    missing = [name for name, path in paths if not path.is_file()]
    if missing:
        raise RuntimeError('无法生成完整源码包，缺少：' + ', '.join(missing))
    output = args.output or ROOT / 'dist' / f'ZJ-HUB-v{version}-source.zip'
    output = output.resolve()
    if output in (path.resolve() for _, path in paths):
        raise ValueError('源码包输出不能覆盖构建所需文件。')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, path in paths:
            archive.write(path, name)
    print(f'源码包：{output}')


if __name__ == '__main__':
    main()
