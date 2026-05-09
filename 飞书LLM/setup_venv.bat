@echo off
chcp 65001 >nul
echo ========================================
echo   飞书智能助手 - 虚拟环境一键安装
echo ========================================
echo.

REM 找到可用的 Python（优先 3.14，其次 3.13/3.11，最后用系统默认）
set PYTHON=
for %%p in (
    "C:\Python314\python.exe"
    "C:\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
) do (
    if exist %%p (
        if "%PYTHON%"=="" set PYTHON=%%~p
    )
)

REM 兜底：用 py launcher
if "%PYTHON%"=="" (
    where py >nul 2>&1
    if %errorlevel%==0 (
        set PYTHON=py -3
    )
)

if "%PYTHON%"=="" (
    echo [错误] 找不到 Python，请先安装 Python 3.11+
    echo 下载地址：https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [1/3] 使用 Python: %PYTHON%
%PYTHON% --version
echo.

REM 确定 venv 目录名（不覆盖用户已有的 .venv / .venv_local）
set VENV_DIR=.venv_local
echo [2/3] 创建虚拟环境: %VENV_DIR%
if exist "%VENV_DIR%\Scripts\python.exe" (
    echo       虚拟环境已存在，跳过创建
) else (
    %PYTHON% -m venv %VENV_DIR%
    if %errorlevel% neq 0 (
        echo [错误] 创建虚拟环境失败
        pause
        exit /b 1
    )
)

echo [3/3] 安装依赖...
call "%VENV_DIR%\Scripts\python.exe" -m pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   安装完成！
echo.
echo   启动方式：
echo     %VENV_DIR%\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
echo.
echo   或者先激活虚拟环境：
echo     %VENV_DIR%\Scripts\activate.bat
echo     python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
echo ========================================
pause
