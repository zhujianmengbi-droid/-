# -*- coding: utf-8 -*-
"""Archive only the sources and embedded assets required to rebuild ZJ HUB."""
import argparse
import ast
from pathlib import Path
import zipfile


ROOT = Path(__file__).resolve().parent
SOURCE_FILES = (
    'main_large.py', 'main.py', 'bugland_api.py',
    'match_data.py', 'optimizer.py', 'startup_motion.py',
    'taskbar_switcher_core.py', 'lobby_server.py',
    'companion_pack.py', 'companions/ZJ-HUB-modpack.zip', 'companions/README.md',
    'build_large.py', 'package_source.py', '电脑优化中心.spec',
    'make_icon.py', 'app.ico', 'zj_hub_icon.png',
    'requirements.txt', 'requirements-chat.txt', 'render.yaml',
    'README.md', 'README.txt',
    'flashback_engine/Program.cs', 'flashback_engine/FlashbackZip.cs',
    'flashback_engine/RecordedSkins.cs', 'flashback_engine/RecorderButton.cs',
    'flashback_engine/RuntimeSupport.cs', 'flashback_engine/app.manifest',
    'flashback_engine/FlashbackRecorder.csproj', 'flashback_engine/README.md',
    'flashback_engine/runtime/runtime-recorder-cli.jar',
    'flashback_engine/runtime/runtime-recorder-agent-v12.jar',
    'flashback_engine/runtime/flashback-protocol-adapter-v5.jar',
)


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
    paths = [(name, ROOT / name) for name in SOURCE_FILES]
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
