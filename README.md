# 🎵 Discord Music & TTS Bot Template

유튜브 봇 차단 완벽 우회 인프라, 실시간 음악-TTS 동적 믹싱(Ducking/Pause), 24시간 무중단 라디오, 그리고 스마트 세션 바인딩이 탑재된 프로덕션급 디스코드 봇 템플릿입니다.

---

## ⚡ 빠른 시작 (Quick Start)

### 1. 환경 설정
```bash
# 가상환경 생성 및 패키지 설치
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

pip install -r requirements.txt
```

### 2. `.env` 설정
`.env.example` 파일을 복사하여 `.env`를 생성하고 봇 토큰을 입력합니다:
```ini
DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN_HERE
ENABLE_MESSAGE_CONTENT=true
```
> **중요**: [Discord Developer Portal](https://discord.com/developers/applications)의 Bot 설정에서 **`MESSAGE CONTENT INTENT`**를 반드시 활성화해야 채팅 TTS 및 덕킹이 정상 작동합니다.

### 3. 나만의 라디오 플레이리스트 구축
`build_custom_radio_db.py`를 열어 원하는 유튜브 플레이리스트 URL을 입력한 후 실행합니다:
```bash
python build_custom_radio_db.py
```

### 4. 봇 실행
- Windows: `run.bat` 더블 클릭 또는 `python main.py`
- Linux/VPS: `python3 main.py` 또는 `systemd` 서비스 등록

---

## 📖 전체 구조 및 트러블슈팅 가이드
자세한 시스템 아키텍처, 유튜브 봇 차단 우회(WARP+Privoxy+POT) 세팅법, 오디오 믹서 원리, 다른 서버 배포 방법은 [COMPLETE_SYSTEM_GUIDE.md](COMPLETE_SYSTEM_GUIDE.md)를 참고하세요.
