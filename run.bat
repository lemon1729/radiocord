@echo off
chcp 65001 > nul
title Discord Music & TTS Bot
echo ========================================================
echo   Starting Discord Music & TTS Bot...
echo ========================================================
python main.py
if errorlevel 1 (
    echo.
    echo [Error] Bot terminated with an error.
    pause
)
