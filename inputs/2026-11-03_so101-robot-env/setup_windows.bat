@echo off
REM Build the SO-101 robot-side environment on Windows.
REM Requires Python 3.11 or 3.12 on PATH.
cd /d "%~dp0"

python --version
python -m venv .venv
if errorlevel 1 goto fail

call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
if errorlevel 1 goto fail

echo.
echo Installing calibration files for our arms (skip this if you use different hardware)...
set CAL=%USERPROFILE%\.cache\huggingface\lerobot\calibration
if not exist "%CAL%\robots\so_follower" mkdir "%CAL%\robots\so_follower"
if not exist "%CAL%\teleoperators\so_leader" mkdir "%CAL%\teleoperators\so_leader"
copy /Y calibration\so101_follower.json "%CAL%\robots\so_follower\" >nul
copy /Y calibration\so101_leader.json "%CAL%\teleoperators\so_leader\" >nul

echo.
echo ============================================
echo  Done. Next:
echo    .venv\Scripts\activate
echo    python robot\scan_motors.py COM9
echo    python robot\drive_arm_demo.py COM9
echo ============================================
pause
exit /b 0

:fail
echo.
echo Install failed - see the error above.
pause
exit /b 1
