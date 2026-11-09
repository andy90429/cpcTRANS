@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Resolve an optional sample folder before switching to the project directory.
set "FUELCORE_CHECK_SAMPLES="
if not "%~1"=="" set "FUELCORE_CHECK_SAMPLES=%~f1"
pushd "%~dp0" || exit /b 1
set "FUELCORE_CHECK_ZIP=%CD%\dist\fuelcore.zip"

rem Use Unicode code points for the default folder so this batch file is ASCII.
powershell.exe -NoProfile -NonInteractive -Command ^
    "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
    "$samples = $env:FUELCORE_CHECK_SAMPLES;" ^
    "if (-not $samples) { $samples = [string][char]0x8CC7 + [char]0x6599 + '\txt' + [char]0x6A23 + [char]0x672C };" ^
    "$files = @(Get-ChildItem -LiteralPath $samples -Filter '*.txt' -File -Recurse);" ^
    "if (-not $files.Count) { throw 'No TXT samples found.' };" ^
    "$paths = @($files | ForEach-Object { $_.FullName });" ^
    "$temp = (New-Item -ItemType Directory -Path (Join-Path ([IO.Path]::GetTempPath()) ('fuelcore-check-' + [guid]::NewGuid().ToString('N')))).FullName;" ^
    "try {" ^
    "    [IO.Compression.ZipFile]::ExtractToDirectory($env:FUELCORE_CHECK_ZIP, $temp);" ^
    "    $bundle = Join-Path $temp 'fuelcore';" ^
    "    Push-Location -LiteralPath $bundle;" ^
    "    try {" ^
    "        Copy-Item -LiteralPath 'fuelcore.ini.example' -Destination 'fuelcore.ini';" ^
    "        & '.\fuelcore_cli.exe' schema --config 'fuelcore.ini';" ^
    "        if ($LASTEXITCODE -ne 0) { throw ('schema failed: ' + $LASTEXITCODE) };" ^
    "        & '.\fuelcore_cli.exe' convert @paths --config 'fuelcore.ini';" ^
    "        if ($LASTEXITCODE -ne 0) { throw ('convert failed: ' + $LASTEXITCODE) };" ^
    "    } finally { Pop-Location };" ^
    "} finally {" ^
    "    $resolved = (Resolve-Path -LiteralPath $temp).ProviderPath;" ^
    "    if ($resolved -ne $temp -or (Split-Path -Parent $resolved) -ne [IO.Path]::GetTempPath().TrimEnd('\')) { throw 'Unexpected temporary directory path.' };" ^
    "    Remove-Item -LiteralPath $resolved -Recurse -Force;" ^
    "}"
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" echo ERROR: fuelcore ZIP verification failed. 1>&2
popd
exit /b %RESULT%
