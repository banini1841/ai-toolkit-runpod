@echo off
REM Windows wrapper - runs the Python installer.
REM Usage: install.bat [--ai-toolkit C:\path\to\ai-toolkit]
python "%~dp0install.py" %*
