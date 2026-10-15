param([string]$JavaHome = 'C:\Program Files\Java\jdk-21.0.10')
$ErrorActionPreference = 'Stop'
$build = Join-Path $PSScriptRoot 'build'
$classes = Join-Path $build 'classes'
New-Item -ItemType Directory -Path $classes -Force | Out-Null
$source = Join-Path $PSScriptRoot 'src\local\flashback\runtime\RuntimeRecorderCli.java'
& (Join-Path $JavaHome 'bin\javac.exe') --release 17 --add-modules jdk.attach -encoding UTF-8 -d $classes $source
if ($LASTEXITCODE -ne 0) { throw 'Recorder CLI compilation failed' }
$manifest = Join-Path $build 'MANIFEST.MF'
[IO.File]::WriteAllText($manifest, "Manifest-Version: 1.0`nMain-Class: local.flashback.runtime.RuntimeRecorderCli`n`n", [Text.UTF8Encoding]::new($false))
$staged = Join-Path $build 'runtime-recorder-cli.jar'
& (Join-Path $JavaHome 'bin\jar.exe') --create --file $staged --manifest $manifest -C $classes local
if ($LASTEXITCODE -ne 0) { throw 'Recorder CLI packaging failed' }
$output = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime\runtime-recorder-cli.jar'
Copy-Item -LiteralPath $staged -Destination $output -Force
Get-FileHash -LiteralPath $output -Algorithm SHA256
