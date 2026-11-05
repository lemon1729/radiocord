# 🎵 Discord Music & TTS Bot (All-in-One Self-Contained)

유튜브 봇 차단(403) 우회 인프라, 실시간 음악-TTS 동적 믹싱(Ducking/Pause), 24시간 무중단 라디오, 스마트 세션 바인딩, 그리고 **권한 오류 없는 인터랙티브 버튼 선택 UI**가 모두 포함된 완전 패키지 디스코드 봇 템플릿입니다.

---

## ✨ 핵심 기능 요약

1. **인터랙티브 버튼(UI View) 곡 선택**:
   - 검색어 입력 시 `1️⃣`~`5️⃣`, `❌` 버튼이 즉시 나타나 원클릭으로 선택 가능 (이모지 반응 권한 불필요).
2. **오디오 엔진 자동 복구 (Self-Contained Audio)**:
   - 시스템에 `ffmpeg`가 설치되어 있지 않아도 `imageio-ffmpeg` 내장 바이너리를 자동 감지하여 소리가 즉시 출력.
   - `PyNaCl` 및 `libopus` 자동 로드 탑재.
3. **유튜브 봇 차단(403/Sign-in) 3단 우회 인프라**:
   - Cloudflare WARP(40000) + Privoxy(8118) + BgUtils Deno POT(4416) 완벽 연동.
   - 차단 시 SoundCloud 자동 폴백(Fallback) 안전망 지원.
4. **실시간 덕킹(Ducking) & 일시정지(Pause) 동적 믹서**:
   - 음악 감상 중 채팅 입력 시 음악 볼륨을 20%로 줄이고 TTS 오버레이 합성, 또는 음악 일시정지 후 음성 완료 시 자동 재개.
5. **24시간 무중단 라디오 & 메시지 폭탄(In-Place Edit) 방지**:
   - 음성 채널에 사람이 없어도 퇴장하지 않고 24시간 재생 유지.
   - 곡이 넘어갈 때마다 새 메시지를 보내지 않고 기존 Now Playing 카드를 무한 `edit()`하여 채팅방 스팸 원천 차단.
   - `@멘션` 알림 핑을 제거하고 닉네임만 표시.

---

## ⚡ 빠른 시작 (원클릭 자동 설치)

### Windows 환경
1. 저장소를 클론받은 후 **`setup.bat`**을 더블 클릭하여 실행합니다. (가상환경 생성, 음성 패키지 설치, FFmpeg 확인 자동 수행)
2. 생성된 `.env` 파일을 열고 디스코드 봇 토큰을 입력합니다:
   ```ini
   DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN_HERE
   ENABLE_MESSAGE_CONTENT=true
   ```
3. **`run.bat`**을 실행하면 봇이 즉시 시작됩니다!

### Linux / Ubuntu VPS 환경
```bash
# 1. 원클릭 필수 패키지 및 가상환경 세팅
chmod +x setup.sh scripts/*.sh
./setup.sh

# 2. .env 토큰 설정
nano .env

# 3. (선택) VPS 유튜브 봇 차단 우회 3단 인프라 원클릭 설치
sudo ./scripts/install_vps_bypass.sh

# 4. 봇 실행
./venv/bin/python3 main.py
```

> **⚠️ 필수 체크**: [Discord Developer Portal](https://discord.com/developers/applications)의 봇 설정에서 **`MESSAGE CONTENT INTENT`**를 반드시 켜주세요.

---

## 📻 나만의 라디오 플레이리스트 교체 방법
`build_custom_radio_db.py`를 열어 원하는 유튜브 재생목록 URL을 넣고 실행하면, 나만의 24시간 라디오 데이터베이스가 자동으로 생성됩니다:
```bash
python build_custom_radio_db.py
```

---

## 📖 전체 구조 및 시스템 가이드
상세 아키텍처 다이어그램 및 원리 설명은 [COMPLETE_SYSTEM_GUIDE.md](COMPLETE_SYSTEM_GUIDE.md)를 참고하세요.
