@echo off
setlocal
cd /d "%~dp0"

set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "PTH_FILE=%~dp0.venv\Lib\site-packages\zzz_glass_coverage_survey.pth"
set "RC_FILE=%~dp0.coveragerc"

if not exist "%VENV_PY%" (
  echo [ERROR] Missing venv. Run Run-Setup-GlassEnv.cmd first.
  exit /b 1
)

if "%~1"=="start"  goto start
if "%~1"=="stop"   goto stop
if "%~1"=="status" goto status
if "%~1"=="report" goto report

echo [ERROR] Explicit subcommand required.
echo Usage: Run-CoverageSurvey.cmd start   - enable usage recording for every venv Python process
echo        Run-CoverageSurvey.cmd stop    - disable recording, keep collected data
echo        Run-CoverageSurvey.cmd status  - show whether recording is enabled and data file count
echo        Run-CoverageSurvey.cmd report  - merge collected data and build the HTML report
exit /b 2

:start
"%VENV_PY%" -c "import coverage" 2>nul
if errorlevel 1 (
  echo [ERROR] coverage package is not installed in the venv.
  echo Run: .venv\Scripts\python.exe -m pip install coverage
  exit /b 1
)
if not exist "%RC_FILE%" (
  echo [ERROR] Missing .coveragerc survey configuration.
  exit /b 1
)
> "%PTH_FILE%" echo import os, coverage; os.environ.setdefault("COVERAGE_PROCESS_START", r"%RC_FILE%"); coverage.process_startup()
if not exist "%PTH_FILE%" (
  echo [ERROR] Could not write survey hook %PTH_FILE%
  exit /b 1
)
echo Survey ENABLED. Every Python process launched from the project venv now
echo records executed lines to .coverage.* files in the project root.
echo NOTE: running pytest while the survey is active counts test-exercised
echo code as "used". Avoid test runs during the survey window.
exit /b 0

:stop
if exist "%PTH_FILE%" del "%PTH_FILE%"
if exist "%PTH_FILE%" (
  echo [ERROR] Could not remove survey hook %PTH_FILE%
  exit /b 1
)
echo Survey DISABLED. Collected .coverage.* data files were kept.
echo Run "Run-CoverageSurvey.cmd report" to build the report.
exit /b 0

:status
if exist "%PTH_FILE%" (
  echo Survey is ENABLED.
) else (
  echo Survey is DISABLED.
)
set "DATA_COUNT=0"
for %%F in (".coverage.*") do set /a DATA_COUNT+=1
echo Collected data files: %DATA_COUNT%
exit /b 0

:report
"%VENV_PY%" coverage_survey_report.py
if errorlevel 1 (
  echo [ERROR] Script usage report generation failed.
  exit /b 1
)
"%VENV_PY%" -m coverage combine --keep
if not exist "%~dp0.coverage" (
  echo [ERROR] No survey data to report. Enable the survey and run the workflows first.
  exit /b 1
)
"%VENV_PY%" -m coverage html
if errorlevel 1 (
  echo [ERROR] HTML report generation failed.
  exit /b 1
)
"%VENV_PY%" -m coverage report
echo.
echo Script usage report: %~dp0reports\coverage_survey_report.html
echo HTML line report:    %~dp0htmlcov\index.html
echo Files near 0%% coverage were never executed during the survey window.
exit /b 0
