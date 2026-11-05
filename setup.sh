#!/usr/bin/env bash
set -e

echo "========================================================"
echo " 🎵 Discord Music & TTS Bot 원클릭 환경 설정 (Linux)"
echo "========================================================"

# 1. 시스템 오디오 필수 패키지 설치 (FFmpeg, libopus)
echo "[1/4] 시스템 필수 패키지를 설치합니다 (ffmpeg, libopus, python3-venv)..."
if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y ffmpeg libopus0 libopus-dev python3-venv python3-pip
elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y ffmpeg opus python3-pip
else
    echo "⚠️ 패키지 매니저를 감지하지 못했습니다. ffmpeg와 libopus를 수동 설치해주세요."
fi

# 2. 파이썬 가상환경 생성
echo "[2/4] 파이썬 가상환경(venv)을 생성합니다..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# 3. 필수 패키지 설치
echo "[3/4] 음성/음악 패키지를 설치합니다..."
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. .env 파일 준비
echo "[4/4] 환경 설정 파일(.env)을 준비합니다..."
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo "✅ .env 파일이 생성되었습니다. nano .env 로 봇 토큰을 입력해주세요."
else
    echo "✅ 기존 .env 파일이 확인되었습니다."
fi

echo "========================================================"
echo " 🎉 설정이 완료되었습니다!"
echo " 실행: ./venv/bin/python3 main.py"
echo " 24시간 가동: systemd 서비스 등록 (COMPLETE_SYSTEM_GUIDE.md 참고)"
echo "========================================================"
