param([string]$JavaHome = 'C:\Program Files\Java\jdk-21.0.10')
$ErrorActionPreference = 'Stop'
$runtime = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime'
$cli = Join-Path $runtime 'runtime-recorder-cli.jar'
$classes = Join-Path $PSScriptRoot 'build\test-classes'
New-Item -ItemType Directory -Path $classes -Force | Out-Null
$source = Join-Path $PSScriptRoot 'tests\local\flashback\runtime\RuntimeRecorderCliSelfTest.java'
& (Join-Path $JavaHome 'bin\javac.exe') --release 17 -encoding UTF-8 -cp $cli -d $classes $source
if ($LASTEXITCODE -ne 0) { throw 'Recorder CLI test compilation failed' }
& (Join-Path $JavaHome 'bin\java.exe') --add-modules jdk.attach -cp "$classes;$cli" local.flashback.runtime.RuntimeRecorderCliSelfTest (Join-Path $PSScriptRoot 'build\selftest') $runtime
if ($LASTEXITCODE -ne 0) { throw 'Recorder CLI tests failed' }
