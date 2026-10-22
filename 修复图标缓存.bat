@echo off
chcp 65001 >nul
title 修复 Windows 图标缓存
echo.
echo   本脚本会：清理图标缓存并重启资源管理器
echo   （不会删除任何程序文件，只影响图标显示）
echo.
echo   如果只是想换个图标显示，也可以把 exe 改名（如 EasyRealityAutoVJ2.exe），
echo   资源管理器会重新读取一次图标。
echo.
pause

echo 正在停止资源管理器...
taskkill /f /im explorer.exe >nul 2>&1
timeout /t 2 >nul

echo 正在清理图标缓存...
del /a /f /q "%LOCALAPPDATA%\IconCache.db" >nul 2>&1
del /a /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\iconcache*.db" >nul 2>&1
del /a /f /q "%LOCALAPPDATA%\Microsoft\Windows\Explorer\thumbcache*.db" >nul 2>&1

echo 正在重启资源管理器...
start explorer.exe
timeout /t 2 >nul

echo.
echo   完成。若仍显示旧图标，请注销再登录一次。
echo.
pause
