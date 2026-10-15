"""Run the real hidden RecorderForm bridge with an isolated sleeping adapter fixture."""
import hashlib
import json
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
import psutil

source = Path(__file__).resolve().parents[2]
work = source / 'tests/BridgeLifecycle/bin/fixtures'
work.mkdir(parents=True, exist_ok=True)
tests = source / 'tests/ExportCompatibility/bin/Release/net8.0'
last = sorted(tests.glob('test-results-*/cancelled-adapter'), key=lambda p: p.stat().st_mtime)[-1]
fixture = work / f'bridge-close-fixture-{time.time_ns()}'
shutil.copytree(last, fixture)
raw = fixture / 'recording.fbrl'
before = hashlib.sha256(raw.read_bytes()).hexdigest()
settings = fixture / 'settings.json'
settings.write_text(json.dumps({'output': str(fixture), 'flashbackDirectory': '', 'autoZip': False}), encoding='utf-8')
helper = tests / 'ExportCompatibility.exe'
marker = last.parent / 'fixture-adapter.jar'
host = source / 'tests/BridgeLifecycle/bin/Release/net8.0-windows/BridgeLifecycle.exe'
messages = queue.Queue()
stderr = []
process = subprocess.Popen([str(host), str(helper), str(marker), str(settings)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    encoding='utf-8', errors='replace', creationflags=subprocess.CREATE_NO_WINDOW)
def read():
    for line in process.stdout:
        try: messages.put(json.loads(line))
        except ValueError: pass
threading.Thread(target=read, daemon=True).start()
threading.Thread(target=lambda: stderr.extend(process.stderr), daemon=True).start()
def send(row):
    process.stdin.write(json.dumps(row) + '\n'); process.stdin.flush()
owned_helper = None
try:
    send({'command': 'status'})
    first = messages.get(timeout=20)
    assert first.get('ok'), first
    engine_cache = Path(first['settings']['skinCacheDirectory'])
    process.stdin.write('[]\n'); process.stdin.flush()
    assert messages.get(timeout=3).get('ok') is False
    send({'command': 'export', 'source': str(raw)})
    deadline = time.monotonic() + 8
    pid_file = None
    while time.monotonic() < deadline:
        files = list(fixture.glob('.flashback-adapted-*.tmp-*/helper.pid'))
        if files:
            pid_file = files[0]; break
        if process.poll() is not None: break
        time.sleep(.05)
    assert pid_file, 'adapter did not start; responses=' + str(list(messages.queue)) + '; stderr=' + ''.join(stderr)
    owned_helper = psutil.Process(int(pid_file.read_text()))
    assert Path(owned_helper.exe()) == helper
    started = time.monotonic()
    send({'command': 'cancel_export'})
    send({'command': 'shutdown'})
    exported = messages.get(timeout=5)
    shutdown = messages.get(timeout=5)
    assert exported.get('command') == 'export' and exported.get('ok'), exported
    assert '取消' in exported.get('message', ''), exported
    assert shutdown.get('command') == 'shutdown' and shutdown.get('ok'), shutdown
    process.wait(timeout=5)
    assert process.returncode == 0, ''.join(stderr)
    assert not owned_helper.is_running(), 'cancelled helper survived'
    assert not engine_cache.exists(), 'engine temporary cache survived normal close'
    assert not list(fixture.glob('.flashback-adapted-*.tmp-*'))
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before
    report = {'ok': True, 'cancelAndShutdownSeconds': round(time.monotonic() - started, 3),
              'exportCancelled': True, 'helperTerminated': True, 'rawUnchanged': True,
              'partialConversionDirectories': 0, 'malformedRequestDidNotStopBridge': True,
              'engineSessionCacheRemoved': True,
              'scope': 'real hidden ZJ HUB bridge; isolated test adapter; no game attach, settings or gameplay changes'}
    (work / 'v1.6.20-bridge-close-verification.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
finally:
    if process.poll() is None:
        for child in psutil.Process(process.pid).children(recursive=True):
            try: child.kill()
            except psutil.NoSuchProcess: pass
        process.kill(); process.wait(timeout=5)
    if owned_helper is not None and owned_helper.is_running():
        owned_helper.kill(); owned_helper.wait(timeout=5)
