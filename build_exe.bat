@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1

set "VENV_PY=.venv_build\Scripts\python.exe"
set "STEP=Create the build virtual environment"
if not exist "%VENV_PY%" (
    if defined FUELCORE_BUILD_PYTHON (
        call "%FUELCORE_BUILD_PYTHON%" -m venv .venv_build || goto :fail
    ) else (
        where py.exe >nul 2>&1
        if errorlevel 1 (
            python.exe -m venv .venv_build || goto :fail
        ) else (
            py.exe -3 -m venv .venv_build || goto :fail
        )
    )
)

set "STEP=Check for 64-bit Python 3.11 or newer"
"%VENV_PY%" -c "import struct,sys; sys.exit(0 if sys.version_info >= (3,11) and struct.calcsize('P') == 8 else 1)" || goto :fail

set "STEP=Install PyInstaller and PyMySQL"
"%VENV_PY%" -m pip install "pyinstaller>=6,<7" "pymysql>=1,<2" || goto :fail

set "STEP=Build fuelcore.exe"
"%VENV_PY%" -m PyInstaller --noconfirm --clean --onedir --console --name fuelcore ^
    --paths "%CD%" --add-data "%CD%\fuelcore\schema.csv:fuelcore" ^
    --distpath dist --workpath build\pyinstaller --specpath build ^
    --python-option "X utf8" fuelcore\__main__.py || goto :fail

set "STEP=Copy sample configuration and README"
copy /y "fuelcore.ini.example" "dist\fuelcore\fuelcore.ini.example" >nul || goto :fail
copy /y "README.md" "dist\fuelcore\README.md" >nul || goto :fail

set "STEP=Create dist\fuelcore.zip"
powershell.exe -NoProfile -NonInteractive -Command ^
    "$ErrorActionPreference = 'Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem;" ^
    "$dist = (Resolve-Path -LiteralPath 'dist').ProviderPath;" ^
    "$zip = Join-Path $dist ('fuelcore-' + [guid]::NewGuid().ToString('N') + '.zip');" ^
    "try { [IO.Compression.ZipFile]::CreateFromDirectory((Join-Path $dist 'fuelcore'), $zip, 'Optimal', $true); Move-Item -LiteralPath $zip -Destination (Join-Path $dist 'fuelcore.zip') -Force }" ^
    "finally { if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip } }" || goto :fail

echo Built dist\fuelcore\fuelcore.exe and dist\fuelcore.zip
popd
exit /b 0

:fail
echo ERROR: %STEP% failed. 1>&2
popd
exit /b 1
