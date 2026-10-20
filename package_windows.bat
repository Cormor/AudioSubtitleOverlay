@echo off
setlocal
cd /d "%~dp0."
if not exist ".venv\Scripts\python.exe" (
    echo Please run build_windows.bat first.
    exit /b 1
)
if defined GITHUB_ACTIONS (
    ".venv\Scripts\python.exe" -X utf8 tools\package_and_install.py --package-only
) else (
    ".venv\Scripts\python.exe" -X utf8 tools\package_and_install.py
)
exit /b %errorlevel%
