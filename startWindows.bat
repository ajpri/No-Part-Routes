@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto tryPython
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto tryPython
set "NPR_PYTHON=py -3"
goto launch
:tryPython
where python >nul 2>nul
if errorlevel 1 goto missingPython
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
if errorlevel 1 goto missingPython
set "NPR_PYTHON=python"
:launch
if not exist settings.ini copy /y settings.example.ini settings.ini >nul
if not exist apiKeys.ini copy /y apiKeys.example.ini apiKeys.ini >nul
if not exist locations.ini copy /y locations.example.ini locations.ini >nul
%NPR_PYTHON% no_part_routes.py %*
set "NPR_EXIT=%ERRORLEVEL%"
if "%NPR_EXIT%"=="0" exit /b 0
echo.
echo Check the configuration files and README.md, then try again.
pause
exit /b %NPR_EXIT%
:missingPython
echo Python 3.10 or later was not found. Install Python with its launcher or PATH option enabled.
pause
exit /b 1
