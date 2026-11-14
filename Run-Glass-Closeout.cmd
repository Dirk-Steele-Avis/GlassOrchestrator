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

if /i "%~1"=="--max" (
  if "%~2"=="" (
    echo [ERROR] --max requires a value.
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
  echo Usage: Run-Glass-Closeout.cmd [--max-invoices N ^| --max N] [--invoice-number N] [--mva MVA] [--csv path [--max-rows N]] [--sweep-terminal-to-needs-review]
  echo Default runs the full closeout for every eligible invoice: work-item closure, invoice validation, FieldPO PO approval, work-order closure, and email finalization.
  echo Invoice limits select once and restrict that same full flow to the selection.
  echo --mva and --csv close work items only and skip invoice validation, approval, and email moves.
  exit /b 0
)

echo [ERROR] Unknown argument: %~1
exit /b 2

:run
if defined MAX_INVOICES goto :scoped_closeout
if defined INVOICE_NUMBER goto :scoped_closeout
if "%SWEEP_TERMINAL%"=="1" goto :sweep_only
if /i "%MODE%"=="mva" goto :work_items_only
if defined CLOSE_CSV goto :work_items_only
if defined MAX_ROWS (
  echo [ERROR] --max-rows requires --csv.
  exit /b 2
)
echo Running full Glass closeout for every eligible invoice...
set "INVOICE_ARGS=--close-work-items"
goto :run_invoices

:scoped_closeout
if /i "%MODE%"=="mva" goto :incompatible_scope
if defined CLOSE_CSV goto :incompatible_scope
if defined MAX_ROWS goto :incompatible_scope
if "%SWEEP_TERMINAL%"=="1" goto :incompatible_scope
echo Running Glass closeout for one shared invoice selection...
set "INVOICE_ARGS=--close-work-items"
goto :run_invoices

:sweep_only
if /i "%MODE%"=="mva" goto :incompatible_scope
if defined CLOSE_CSV goto :incompatible_scope
if defined MAX_ROWS goto :incompatible_scope
echo Running terminal-invoice sweep to Needs Review...
set "INVOICE_ARGS="
goto :run_invoices

:work_items_only
if /i "%MODE%"=="mva" (
  if defined CLOSE_CSV goto :incompatible_scope
  if defined MAX_ROWS goto :incompatible_scope
  echo Closing Glass work item for MVA %TARGET_MVA%...
  call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --mvas "%TARGET_MVA%" --concise-console --results-json data\glass_closeout_work_items.json
  if errorlevel 1 goto :stage_failed
) else (
  echo Closing Glass work items from reviewed CSV %CLOSE_CSV%...
  if defined MAX_ROWS (
    call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --csv "%CLOSE_CSV%" --max-rows %MAX_ROWS% --concise-console --results-json data\glass_closeout_work_items.json
  ) else (
    call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe WorkItems\close_workitem.py --csv "%CLOSE_CSV%" --concise-console --results-json data\glass_closeout_work_items.json
  )
  if errorlevel 1 goto :stage_failed
)
echo Work-item closure complete. Invoice validation, FieldPO PO approval, and email
echo finalization were not run; run Run-Glass-Closeout.cmd to complete the closeout.
exit /b 0

:incompatible_scope
echo [ERROR] These closeout options cannot be combined; use one scope: --mva, --csv [--max-rows], --sweep-terminal-to-needs-review, or an invoice selection (--max-invoices / --invoice-number).
exit /b 2

:run_invoices
if defined MAX_INVOICES set "INVOICE_ARGS=%INVOICE_ARGS% --max-invoices %MAX_INVOICES%"
if defined INVOICE_NUMBER set "INVOICE_ARGS=%INVOICE_ARGS% --invoice-number %INVOICE_NUMBER%"
if "%SWEEP_TERMINAL%"=="1" set "INVOICE_ARGS=%INVOICE_ARGS% --sweep-terminal-to-needs-review"

call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe -m invoice_workflow.cli %INVOICE_ARGS% --work-item-results data\glass_closeout_work_items.json
set "RUN_EXIT=%errorlevel%"
echo.
echo Exit code: %RUN_EXIT%
exit /b %RUN_EXIT%

:stage_failed
exit /b %errorlevel%