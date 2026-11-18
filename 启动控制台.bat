@echo off
chcp 65001 >nul
cd /d %~dp0
python -m douban_wrapped ui
if errorlevel 1 pause
