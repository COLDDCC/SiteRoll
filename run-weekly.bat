@echo off
cd /d "%~dp0"
if not exist data mkdir data
set PYTHONIOENCODING=utf-8
py -3 app.py run >> data\run.log 2>&1
