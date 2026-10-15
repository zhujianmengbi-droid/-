"""Use ZJ HUB's real import implementation, then remove only its session cache."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from runtime_paths import SessionSkinCache


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('replay', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    original_hash = hashlib.sha256(args.replay.read_bytes()).hexdigest()
    with zipfile.ZipFile(args.replay) as z:
        meta = json.loads(z.read('metadata.json'))
        players = json.loads(z.read('recorded-skins.json'))['players']
    engine_root = ROOT / 'flashback_engine/bin/Release/net8.0-windows'
    engine = engine_root / 'ZJHUB.Recorder.Engine.dll'
    helper = ROOT / 'tests/ExportCompatibility/bin/Release/net8.0/ExportCompatibility.dll'
    with tempfile.TemporaryDirectory(prefix='zj-import-cycle-') as directory:
        fixture = Path(directory)
        def install(game, cache):
            (game / 'mods').mkdir(parents=True, exist_ok=True)
            (game / 'mods/flashback-test-marker.jar').write_bytes(b'fixture, never launched')
            result = subprocess.run(['dotnet', 'exec', '--runtimeconfig', str(engine_root / 'ZJHUB.Recorder.Engine.runtimeconfig.json'),
                str(helper), '--import', str(engine), str(args.replay.resolve()), str(game), str(cache.path())],
                text=True, encoding='utf-8', capture_output=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW)
            return json.loads(result.stdout)
        def installed_files(game):
            state_file = game / 'flashback/editor_states' / (meta['uuid'] + '.json')
            state = json.loads(state_file.read_text(encoding='utf-8-sig'))
            overrides = state['skinOverrideFromFile']
            assert set(overrides) == {p['uuid'] for p in players}
            files = [Path(value['pathToSkin']) for value in overrides.values()]
            assert all(path.parent == game / 'flashback/recorded_skins' and path.is_file() for path in files)
            return state_file, files
        first_game = fixture / '播放器 版本一'
        first_cache = SessionSkinCache([fixture / 'cache'])
        install(first_game, first_cache)
        state_file, files = installed_files(first_game)
        state_hash = hashlib.sha256(state_file.read_bytes()).hexdigest()
        image_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
        cache_path = first_cache.path()
        assert list(cache_path.glob('*.png'))
        first_cache.cleanup()
        assert not cache_path.exists() and all(path.is_file() for path in files)
        second_cache = SessionSkinCache([fixture / 'cache'])
        repeated = install(first_game, second_cache)
        assert repeated['skinInstall']['PreservedOverrides'] == len(players)
        assert repeated['skinInstall']['AddedOverrides'] == 0
        assert list(second_cache.path().glob('*.png'))
        second_cache.cleanup()
        assert hashlib.sha256(state_file.read_bytes()).hexdigest() == state_hash
        assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in image_hashes.items())
        second_game = fixture / '播放器 版本二'
        third_cache = SessionSkinCache([fixture / 'cache'])
        install(second_game, third_cache)
        _, second_files = installed_files(second_game)
        third_cache.cleanup()
        assert not set(files).intersection(second_files)
        assert hashlib.sha256(args.replay.read_bytes()).hexdigest() == original_hash
        report = {'ok': True, 'players': len(players), 'temporaryCacheRemoved': True,
            'playerAssetsSurviveCleanup': True, 'reimportRebuildsCache': True,
            'existingEditorStateUnchanged': True, 'playerVersionDirectoriesIndependent': True,
            'originalReplayUnchanged': True}
    args.report.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
