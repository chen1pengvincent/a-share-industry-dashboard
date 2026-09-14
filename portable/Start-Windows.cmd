@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 exit /b 1
if not exist "%~dp0runtime\python.exe" (
    echo ERROR: BUNDLED_PYTHON_UNAVAILABLE. Extract the complete Windows x64 package. 1>&2
    pause
    exit /b 1
)
"%~dp0runtime\python.exe" -I -B -X utf8 "%~dp0launcher.py" %*
set "SWIVD_EXIT=%ERRORLEVEL%"
if not "%SWIVD_EXIT%"=="0" pause
exit /b %SWIVD_EXIT%
