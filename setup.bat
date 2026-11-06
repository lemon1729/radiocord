@echo off
chcp 65001 > nul
echo ========================================================
echo  🎵 Discord Music & TTS Bot 원클릭 환경 설정 (Windows)
echo ========================================================
echo.

:: 1. Python 설치 확인
python --version > nul 2>&1
if %errorlevel% neq 0 (
    echo [오류] Python이 설치되어 있지 않거나 환경 변수 PATH에 등록되지 않았습니다.
    echo https://www.python.org 에서 Python 3.10 이상을 설치해주세요. (Add to PATH 필수 체크)
    pause
    exit /b 1
)

:: 2. 가상환경 생성
if not exist "venv" (
    echo [1/4] 파이썬 가상환경(venv)을 생성합니다...
    python -m venv venv
) else (
    echo [1/4] 기존 가상환경(venv)을 확인했습니다.
)

:: 3. 패키지 설치
echo [2/4] 음성/음악 필수 패키지를 설치합니다 (discord.py[voice], davey, PyNaCl, yt-dlp, edge-tts 등)...
call venv\Scripts\activate
python -m pip install --upgrade pip
pip install -r requirements.txt

:: 4. FFmpeg 확인
echo [3/4] 시스템 FFmpeg 오디오 엔진을 확인합니다...
where ffmpeg > nul 2>&1
if %errorlevel% neq 0 (
    echo   ⚠️ 시스템 PATH에 ffmpeg가 없습니다.
    echo   💡 imageio-ffmpeg 내장 바이너리를 자동 사용하지만,
    echo   최상의 성능을 위해 'winget install Gyan.FFmpeg' 실행을 권장합니다.
) else (
    echo   ✅ FFmpeg가 정상 감지되었습니다.
)

:: 5. .env 파일 준비
echo [4/4] 환경 설정 파일을 준비합니다...
if not exist ".env" (
    copy .env.example .env > nul
    echo   ✅ .env 파일이 생성되었습니다. 메모장으로 열어 DISCORD_BOT_TOKEN을 입력해주세요!
) else (
    echo   ✅ 기존 .env 파일이 존재합니다.
)

echo.
echo ========================================================
echo  🎉 모든 설정이 완료되었습니다!
echo  1. .env 파일을 열고 발급받은 디스코드 봇 토큰을 입력하세요.
echo  2. run.bat 을 실행하면 봇이 가동됩니다.
echo ========================================================
echo.
pause
