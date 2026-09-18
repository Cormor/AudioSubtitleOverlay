@echo off
setlocal
cd /d "%~dp0."

if not exist "dist_minimal\AudioSubtitleOverlay\AudioSubtitleOverlay.exe" (
    echo 未找到构建结果，请先运行 build_windows.bat。
    exit /b 1
)

python package_minimal.py
if errorlevel 1 (
    echo 创建压缩包失败。
    exit /b 1
)

echo 压缩包创建完成。
endlocal
