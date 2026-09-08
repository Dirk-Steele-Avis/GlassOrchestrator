@echo off
setlocal EnableExtensions

cd /d "%~dp0"

set "PYTHON_EXE=.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo ==================================================
  echo [TEST RUNNER] .venv not found. Creating virtual environment...
  echo ==================================================

  where py >nul 2>nul
  if "%ERRORLEVEL%"=="0" (
    py -3 -m venv .venv
  ) else (
    where python >nul 2>nul
    if not "%ERRORLEVEL%"=="0" (
      echo [TEST RUNNER] ERROR: Could not find Python launcher.
      exit /b 1
    )
    python -m venv .venv
  )

  if not exist "%PYTHON_EXE%" (
    echo [TEST RUNNER] ERROR: Failed to create .venv
    exit /b 1
  )
)

echo ==================================================
echo [TEST RUNNER] Installing dependencies...
echo ==================================================
"%PYTHON_EXE%" -m pip install -r requirements.txt
if not "%ERRORLEVEL%"=="0" (
  echo [TEST RUNNER] ERROR: dependency installation failed.
  exit /b 1
)

set "TEST_TARGETS=tests"
if not "%~1"=="" (
  set "TEST_TARGETS=%*"
)

echo ==================================================
echo [TEST RUNNER] START
echo Working dir : %CD%
echo Python      : %PYTHON_EXE%
echo Targets     : %TEST_TARGETS%
echo ==================================================

"%PYTHON_EXE%" -m pytest %TEST_TARGETS%
set "TEST_EXIT=%ERRORLEVEL%"

echo.
if "%TEST_EXIT%"=="0" (
  echo ==================================================
  echo [TEST RUNNER] PASS - all selected tests passed
  echo Exit code: %TEST_EXIT%
  echo ==================================================
) else (
  echo ==================================================
  echo [TEST RUNNER] FAIL - one or more tests failed
  echo Exit code: %TEST_EXIT%
  echo ==================================================
)

exit /b %TEST_EXIT%
