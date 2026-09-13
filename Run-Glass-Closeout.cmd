@echo off
setlocal EnableExtensions EnableDelayedExpansion

cd /d "%~dp0"

set "MODE=batch"
set "TARGET_MVA="
set "CLOSE_CSV="
set "MAX_ROWS="
set "MAX_INVOICES="
set "INVOICE_NUMBER="
set "SWEEP_TERMINAL=0"

:parse_args
if "%~1"=="" goto :run

if /i "%~1"=="--batch" (
  set "MODE=batch"
  shift
  goto :parse_args
)

if /i "%~1"=="--mva" (
  if "%~2"=="" (
    echo [ERROR] --mva requires a value.
    exit /b 2
  )
  set "MODE=mva"
  set "TARGET_MVA=%~2"
  shift
  shift
  goto :parse_args
)

if /i "%~1"=="--csv" (
  if "%~2"=="" (
    echo [ERROR] --csv requires a path.
    exit /b 2
  )
  set "CLOSE_CSV=%~2"
  shift
  shift
  goto :parse_args
)

if /i "%~1"=="--max-rows" (
  if "%~2"=="" (
    echo [ERROR] --max-rows requires a value.
    exit /b 2
  )
  set "MAX_ROWS=%~2"
  shift
  shift
  goto :parse_args
)

if /i "%~1"=="--max-invoices" (
  if "%~2"=="" (
    echo [ERROR] --max-invoices requires a value.
    exit /b 2
  )
  set "MAX_INVOICES=%~2"
  shift
  shift
  goto :parse_args
)

if /i "%~1"=="--invoice-number" (
  if "%~2"=="" (
    echo [ERROR] --invoice-number requires a value.
    exit /b 2
  )
  set "INVOICE_NUMBER=%~2"
  shift
  shift
  goto :parse_args
)

if /i "%~1"=="--sweep-terminal-to-needs-review" (
  set "SWEEP_TERMINAL=1"
  shift
  goto :parse_args
)

if /i "%~1"=="--help" (
  echo Usage: Run-Glass-Closeout.cmd [--batch ^| --mva MVA] [--csv path] [--max-rows N] [--max-invoices N] [--invoice-number N] [--sweep-terminal-to-needs-review]
  exit /b 0
)

echo [ERROR] Unknown argument: %~1
exit /b 2

:run
if /i "%MODE%"=="mva" (
  echo Running Glass closeout for MVA %TARGET_MVA%...
  call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --mvas "%TARGET_MVA%"
  if errorlevel 1 exit /b %errorlevel%
) else (
  if defined CLOSE_CSV (
    echo Running Glass closeout from reviewed CSV %CLOSE_CSV%...
    if defined MAX_ROWS (
      call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --csv "%CLOSE_CSV%" --max-rows %MAX_ROWS%
    ) else (
      call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --csv "%CLOSE_CSV%"
    )
    if errorlevel 1 exit /b %errorlevel%
  ) else (
    echo Running Glass closeout batch...
    if defined MAX_ROWS (
      call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\build_close_queue.py --apply --max-rows %MAX_ROWS%
    ) else (
      call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\build_close_queue.py --apply
    )
    if errorlevel 1 exit /b %errorlevel%

    if defined MAX_ROWS (
      call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --max-rows %MAX_ROWS%
    ) else (
      call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py
    )
    if errorlevel 1 exit /b %errorlevel%
  )
)

set "INVOICE_ARGS="
if defined MAX_INVOICES set "INVOICE_ARGS=%INVOICE_ARGS% --max-invoices %MAX_INVOICES%"
if defined INVOICE_NUMBER set "INVOICE_ARGS=%INVOICE_ARGS% --invoice-number %INVOICE_NUMBER%"
if "%SWEEP_TERMINAL%"=="1" set "INVOICE_ARGS=%INVOICE_ARGS% --sweep-terminal-to-needs-review"

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe -m invoice_workflow.cli %INVOICE_ARGS%
set "RUN_EXIT=%errorlevel%"
echo.
echo Exit code: %RUN_EXIT%
exit /b %RUN_EXIT%