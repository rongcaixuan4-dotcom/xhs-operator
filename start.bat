@echo off
rem ============================================================
rem 「XHS 运营助手」Windows 一键启动脚本
rem 双击本文件：启动后端(8787) 并自动打开工作台页面
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"

echo 正在启动「XHS 运营助手」...
python run.py
if errorlevel 1 (
    echo.
    echo 启动失败，请检查：
    echo   1. 是否已安装 Python 3.11+ 并加入 PATH
    echo   2. 是否已执行 pip install -r backend/requirements.txt
    echo   3. 是否已执行 playwright install chromium
    pause
)
