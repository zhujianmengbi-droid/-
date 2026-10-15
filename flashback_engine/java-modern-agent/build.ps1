param(
    [Parameter(Mandatory = $true)][string]$InstanceRoot,
    [string]$JavaHome = $env:JAVA_HOME,
    [string]$AsmJar = '',
    [string]$OutputJar = ''
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
if (!$JavaHome -or !(Test-Path -LiteralPath (Join-Path $JavaHome 'bin\javac.exe'))) {
    throw 'Set JAVA_HOME or pass -JavaHome with a Java 21+ JDK.'
}
$classes = Join-Path $root 'build\classes'
New-Item -ItemType Directory -Path $classes -Force | Out-Null
$minecraft = Join-Path $InstanceRoot 'libraries\net\neoforged\minecraft-client-patched\21.10.64\minecraft-client-patched-21.10.64.jar'
$neo = Join-Path $InstanceRoot 'libraries\net\neoforged\neoforge\21.10.64\neoforge-21.10.64-universal.jar'
$asm = $AsmJar
if (!$asm) {
    $asmRoot = Join-Path $InstanceRoot 'libraries\org\ow2\asm\asm'
    $asm = Get-ChildItem -LiteralPath $asmRoot -Directory | Sort-Object { [version]$_.Name } -Descending |
        ForEach-Object { Join-Path $_.FullName ('asm-' + $_.Name + '.jar') } |
        Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (!$asm -or !(Test-Path -LiteralPath $asm)) { throw 'ASM JAR not found; pass -AsmJar.' }
$version = Get-Content -LiteralPath (Join-Path $InstanceRoot 'versions\1.21.10\1.21.10.json') -Encoding utf8 -Raw | ConvertFrom-Json
$deps = @($version.libraries | ForEach-Object {
    $relative = $_.downloads.artifact.path
    if (!$relative) {
        $parts = $_.name.Split(':')
        $classifier = if ($parts.Length -gt 3) { '-'+$parts[3] } else { '' }
        $relative = $parts[0].Replace('.','/')+'/'+$parts[1]+'/'+$parts[2]+'/'+$parts[1]+'-'+$parts[2]+$classifier+'.jar'
    }
    $candidate = Join-Path $InstanceRoot ('libraries/'+$relative)
    if (Test-Path -LiteralPath $candidate) { $candidate }
})
$cp = (@($minecraft, $neo, $asm) + $deps) -join ';'
$sources = @(Get-ChildItem -LiteralPath (Join-Path $root 'src') -Recurse -Filter '*.java' | Select-Object -ExpandProperty FullName)
$argfile = Join-Path $root 'build\javac.args'
$arguments = @('--release','21','-proc:none','-encoding','UTF-8','-cp',('"'+$cp.Replace('\','/')+'"'),'-d',('"'+$classes.Replace('\','/')+'"'))
$arguments += $sources | ForEach-Object { '"'+$_.Replace('\','/')+'"' }
[IO.File]::WriteAllLines($argfile, $arguments, [Text.UTF8Encoding]::new($false))
& (Join-Path $JavaHome 'bin\javac.exe') ('@'+$argfile)
if ($LASTEXITCODE -ne 0) { throw 'Modern agent javac failed' }
$stage = Join-Path $root 'build\asm'
New-Item -ItemType Directory -Path $stage -Force | Out-Null
Push-Location $stage
try { & (Join-Path $JavaHome 'bin\jar.exe') -xf $asm } finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'ASM extraction failed' }
$manifest = Join-Path $root 'build\MANIFEST.MF'
[IO.File]::WriteAllText($manifest, "Manifest-Version: 1.0`nAgent-Class: local.flashback.modern.AgentV2`nCan-Retransform-Classes: true`nCan-Redefine-Classes: false`n`n", [Text.UTF8Encoding]::new($false))
$output = if ($OutputJar) { $OutputJar } else { Join-Path $root '..\runtime\runtime-recorder-agent-1.21.10-v2.jar' }
& (Join-Path $JavaHome 'bin\jar.exe') --create --file $output --manifest $manifest -C $classes local -C $stage org
if ($LASTEXITCODE -ne 0) { throw 'Modern agent packaging failed' }
Get-FileHash -LiteralPath $output
