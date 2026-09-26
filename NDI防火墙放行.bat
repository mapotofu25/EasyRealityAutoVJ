@echo off
chcp 65001 >nul
title NDI 防火墙放行
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   【请右键本文件，选择「以管理员身份运行」】
    echo.
    pause
    exit /b 1
)
echo.
echo   正在放行 NDI 需要的端口...
echo.
netsh advfirewall firewall delete rule name="NDI" >nul 2>&1
netsh advfirewall firewall delete rule name="NDI TCP" >nul 2>&1
netsh advfirewall firewall delete rule name="NDI UDP" >nul 2>&1
netsh advfirewall firewall add rule name="NDI" dir=in action=allow protocol=UDP localport=5353 >nul
netsh advfirewall firewall add rule name="NDI TCP" dir=in action=allow protocol=TCP localport=5960-5990 >nul
netsh advfirewall firewall add rule name="NDI UDP" dir=in action=allow protocol=UDP localport=5960-5990 >nul
echo   ------------------------------------------------------------
echo   完成！NDI 端口已放行：
echo     UDP 5353        （设备发现用的多播）
echo     TCP 5960-5990   （视频/音频数据）
echo     UDP 5960-5990   （视频/音频数据）
echo.
echo   ★ 发送端和接收端【两台电脑都要】跑一次这个文件！
echo   ------------------------------------------------------------
echo.
pause
