@echo off
setlocal
cd /d "%~dp0."

rem 构建前只强制停止本应用，避免运行中的 EXE 锁定构建目录。
taskkill /F /T /IM AudioSubtitleOverlay.exe >nul 2>&1
rem 若旧应用以管理员权限运行，则使用同等权限停止；仍未停止时不得覆盖输出。
tasklist /FI "IMAGENAME eq AudioSubtitleOverlay.exe" /NH | findstr /I "AudioSubtitleOverlay.exe" >nul
if not errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -FilePath $env:WINDIR\System32\taskkill.exe -ArgumentList '/F /T /IM AudioSubtitleOverlay.exe' -Verb RunAs -Wait"
)
tasklist /FI "IMAGENAME eq AudioSubtitleOverlay.exe" /NH | findstr /I "AudioSubtitleOverlay.exe" >nul
if not errorlevel 1 (
    echo 本应用仍在运行，未覆盖构建目录。
    exit /b 1
)

rem 优先使用 Python 3.11；没有时使用 py 启动器当前的 Python 3 版本
py -3.11 --version >nul 2>&1
if errorlevel 1 (
    set "PYTHON_CMD=py -3"
) else (
    set "PYTHON_CMD=py -3.11"
)

rem 创建独立虚拟环境并安装 Windows 运行依赖
if not exist ".venv\Scripts\python.exe" (
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 (
        echo 创建 Python 虚拟环境失败。
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 (
    echo 更新 pip 失败。
    exit /b 1
)
python -m pip install -r requirements-windows.txt
if errorlevel 1 (
    echo 安装依赖失败。
    exit /b 1
)

rem 不把语音模型和NVIDIA用户态运行库放入主包，由内部组件下载器按配置准备。
rem 打包阶段使用本地依赖，不查询 Hub 在线列表或读取隐式令牌。
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_TELEMETRY=1"
set "HF_HUB_DISABLE_IMPLICIT_TOKEN=1"
python -m PyInstaller --noconfirm --clean --distpath dist_minimal --workpath build_minimal AudioSubtitleOverlay.spec
if errorlevel 1 (
    echo PyInstaller 构建失败。
    exit /b 1
)

echo 构建完成：dist_minimal\AudioSubtitleOverlay\AudioSubtitleOverlay.exe
endlocal
