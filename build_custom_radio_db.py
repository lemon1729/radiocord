#!/usr/bin/env python3
"""
=============================================================================
범용 라디오 플레이리스트 데이터베이스 자동 생성기 (build_custom_radio_db.py)
=============================================================================
유튜브 재생목록(Playlist) URL 또는 채널 URL들을 입력받아,
라디오 봇에서 24시간 랜덤 셔플 On-Air로 사용할 수 있는 JSON 데이터베이스를 구축합니다.

[사용법]
1. 하단의 TARGET_PLAYLISTS 또는 TARGET_CHANNELS에 원하는 유튜브 URL을 등록합니다.
2. python build_custom_radio_db.py 실행
3. data/custom_radio_database.json 파일이 자동 생성됩니다.
"""

import json
import socket
import sys
from pathlib import Path
import yt_dlp

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DB_PATH = DATA_DIR / "custom_radio_database.json"
COOKIE_PATH = BASE_DIR / "cookies.txt"

# ---------------------------------------------------------------------------
# [사용자 설정] 여기에 수집하고 싶은 유튜브 재생목록(또는 채널)을 등록하세요!
# ---------------------------------------------------------------------------
CATEGORIES = {
    "all": {"name": "🌟 전체 곡 랜덤 방송", "color": 0x5865F2, "desc": "모든 등록된 음원 셔플"},
    "pop": {"name": " Pop & 댄스", "color": 0x3498DB, "desc": "신나는 팝 및 댄스곡"},
    "chill": {"name": "☕ Lo-Fi & 칠링/어쿠스틱", "color": 0x2ECC71, "desc": "편안하게 듣기 좋은 음악"},
    "ost": {"name": "🎬 애니 / 게임 OST", "color": 0xE67E22, "desc": "인기 OST 및 배경음악"},
}

TARGET_PLAYLISTS = [
    # 예시: (카테고리ID, 재생목록_URL, 카테고리_설명)
    # ("pop", "https://www.youtube.com/playlist?list=PLxxxxxxxxxxxxxxxx", "인기 팝송 모음"),
    # ("chill", "https://www.youtube.com/playlist?list=PLyyyyyyyyyyyyyyyy", "공부할 때 듣는 로파이"),
]

def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0

def get_ytdl_extractor():
    opts = {
        "extract_flat": True,
        "skip_download": True,
        "ignoreerrors": True,
        "quiet": False,
        "no_warnings": True,
    }
    # VPS 환경의 Privoxy(8118) 또는 WARP(40000) 자동 감지
    if is_port_open(8118):
        opts["proxy"] = "http://127.0.0.1:8118"
    elif is_port_open(40000):
        opts["proxy"] = "socks5://127.0.0.1:40000"

    # BgUtils POT 서버(4416) 자동 감지
    if is_port_open(4416):
        opts["extractor_args"] = {
            "youtubepot-bgutilhttp": {
                "base_url": ["http://127.0.0.1:4416"]
            }
        }

    if COOKIE_PATH.exists():
        opts["cookiefile"] = str(COOKIE_PATH)

    return yt_dlp.YoutubeDL(opts)

def build_database():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ytdl = get_ytdl_extractor()
    
    songs_dict = {}  # video_id -> song metadata
    print("=" * 60)
    print("📻 범용 라디오 데이터베이스 수집을 시작합니다...")
    print("=" * 60)

    if not TARGET_PLAYLISTS:
        print("[안내] TARGET_PLAYLISTS에 등록된 URL이 없습니다.")
        print("스크립트 상단의 TARGET_PLAYLISTS에 유튜브 플레이리스트 URL을 등록한 뒤 다시 실행해주세요.")
        
        # 기본 템플릿 구조 파일 생성
        template_db = {
            "categories": CATEGORIES,
            "songs": [
                {
                    "id": "dQw4w9WgXcQ",
                    "title": "Sample Radio Track (Rick Astley)",
                    "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "duration": 213,
                    "category": "pop",
                    "uploader": "Rick Astley"
                }
            ]
        }
        with open(OUTPUT_DB_PATH, "w", encoding="utf-8") as f:
            json.dump(template_db, f, ensure_ascii=False, indent=2)
        print(f"✅ 기본 템플릿 데이터베이스가 생성되었습니다: {OUTPUT_DB_PATH}")
        return

    for cat_key, pl_url, label in TARGET_PLAYLISTS:
        print(f"\n🔍 수집 중: [{cat_key}] {label} ({pl_url})")
        try:
            info = ytdl.extract_info(pl_url, download=False)
            if not info:
                continue
            entries = info.get("entries", [])
            print(f"   -> {len(entries)}개의 항목 발견. 메타데이터 파싱 중...")
            
            for item in entries:
                if not item:
                    continue
                v_id = item.get("id")
                title = item.get("title")
                if not v_id or not title or title in ("[Private video]", "[Deleted video]"):
                    continue
                
                # 중복 수집 방지 및 카테고리 태깅
                if v_id not in songs_dict:
                    songs_dict[v_id] = {
                        "id": v_id,
                        "title": title,
                        "url": f"https://www.youtube.com/watch?v={v_id}",
                        "duration": item.get("duration", 0),
                        "category": cat_key,
                        "uploader": item.get("uploader") or item.get("channel") or "Unknown"
                    }
        except Exception as e:
            print(f"❌ 수집 오류 ({pl_url}): {e}")

    songs_list = list(songs_dict.values())
    final_db = {
        "categories": CATEGORIES,
        "songs": songs_list
    }

    with open(OUTPUT_DB_PATH, "w", encoding="utf-8") as f:
        json.dump(final_db, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    print(f"🎉 수집 완료! 총 {len(songs_list)}곡이 라디오 DB에 등록되었습니다.")
    print(f"📁 저장 위치: {OUTPUT_DB_PATH}")
    print("=" * 60)

if __name__ == "__main__":
    build_database()
