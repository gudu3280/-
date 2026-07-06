@echo off
chcp 65001 >nul 2>&1
title 超星助手 - 安装与启动

set "SCRIPT_DIR=%~dp0"
set "DESKTOP_DIR=%SCRIPT_DIR%desktop"

:: 优先使用项目自带的虚拟环境，按优先级查找
if exist "%DESKTOP_DIR%\xue_xi_tong\Scripts\python.exe" (
    set "VENV_DIR=%DESKTOP_DIR%\xue_xi_tong"
    echo [OK] 使用项目环境: xue_xi_tong
) else if exist "%DESKTOP_DIR%\venv\Scripts\python.exe" (
    set "VENV_DIR=%DESKTOP_DIR%\venv"
    echo [OK] 使用虚拟环境: venv
) else (
    :: 检查系统 Python
    where python >nul 2>&1
    if %errorlevel% neq 0 (
        echo [错误] 未检测到 Python，请先安装 Python 3.9+
        echo 下载地址: https://www.python.org/downloads/
        echo 安装时请勾选 "Add Python to PATH"
        pause
        exit /b 1
    )

    echo [1/3] 创建虚拟环境...
    set "VENV_DIR=%DESKTOP_DIR%\venv"
    python -m venv "%VENV_DIR%"
    if %errorlevel% neq 0 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
    echo [OK] 虚拟环境已创建

    echo.
    echo [2/3] 安装依赖...
    "%VENV_DIR%\Scripts\python.exe" -m pip install -r "%DESKTOP_DIR%\requirements.txt" -q -i https://pypi.tuna.tsinghua.edu.cn/simple
    if %errorlevel% neq 0 (
        echo [警告] 部分依赖安装失败
    )
    echo [OK] 依赖安装完成
    goto :run_app
)

:: 已有环境，检查关键依赖是否齐全
echo [2/3] 检查依赖...
"%VENV_DIR%\Scripts\python.exe" -c "import PyQt5; import zendriver" >nul 2>&1
if %errorlevel% neq 0 (
    echo 正在补装依赖...
    "%VENV_DIR%\Scripts\python.exe" -m pip install -r "%DESKTOP_DIR%\requirements.txt" -q -i https://pypi.tuna.tsinghua.edu.cn/simple
)
echo [OK] 依赖就绪

:run_app
echo.
echo [3/3] 启动超星助手...
echo ==========================================
echo.
cd /d "%DESKTOP_DIR%"
"%VENV_DIR%\Scripts\python.exe" main.py

if %errorlevel% neq 0 (
    echo.
    echo [错误] 程序异常退出，错误码: %errorlevel%
    pause
)
