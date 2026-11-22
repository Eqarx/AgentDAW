@echo off
setlocal
if exist "%~dp0runtime\python.exe" (
  "%~dp0runtime\python.exe" -B -X utf8 "%~dp0studio.py" %*
) else (
  python -B -X utf8 "%~dp0studio.py" %*
)
exit /b %errorlevel%
