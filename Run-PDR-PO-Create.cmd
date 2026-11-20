@echo off
REM Create FieldPO work orders + POs from Maaco PDR estimates in data\pdr_estimates.
REM Default is a dry run. Add --live to create (asks before each WO and each PO).
cd /d "%~dp0"
call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe pdr_po_create.py %*
