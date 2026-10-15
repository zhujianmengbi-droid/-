param([string]$Jdk = 'C:\Program Files\Java\jdk-21.0.10')
$ErrorActionPreference = 'Stop'
$adapter = Join-Path (Split-Path -Parent $PSScriptRoot) 'runtime\flashback-protocol-adapter-v6.jar'
$testClasses = Join-Path $PSScriptRoot 'build\test-classes'
New-Item -ItemType Directory -Force -Path $testClasses | Out-Null
& (Join-Path $Jdk 'bin\javac.exe') --release 17 -encoding UTF-8 -cp $adapter -d $testClasses (Join-Path $PSScriptRoot 'ModernAdapterSelfTest.java')
if ($LASTEXITCODE -ne 0) { throw 'Modern adapter test compilation failed' }
& (Join-Path $Jdk 'bin\java.exe') -cp "$testClasses;$adapter" ModernAdapterSelfTest (Join-Path $PSScriptRoot 'build\selftest')
if ($LASTEXITCODE -ne 0) { throw 'Modern adapter tests failed' }
