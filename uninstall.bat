@echo off
REM Windows wrapper - runs the Python uninstaller.
REM Usage: uninstall.bat [--ai-toolkit C:\path\to\ai-toolkit]
python "%~dp0uninstall.py" %*
