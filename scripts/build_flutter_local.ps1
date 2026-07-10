[CmdletBinding()]
param(
    [ValidateSet("windows", "android", "test", "all")]
    [string]$Target = "windows",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$AppRoot = Join-Path $RepoRoot "app"
$FlutterRoot = Join-Path $RepoRoot ".tooling\flutter"
$Flutter = Join-Path $FlutterRoot "bin\flutter.bat"
$Python = Join-Path $RepoRoot "venv\Scripts\python.exe"

function Invoke-NativeStep {
    param([string]$Label, [scriptblock]$Action)
    Write-Host "`n==> $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

if (-not (Test-Path -LiteralPath $Flutter -PathType Leaf)) {
    $Git = (Get-Command git -ErrorAction Stop).Source
    New-Item -ItemType Directory -Force -Path (Split-Path $FlutterRoot) | Out-Null
    Invoke-NativeStep "Install local Flutter stable SDK" {
        & $Git clone --depth 1 --branch stable https://github.com/flutter/flutter.git $FlutterRoot
    }
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Python = (Get-Command python -ErrorAction Stop).Source
}

$OriginalCC = $env:CC
$OriginalCXX = $env:CXX
try {
    # This machine may export CC=claude.exe. Flutter's Visual Studio generator
    # should discover MSVC itself instead of receiving that invalid compiler.
    Remove-Item Env:CC -ErrorAction SilentlyContinue
    Remove-Item Env:CXX -ErrorAction SilentlyContinue

    Push-Location $RepoRoot
    try {
        Invoke-NativeStep "Package bundled local system" {
            & $Python scripts\package_local_system.py `
                --out app\build\local-system `
                --zip app\assets\local-system.zip
        }
        Push-Location $AppRoot
        try {
            Invoke-NativeStep "Resolve Flutter packages" { & $Flutter pub get }
            if (-not $SkipTests) {
                Invoke-NativeStep "Analyze Flutter app" { & $Flutter analyze }
                Invoke-NativeStep "Run Flutter tests" { & $Flutter test }
            }
            if ($Target -in @("windows", "all")) {
                Invoke-NativeStep "Build Windows release" { & $Flutter build windows --release }
                $ReleaseRoot = (Resolve-Path "build\windows\x64\runner\Release").Path
                $PayloadTarget = Join-Path $ReleaseRoot "local-system"
                if (-not $PayloadTarget.StartsWith($ReleaseRoot, [StringComparison]::OrdinalIgnoreCase)) {
                    throw "unsafe local-system target"
                }
                if (Test-Path -LiteralPath $PayloadTarget) {
                    Remove-Item -LiteralPath $PayloadTarget -Recurse -Force
                }
                Copy-Item -LiteralPath "build\local-system" -Destination $PayloadTarget -Recurse
                Write-Host "Windows app: $(Join-Path $ReleaseRoot 'trilobite.exe')" -ForegroundColor Green
            }
            if ($Target -in @("android", "all")) {
                Invoke-NativeStep "Build Android release" { & $Flutter build apk --release }
                Write-Host "Android app: $AppRoot\build\app\outputs\flutter-apk\app-release.apk" -ForegroundColor Green
            }
        }
        finally {
            Pop-Location
        }
    }
    finally {
        Pop-Location
    }
}
finally {
    if ($null -eq $OriginalCC) { Remove-Item Env:CC -ErrorAction SilentlyContinue } else { $env:CC = $OriginalCC }
    if ($null -eq $OriginalCXX) { Remove-Item Env:CXX -ErrorAction SilentlyContinue } else { $env:CXX = $OriginalCXX }
}
