@echo off
setlocal
cd /d "%~dp0"
if errorlevel 1 exit /b 1

if exist ".venv" (
    ".venv\Scripts\python.exe" -B -c "import sys; sys.exit(not (sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15)))" >nul 2>&1
    if errorlevel 1 goto invalid_venv
    ".venv\Scripts\python.exe" -B run_dashboard.py serve %*
    exit /b
)

for %%V in (3.14 3.13 3.12 3.11) do (
    py -%%V -B -c "import sys; sys.exit(not (sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15)))" >nul 2>&1
    if not errorlevel 1 (
        py -%%V -B run_dashboard.py serve %*
        exit /b
    )
)
python -B -c "import sys; sys.exit(not (sys.implementation.name == 'cpython' and (3, 11) <= sys.version_info[:2] < (3, 15)))" >nul 2>&1
if errorlevel 1 goto missing_python
python -B run_dashboard.py serve %*
exit /b

:invalid_venv
echo The project .venv is unusable. Recreate it with Python 3.11-3.14 as described in README.md. 1>&2
exit /b 1

:missing_python
echo Python 3.11-3.14 is required. Create .venv and install requirements.lock as described in README.md. 1>&2
exit /b 1
