@echo off
chcp 65001 >nul
cd /d %~dp0
venv\Scripts\python.exe src\main.py
if errorlevel 1 pause
