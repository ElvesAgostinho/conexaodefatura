@echo off
rem Duplo clique para instalar. Pede privilégios de administrador (para arrancar com o Windows).
net session >nul 2>&1
if %errorlevel% neq 0 (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0instalar.ps1" -AbrirFirewall
echo.
pause
