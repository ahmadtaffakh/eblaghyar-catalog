@echo off
setlocal
cd /d "%~dp0"
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"

py -3.12 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3.12 -m eblaghyar_catalog.ui
    goto :finished
)

py -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -m eblaghyar_catalog.ui
    goto :finished
)

python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    python -m eblaghyar_catalog.ui
    goto :finished
)

echo Python 3.11 or newer was not found.
pause
exit /b 1

:finished
if errorlevel 1 (
    echo.
    echo Eblaghyar Catalog could not start.
    pause
    exit /b 1
)
endlocal
