@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%~dp0src\launch.py" %*
    exit /b
)
where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0src\launch.py" %*
    exit /b
)
echo Python 3.10 or newer is required. Install Python and add it to PATH.
exit /b 1
