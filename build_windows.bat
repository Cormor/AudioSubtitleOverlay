@echo off
setlocal
cd /d "%~dp0."

rem 不结束正在运行的安装版；安装更新使用并列版本目录。

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
