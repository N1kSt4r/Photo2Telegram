@echo off
setlocal
where py >nul 2>nul
if not errorlevel 1 goto use_py
where python >nul 2>nul
if not errorlevel 1 goto use_python
echo Python 3.10 or newer is required. Install Python and add it to PATH.
exit /b 1

:use_py
py -3 "%~dp0src\launch.py" %*
exit /b %errorlevel%

:use_python
python "%~dp0src\launch.py" %*
exit /b %errorlevel%
