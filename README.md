# 🎵 Radiocord (Discord Music & TTS Bot)

> **All-in-One Self-Contained Discord Bot**  
> 유튜브 봇 차단(403) 우회 인프라, 실시간 음악-TTS 동적 믹싱(Ducking/Pause), 24시간 무중단 라디오, 권한 오류 없는 인터랙티브 버튼 선택 UI가 모두 포함된 완전 패키지 디스코드 봇입니다.

---

## ✨ 핵심 기능 요약

- **🎛️ 권한 오류 없는 인터랙티브 버튼(UI View) 곡 선택**:
  - 검색어 입력 시 `1️⃣`~`5️⃣`, `❌` 버튼이 즉시 렌더링되어 원클릭으로 선택 가능 (봇의 이모지 반응 권한 불필요).
- **🔊 오디오 엔진 자가 복구 (Self-Healing Audio)**:
  - 시스템에 FFmpeg가 없어도 `imageio-ffmpeg` 내장 바이너리를 자동 감지하여 묵음(소리 안 남) 현상 원천 차단.
  - `PyNaCl` 및 `libopus` 자동 로드 탑재.
- **🛡️ 유튜브 봇 차단(403 Forbidden / Sign-in) 3단 우회 인프라**:
  - Cloudflare WARP(40000) + Privoxy(8118) + BgUtils Deno POT(4416) 완벽 연동.
  - 차단 감지 시 SoundCloud 자동 폴백(Fallback) 안전망 지원.
- **🎧 실시간 오디오 덕킹(Ducking) & 일시정지(Pause) 동적 믹서**:
  - 음악 감상 중 채팅 입력 시 음악 볼륨을 20%로 줄이고 TTS 오버레이 합성, 또는 음악 일시정지 후 TTS 완료 시 자동 재개.
- **📻 24시간 무중단 라디오 & 메시지 폭탄 방지(In-Place Edit)**:
  - 음성 채널에 유저가 아무도 없어도 자동 퇴장하지 않고 24시간 연속 방송.
  - 곡이 바뀔 때마다 새 메시지를 보내지 않고 기존 Now Playing 카드를 무한 `edit()`하여 채팅방 스팸 방지.
  - `@멘션` 알림 핑을 제거하고 닉네임만 표시.

---

## 🎮 슬래시 커맨드 전체 일람표

### 🎵 음악 명령어
| 명령어 | 설명 |
| :--- | :--- |
| `/play <검색어/링크>` | 노래 제목으로 검색(상위 5곡 버튼 선택)하거나 유튜브/사운드클라우드 링크 즉시 재생 |
| `/playnext <검색어/링크>` | 현재 재생 중인 곡 바로 다음 순서로 우선 예약 등록 |
| `/skip` | 현재 재생 중인 곡을 건너뛰고 다음 곡 재생 |
| `/pause` / `/resume` | 음악 일시정지 및 다시 재생 |
| `/stop` | 음악 재생 중지 및 대기열 초기화 (음성 채널 퇴장) |
| `/queue` | 현재 대기 중인 곡 목록 확인 |
| `/nowplaying` | 현재 재생 중인 곡 정보 및 실시간 컨트롤러 버튼 표시 |
| `/volume <1~150>` | 음악 볼륨 조절 (기본값 80%) |
| `/loop <off/song/queue>` | 반복 모드 설정 (끄기 / 한곡 반복 / 전체 반복) |
| `/shuffle` | 현재 대기열의 곡 순서를 무작위로 셔플 |
| `/autoleave <on/off>` | 채널에 아무도 없을 때 봇 자동 퇴장 여부 설정 |

### 📻 24시간 라디오 명령어
| 명령어 | 설명 |
| :--- | :--- |
| `/radio play [카테고리] [재생모드]` | 24시간 무한 연속 On-Air 라디오 시작 (지정 곡 수 모드 지원) |
| `/radio stop` | 라디오 자동 선곡 모드 종료 |
| `/radio channels` | 등록된 아티스트 및 플레이리스트 목록 조회 |

### 📢 TTS (텍스트 음성 변환) 명령어
| 명령어 | 설명 |
| :--- | :--- |
| `/tts join` | 현재 통화방에 봇을 연결하고 채팅방과 음성 세션 바인딩 |
| `/tts leave` | TTS 음성 읽기 세션 종료 및 퇴장 |
| `/tts say <메시지>` | 입력한 텍스트를 즉시 음성으로 출력 |
| `/tts voice <목소리>` | 내 개인 TTS 보이스 변경 (선희, 인준, 현수, 나나미, 케이타) |
| `/tts speed <속도>` | 읽기 속도 조절 (-50% ~ +100%) |
| `/tts name <on/off>` | 메시지 읽을 때 사용자 이름(닉네임) 포함 여부 토글 |
| `/tts ducking <mode> [volume]` | 음악 재생 중 TTS 출력 방식 설정 (`duck` 덕킹 / `pause` 일시정지 / `off` 끄기) |

---

## ⚡ 빠른 시작 (원클릭 자동 설치)

### Windows 환경
1. 리포지토리를 클론합니다:
   ```bash
   git clone https://github.com/lemon1729/radiocord.git
   cd radiocord
   ```
2. **`setup.bat`**을 더블 클릭하여 실행합니다. (가상환경 생성, 음성 패키지 설치, FFmpeg/Opus 자동 검증 원클릭 수행)
3. 생성된 `.env` 파일을 열고 디스코드 봇 토큰을 입력합니다:
   ```ini
   DISCORD_BOT_TOKEN=YOUR_DISCORD_BOT_TOKEN_HERE
   ENABLE_MESSAGE_CONTENT=true
   ```
4. **`run.bat`**을 실행하면 봇이 즉시 가동됩니다!

### Linux / Ubuntu VPS 환경
```bash
git clone https://github.com/lemon1729/radiocord.git
cd radiocord

# 1. 원클릭 필수 패키지 및 가상환경 세팅
chmod +x setup.sh scripts/*.sh
./setup.sh

# 2. .env 토큰 설정
nano .env

# 3. (선택) VPS 유튜브 봇 차단 우회 3단 인프라 원클릭 자동 설치
sudo ./scripts/install_vps_bypass.sh

# 4. 봇 실행
./venv/bin/python3 main.py
```

> **⚠️ 필수 체크 (중요)**:  
> [Discord Developer Portal](https://discord.com/developers/applications) ➡️ 본인 봇 ➡️ **Bot** 탭에서 **`MESSAGE CONTENT INTENT`**를 반드시 켜주세요. (꺼져 있으면 채팅 메시지를 읽지 못해 TTS 및 덕킹이 작동하지 않습니다)

---

## 📻 나만의 라디오 플레이리스트 구축 방법

1. [`build_custom_radio_db.py`](build_custom_radio_db.py)를 열고 상단의 `TARGET_PLAYLISTS`에 수집하고 싶은 유튜브 재생목록 URL을 입력합니다:
   ```python
   TARGET_PLAYLISTS = [
       ("kpop", "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxx", "인기 K-POP 모음"),
       ("lofi", "https://www.youtube.com/playlist?list=PLyyyyyyyyyyyyyyy", "공부할 때 듣는 로파이"),
   ]
   ```
2. 스크립트를 실행하면 `data/custom_radio_database.json` 파일에 곡 메타데이터가 자동 생성됩니다:
   ```bash
   python build_custom_radio_db.py
   ```
3. 이제 `/radio play` 실행 시 수집된 곡들이 24시간 셔플 On-Air로 자동 방송됩니다!

---

## 🛠️ 문제 해결 (FAQ & Troubleshooting)

### Q. 봇이 음성 채널에 들어왔는데 아무 소리도 안 나요!
1. **FFmpeg 설치 여부 확인**:
   - Windows: `winget install Gyan.FFmpeg` 실행 (또는 `setup.bat` 실행 시 내장 imageio-ffmpeg 자동 적용)
   - Linux: `sudo apt install -y ffmpeg libopus0 libopus-dev`
2. **유튜브 403 차단 확인**:
   - 클라우드 VPS(AWS, Oracle 등)에서는 유튜브가 IP를 차단하므로, `sudo ./scripts/install_vps_bypass.sh`를 실행해 WARP+Privoxy+POT 우회 인프라를 가동해야 합니다.

### Q. 채팅을 쳐도 TTS가 안 읽어줘요!
- Discord 개발자 포털의 Bot 설정에서 **`MESSAGE CONTENT INTENT`** 스위치가 켜져 있는지 확인하고, `.env`에 `ENABLE_MESSAGE_CONTENT=true`가 설정되어 있는지 확인하세요.

---

## 📖 전체 시스템 아키텍처 가이드
FFmpeg 버스트 몽키패치 원리, DuckingAudioSource 내부 믹싱 구조, 다중 봇 배포 가이드는 [COMPLETE_SYSTEM_GUIDE.md](COMPLETE_SYSTEM_GUIDE.md)에 상세히 기술되어 있습니다.
