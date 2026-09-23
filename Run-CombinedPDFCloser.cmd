@echo off
setlocal EnableExtensions

cd /d "%~dp0"
call ".\Run-GlassBootstrap.cmd" .\.venv\Scripts\python.exe outlook\FieldPO_Closer\CombinedPDFCloser.py %*
exit /b %errorlevel%