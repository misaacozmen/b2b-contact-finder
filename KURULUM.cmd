@echo off
rem Kurulum: kurulum.ps1 betigini calistirir (Talimat 30).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0kurulum.ps1" %*
echo.
pause
