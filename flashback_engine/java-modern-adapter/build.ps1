param(
    [string]$Jdk = 'C:\Program Files\Java\jdk-21.0.10',
    [string]$BaseJar = '',
    [string]$OutputJar = ''
)
$ErrorActionPreference = 'Stop'
$engine = Split-Path -Parent $PSScriptRoot
if (!$BaseJar) {
    $BaseJar = Join-Path $engine 'runtime\flashback-protocol-adapter-v5.jar'
    if (!(Test-Path -LiteralPath $BaseJar)) { $BaseJar = Join-Path $engine 'runtime\flashback-protocol-adapter-v6.jar' }
}
if (!$OutputJar) { $OutputJar = Join-Path $engine 'runtime\flashback-protocol-adapter-v6.jar' }
$build = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Force -Path $build | Out-Null
$classes = Join-Path $build 'classes'
New-Item -ItemType Directory -Force -Path $classes | Out-Null
& (Join-Path $Jdk 'bin\javac.exe') --release 17 -encoding UTF-8 -cp $BaseJar -d $classes (Join-Path $PSScriptRoot 'ModernFlashbackProtocolAdapter.java')
if ($LASTEXITCODE -ne 0) { throw 'Modern adapter compilation failed' }
$staged = Join-Path $build 'flashback-protocol-adapter-v6.jar'
Copy-Item -LiteralPath $BaseJar -Destination $staged -Force
& (Join-Path $Jdk 'bin\jar.exe') --update --file $staged -C $classes .
if ($LASTEXITCODE -ne 0) { throw 'Modern adapter packaging failed' }
Copy-Item -LiteralPath $staged -Destination $OutputJar -Force
Get-FileHash -LiteralPath $OutputJar -Algorithm SHA256
