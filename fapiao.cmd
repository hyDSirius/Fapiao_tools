@echo off
"%~dp0.venv\Scripts\python.exe" -X utf8 "%~dp0fapiao.py" %*
exit /b %errorlevel%
