@echo off
chcp 65001 >nul
REM ============================================================
REM  WinCountdown 单文件 exe 打包脚本
REM  用法: 双击 build.bat 或在命令行执行 build.bat
REM  产物: dist\WinCountdown.exe
REM ============================================================

echo.
echo === WinCountdown 打包工具 ===
echo.

REM 检查 PyInstaller 是否已安装
python -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo [信息] 未检测到 PyInstaller，正在安装...
    pip install pyinstaller || (
        echo [错误] PyInstaller 安装失败
        pause
        exit /b 1
    )
)

echo [信息] 开始打包（单文件模式，无控制台窗口）...
echo.

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --noconfirm ^
    --clean ^
    --name WinCountdown ^
    --collect-submodules PIL ^
    floating_ball.py

if errorlevel 1 (
    echo.
    echo [错误] 打包失败，请检查上方错误信息
    pause
    exit /b 1
)

echo.
echo === 打包完成 ===
echo 产物位置: dist\WinCountdown.exe
echo.
echo 可以直接双击 dist\WinCountdown.exe 运行，
echo 也可拷贝到任意 Windows 电脑使用（无需安装 Python）。
echo.
pause
