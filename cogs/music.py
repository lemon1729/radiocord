import asyncio
import json
import random
import socket
import shutil
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional, Dict, Any, List

try:
    import audioop
except ImportError:
    class _AudioOpFallback:
        @staticmethod
        def mul(fragment: bytes, width: int, factor: float) -> bytes:
            import struct
            count = len(fragment) // 2
            samples = struct.unpack(f"<{count}h", fragment)
            out = [max(-32768, min(32767, int(s * factor))) for s in samples]
            return struct.pack(f"<{count}h", *out)

        @staticmethod
        def add(f1: bytes, f2: bytes, width: int) -> bytes:
            import struct
            count = min(len(f1), len(f2)) // 2
            s1 = struct.unpack(f"<{count}h", f1[:count*2])
            s2 = struct.unpack(f"<{count}h", f2[:count*2])
            out = [max(-32768, min(32767, a + b)) for a, b in zip(s1, s2)]
            return struct.pack(f"<{count}h", *out)
    audioop = _AudioOpFallback()

import discord
import discord.player
from discord import app_commands
from discord.ext import commands
import yt_dlp
import imageio_ffmpeg

# ----------------------------------------------------
# discord.py AudioPlayer 초반 빨리감기(Burst) 방지 패치
# ----------------------------------------------------
def _patch_discord_audio_player():
    """
    discord.py AudioPlayer는 곡 시작 시 _start 시간을 즉시 기록한 뒤 첫 오디오 패킷을 읽습니다.
    FFmpeg가 스트림을 열고 첫 패킷을 디코딩하는 0.5~1.5초 동안 시간이 흐르면,
    봇이 '시간이 밀렸다'고 판단하여 수십 개의 패킷을 딜레이 없이 한꺼번에 전송(Burst)하여
    초반 1~2초가 배속(빨리감기)으로 재생되는 현상이 발생합니다.
    첫 오디오 패킷이 실제로 반환된 시점에 _start를 설정하고 버퍼 지연 시 배속을 방지합니다.
    """
    if getattr(discord.player.AudioPlayer, "_music_bot_patched", False):
        return

    orig_do_run = discord.player.AudioPlayer._do_run

    def patched_do_run(self):
        self.loops = 0
        self._start = None  # 첫 패킷이 실제로 디코딩되어 읽힌 시점에 타이머 시작!

        client = self.client
        play_audio = client.send_audio_packet
        self._speak(discord.player.SpeakingState.voice)

        while not self._end.is_set():
            if not self._resumed.is_set():
                self.send_silence()
                self._resumed.wait()
                continue

            data = self.source.read()

            if not data:
                if self._current_error is None:
                    source_error = getattr(self.source, '_current_error', None)
                    if source_error:
                        self._current_error = source_error
                self.stop()
                break

            if not client.is_connected():
                connected = client.wait_until_connected(client.timeout)
                if self._end.is_set() or not connected:
                    return
                self._speak(discord.player.SpeakingState.voice)
                self.loops = 0
                self._start = None

            now = time.perf_counter()
            # 첫 번째 패킷 수신 시점에 기준 시간 설정 (초기 로딩 딜레이 무효화)
            if self._start is None:
                self._start = now

            play_audio(data, encode=not self.source.is_opus())
            self.loops += 1
            next_time = self._start + self.DELAY * self.loops
            diff = next_time - now

            # 네트워크 지연이나 버퍼링으로 인해 심하게 뒤처질 경우(-0.06s 이상)
            # 패킷을 쏟아붓지 않고(Burst 방지) 기준 시간을 현재 시간으로 리셋하여 정상 1.0배속 유지
            if diff < -0.06:
                self._start = now
                self.loops = 0
                next_time = self._start + self.DELAY
                diff = 0

            delay = max(0, self.DELAY + diff)
            time.sleep(delay)

        if client.is_connected():
            self.send_silence()

    discord.player.AudioPlayer._do_run = patched_do_run
    discord.player.AudioPlayer._music_bot_patched = True

_patch_discord_audio_player()

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PLAYLISTS_PATH = DATA_DIR / "playlists.json"
MUSIC_SETTINGS_PATH = DATA_DIR / "music_settings.json"
STELLIVE_DB_PATH = DATA_DIR / "stellive_database.json"
COOKIE_PATH = DATA_DIR.parent / "cookies.txt"

# FFmpeg 실행 파일 경로 감지: 시스템 설치(apt) 우선, 없을 시 imageio-ffmpeg 사용
FFMPEG_EXECUTABLE = shutil.which("ffmpeg")
if not FFMPEG_EXECUTABLE:
    try:
        FFMPEG_EXECUTABLE = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        FFMPEG_EXECUTABLE = "ffmpeg"


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    """로컬 포트(Privoxy, WARP 프록시, PO Token 서버 등) 리스닝 여부 확인"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


# 1. YouTube 전용 yt-dlp 옵션 (고음질 스트림 우선 + Privoxy HTTP -> WARP SOCKS5 프록시 + PO Token 서버 + 쿠키)
YTDL_YT_OPTIONS = {
    "format": "bestaudio[abr>0]/bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best",
    "extractaudio": True,
    "audioformat": "mp3",
    "outtmpl": "%(extractor)s-%(id)s-%(title)s.%(ext)s",
    "restrictfilenames": True,
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "remote_components": ["ejs:github"],
}

# Privoxy(8118) 우선 적용 -> 없을 시 WARP SOCKS5(40000) 직접 적용
if is_port_open(8118):
    YTDL_YT_OPTIONS["proxy"] = "http://127.0.0.1:8118"
elif is_port_open(40000):
    YTDL_YT_OPTIONS["proxy"] = "socks5://127.0.0.1:40000"

# PO Token 서버(4416) 감지 시 적용
if is_port_open(4416):
    YTDL_YT_OPTIONS["extractor_args"] = {
        "youtubepot-bgutilhttp": {
            "base_url": ["http://127.0.0.1:4416"]
        }
    }

# 쿠키 파일 감지 시 적용
if COOKIE_PATH.exists():
    YTDL_YT_OPTIONS["cookiefile"] = str(COOKIE_PATH)

# 2. SoundCloud 전용 yt-dlp 옵션 (안전망 Fallback - 클라우드 IP 차단 없음)
YTDL_SC_OPTIONS = {
    "format": "bestaudio[abr>0]/bestaudio/best",
    "extractaudio": True,
    "audioformat": "mp3",
    "noplaylist": True,
    "nocheckcertificate": True,
    "ignoreerrors": False,
    "logtostderr": False,
    "quiet": True,
    "no_warnings": True,
    "default_search": "scsearch",
}

# 3. FFmpeg 옵션 분리: 유튜브는 스트림 IP 일치를 위해 Privoxy(8118) HTTP 프록시 경유
FFMPEG_YT_BEFORE = "-http_proxy http://127.0.0.1:8118 -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5" if is_port_open(8118) else "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_YT_OPTIONS = {
    "before_options": FFMPEG_YT_BEFORE,
    "options": "-vn -nostdin",
}

FFMPEG_SC_OPTIONS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn -nostdin",
}

ytdl_yt = yt_dlp.YoutubeDL(YTDL_YT_OPTIONS)
ytdl_sc = yt_dlp.YoutubeDL(YTDL_SC_OPTIONS)


def format_duration(seconds: Optional[float | int]) -> str:
    if not seconds:
        return "라이브 또는 알 수 없음"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


class Song:
    """재생 대상 곡 정보 객체"""

    def __init__(self, data: dict, requester: discord.Member, platform: str = "YouTube"):
        self.data = data
        self.requester = requester
        self.platform = platform  # "YouTube", "SoundCloud", "SoundCloud (대체)"
        self.fallback_used = "대체" in platform

        self.title: str = data.get("title", "알 수 없는 제목")
        self.url: Optional[str] = data.get("url")  # 오디오 스트림 직접 URL
        self.webpage_url: str = data.get("webpage_url") or data.get("url") or "https://youtube.com"
        self.duration: Optional[float | int] = data.get("duration")
        self.uploader: str = data.get("uploader") or data.get("channel") or "알 수 없음"

        # 썸네일 추출 (단일 필드 또는 리스트의 고화질 이미지)
        thumb = data.get("thumbnail")
        if not thumb and data.get("thumbnails"):
            try:
                thumb = data["thumbnails"][-1].get("url")
            except Exception:
                pass
        self.thumbnail: Optional[str] = thumb
        self.is_radio: bool = False
        self.radio_type: Optional[str] = None
        self.radio_artists: list[str] = []
        self.radio_theme_color: Optional[int] = None

    @property
    def platform_badge(self) -> str:
        if "대체" in self.platform:
            return "🟠 SoundCloud (대체)"
        elif "SoundCloud" in self.platform:
            return "🟠 SoundCloud"
        return "🔴 YouTube"

    async def ensure_stream_url(self, loop=None) -> str:
        """재생 직전 실제 오디오 스트림 URL이 유효한지 확인하고 필요 시 추출/대체"""
        if self.url and not self.url.startswith("http://www.youtube.com") and not self.url.startswith("https://www.youtube.com"):
            return self.url

        loop = loop or asyncio.get_event_loop()

        # 1. 이미 사운드클라우드 플랫폼인 경우
        if "soundcloud" in self.platform.lower():
            def _extract_sc():
                return ytdl_sc.extract_info(self.webpage_url, download=False)
            data = await loop.run_in_executor(None, _extract_sc)
            self.url = data.get("url")
            return self.url

        # 2. 유튜브 음원 추출 시도
        def _extract_yt():
            return ytdl_yt.extract_info(self.webpage_url, download=False)

        try:
            data = await loop.run_in_executor(None, _extract_yt)
            self.url = data.get("url")
            if data.get("duration") and not self.duration:
                self.duration = data.get("duration")
            return self.url
        except Exception as yt_err:
            err_str = str(yt_err).lower()
            # 봇 차단 감지 시 사운드클라우드로 자동 안전망 대체
            if "bot" in err_str or "sign in" in err_str or "confirm" in err_str:
                print(f"[Music Fallback] YouTube bot block for '{self.title}'. Fallback to SoundCloud...")
                def _sc_fallback():
                    return ytdl_sc.extract_info(f"scsearch1:{self.title}", download=False)
                sc_res = await loop.run_in_executor(None, _sc_fallback)
                if sc_res and "entries" in sc_res and sc_res["entries"]:
                    sc_entry = sc_res["entries"][0]
                    self.url = sc_entry.get("url")
                    self.platform = "SoundCloud (대체)"
                    self.fallback_used = True
                    print(f"[Music Fallback] Retrieved stream URL from SoundCloud for '{self.title}'")
                    return self.url
            raise yt_err

    @classmethod
    async def from_query(cls, query: str, requester: discord.Member, loop=None):
        """단일 쿼리/URL로부터 최우선 곡 1개를 가져오는 레거시 호환 메서드"""
        _, songs = await search_songs(query, requester, loop)
        if songs:
            song = songs[0]
            await song.ensure_stream_url(loop)
            return song
        return None


# ----------------------------------------------------
# 스텔라이브 (Stellive) 라디오 데이터베이스 헬퍼
# ----------------------------------------------------
def load_stellive_db() -> dict:
    """스텔라이브 음원 및 아티스트 데이터베이스 로드"""
    try:
        if STELLIVE_DB_PATH.exists():
            with open(STELLIVE_DB_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        print(f"[Stellive DB] Error loading database: {e}")
    return {"artists": {}, "official_playlists": {}, "songs": []}


def detect_artists(title: str) -> list[str]:
    """노래 제목에서 스텔라이브 멤버 감지"""
    t = title.lower()
    found = []
    checks = [
        ("yuni", ["유니", "yuni"]),
        ("kanna", ["칸나", "kanna"]),
        ("hina", ["히나", "hina"]),
        ("mashiro", ["마시로", "mashiro"]),
        ("lize", ["리제", "lize"]),
        ("tabi", ["타비", "tabi"]),
        ("shibuki", ["시부키", "shibuki"]),
        ("rin", ["린", "rin"]),
        ("nana", ["나나", "nana"]),
        ("riko", ["리코", "riko"]),
        ("fuya", ["후야", "fuya", "huya", "sakihane"]),
    ]
    for key, aliases in checks:
        if any(alias in t for alias in aliases):
            found.append(key)
    if not found:
        found.append("official")
    return found


def get_stellive_songs(filter_key: str = "all") -> tuple[list[dict], dict]:
    """카테고리/아티스트 필터에 맞는 스텔라이브 음원 목록 및 메타데이터 반환"""
    db = load_stellive_db()
    all_songs = db.get("songs", [])
    if filter_key == "all":
        return all_songs, db
    elif filter_key == "original":
        return [s for s in all_songs if s.get("type") == "original"], db
    elif filter_key == "cover":
        return [s for s in all_songs if s.get("type") == "cover"], db
    elif filter_key == "gen1":
        return [s for s in all_songs if any(a in ("yuni", "kanna", "fuya") for a in s.get("artists", []))], db
    elif filter_key == "gen2":
        return [s for s in all_songs if any(a in ("hina", "mashiro", "lize", "tabi") for a in s.get("artists", []))], db
    elif filter_key == "gen3":
        return [s for s in all_songs if any(a in ("shibuki", "rin", "nana", "riko") for a in s.get("artists", []))], db
    elif filter_key == "group":
        return [s for s in all_songs if len(s.get("artists", [])) >= 2 or "official" in s.get("artists", [])], db
    else:
        return [s for s in all_songs if filter_key in s.get("artists", [])], db


def create_radio_song(song_entry: dict, requester: discord.Member, artists_meta: dict) -> Song:
    """스텔라이브 DB 항목으로부터 Radio Song 인스턴스 생성"""
    artists_list = song_entry.get("artists", [])
    uploader_name = "스텔라이브 (Stellive)"
    if artists_list:
        artist_names = [artists_meta.get(a, {}).get("name", a) for a in artists_list if a in artists_meta]
        if artist_names:
            uploader_name = f"스텔라이브 - {', '.join(artist_names)}"

    dummy_data = {
        "id": song_entry.get("id"),
        "title": song_entry.get("title", "알 수 없는 제목"),
        "url": None,  # 재생 직전에 ensure_stream_url로 추출
        "webpage_url": song_entry.get("url") or f"https://www.youtube.com/watch?v={song_entry.get('id')}",
        "duration": song_entry.get("duration", 0),
        "thumbnail": f"https://i.ytimg.com/vi/{song_entry['id']}/hqdefault.jpg",
        "uploader": uploader_name,
    }
    song = Song(dummy_data, requester, platform="YouTube")
    song.is_radio = True
    song.radio_type = song_entry.get("type", "cover")
    song.radio_artists = artists_list

    if len(artists_list) == 1 and artists_list[0] in artists_meta:
        song.radio_theme_color = artists_meta[artists_list[0]].get("color", 0x9B59B6)
    else:
        song.radio_theme_color = 0x9B59B6

    return song


RADIO_CHOICES_DESC = {
    "all": ("🌟 전체 (오리지널 + 커버)", 0x9B59B6),
    "original": ("💿 오리지널 / 발매곡 전체", 0x9B59B6),
    "cover": ("🎤 멤버 커버곡 전체", 0x5DADE2),
    "group": ("👥 단체곡 및 유닛곡", 0xAF7AC5),
    "gen1": ("⭐ 1기생 Everys (유니, 칸나, 후야)", 0x5DADE2),
    "gen2": ("🪐 2기생 UNIVERSE (히나, 마시로, 리제, 타비)", 0x85C1E9),
    "gen3": ("✨ 3기생 CLICHÉ (시부키, 린, 나나, 리코)", 0xEB984E),
    "yuni": ("🦄 아야츠노 유니", 0x5DADE2),
    "kanna": ("🐉 아이리 칸나", 0x3498DB),
    "fuya": ("💜 사키하네 후야", 0x8E44AD),
    "hina": ("❄️ 시라유키 히나", 0x85C1E9),
    "mashiro": ("🐱 네네코 마시로", 0xF5B7B1),
    "lize": ("🍷 아카네 리제", 0xC0392B),
    "tabi": ("🐾 아라하시 타비", 0x58D68D),
    "shibuki": ("🦊 텐코 시부키", 0xEB984E),
    "rin": ("☁️ 아오쿠모 린", 0x76D7C4),
    "nana": ("🌸 하나코 나나", 0xF1948A),
    "riko": ("🍋 유즈하 리코", 0xF7DC6F),
}

RADIO_CATEGORY_CHOICES = [
    app_commands.Choice(name="🌟 스텔라이브 전체 (오리지널+커버)", value="all"),
    app_commands.Choice(name="💿 스텔라이브 공식 오리지널곡", value="original"),
    app_commands.Choice(name="🎤 스텔라이브 멤버 커버곡 전체", value="cover"),
    app_commands.Choice(name="👥 단체곡 및 유닛곡", value="group"),
    app_commands.Choice(name="⭐ 1기생 Everys (유니, 칸나, 후야)", value="gen1"),
    app_commands.Choice(name="🪐 2기생 UNIVERSE (히나, 마시로, 리제, 타비)", value="gen2"),
    app_commands.Choice(name="✨ 3기생 CLICHÉ (시부키, 린, 나나, 리코)", value="gen3"),
    app_commands.Choice(name="🦄 아야츠노 유니", value="yuni"),
    app_commands.Choice(name="🐉 아이리 칸나", value="kanna"),
    app_commands.Choice(name="💜 사키하네 후야", value="fuya"),
    app_commands.Choice(name="❄️ 시라유키 히나", value="hina"),
    app_commands.Choice(name="🐱 네네코 마시로", value="mashiro"),
    app_commands.Choice(name="🍷 아카네 리제", value="lize"),
    app_commands.Choice(name="🐾 아라하시 타비", value="tabi"),
    app_commands.Choice(name="🦊 텐코 시부키", value="shibuki"),
    app_commands.Choice(name="☁️ 아오쿠모 린", value="rin"),
    app_commands.Choice(name="🌸 하나코 나나", value="nana"),
    app_commands.Choice(name="🍋 유즈하 리코", value="riko"),
]

RADIO_MODE_CHOICES = [
    app_commands.Choice(name="무한 라디오 (24시간 연속 On-Air)", value="infinite"),
    app_commands.Choice(name="5곡 재생 후 종료", value="5"),
    app_commands.Choice(name="10곡 재생 후 종료", value="10"),
    app_commands.Choice(name="20곡 재생 후 종료", value="20"),
]


def is_youtube_video(item: dict) -> bool:
    """yt-dlp flat 검색 결과 중 채널/플레이리스트를 제외하고 실제 동영상만 판별"""
    if not item or not isinstance(item, dict):
        return False
    # 1. 타입 확인
    if item.get("_type") in ("channel", "playlist"):
        return False
    if item.get("ie_key") in ("YoutubeChannel", "YoutubeTab", "YoutubePlaylist"):
        return False
    # 2. ID 확인 (YouTube 채널 ID는 통상 UC로 시작하며 24자 내외)
    _id = str(item.get("id", ""))
    if _id.startswith("UC") and len(_id) >= 20:
        return False
    # 3. URL 확인 (채널/플레이리스트 경로 제외)
    url = (str(item.get("url", "")) + " " + str(item.get("webpage_url", ""))).lower()
    channel_indicators = ["/channel/", "/@", "/c/", "/user/", "/playlist?list="]
    if any(ind in url for ind in channel_indicators):
        return False
    # 4. 제목 필수
    if not item.get("title"):
        return False
    return True


async def search_songs(query: str, requester: discord.Member, loop=None) -> tuple[str, List[Song]]:
    """
    유튜브 최우선 -> 실패 시 사운드클라우드 자동 안전망 검색
    반환값: (검색_플랫폼_이름, 곡_목록)
    """
    loop = loop or asyncio.get_event_loop()
    query = query.strip()

    # 1. URL 직접 입력인 경우
    if query.startswith(("http://", "https://")):
        if "soundcloud.com" in query:
            def _extract_sc():
                return ytdl_sc.extract_info(query, download=False)
            data = await loop.run_in_executor(None, _extract_sc)
            return "SoundCloud", [Song(data, requester, platform="SoundCloud")]
        else:
            def _extract_yt():
                return ytdl_yt.extract_info(query, download=False)
            try:
                data = await loop.run_in_executor(None, _extract_yt)
                return "YouTube", [Song(data, requester, platform="YouTube")]
            except Exception as e:
                err_str = str(e).lower()
                if "bot" in err_str or "sign in" in err_str:
                    # 제목만 추출해 사운드클라우드 대체 검색 시도
                    try:
                        def _flat():
                            y_flat = yt_dlp.YoutubeDL({**YTDL_YT_OPTIONS, "extract_flat": True})
                            return y_flat.extract_info(query, download=False)
                        f_data = await loop.run_in_executor(None, _flat)
                        title = f_data.get("title")
                        if title:
                            def _sc_fallback():
                                return ytdl_sc.extract_info(f"scsearch1:{title}", download=False)
                            sc_res = await loop.run_in_executor(None, _sc_fallback)
                            if sc_res and sc_res.get("entries"):
                                return "SoundCloud (대체)", [Song(sc_res["entries"][0], requester, platform="SoundCloud (대체)")]
                    except Exception:
                        pass
                raise e

    # 2. 일반 검색어: 1순위 유튜브 동영상 검색 (extract_flat으로 최대 10개 조회 후 순수 동영상 5개 선별)
    yt_songs: List[Song] = []
    yt_failed = False

    def _search_youtube():
        y_flat = yt_dlp.YoutubeDL({**YTDL_YT_OPTIONS, "extract_flat": True})
        return y_flat.extract_info(f"ytsearch10:{query}", download=False)

    try:
        yt_res = await loop.run_in_executor(None, _search_youtube)
        if yt_res and "entries" in yt_res:
            for item in yt_res["entries"]:
                if is_youtube_video(item):
                    if not item.get("webpage_url"):
                        item["webpage_url"] = f"https://www.youtube.com/watch?v={item['id']}"
                    yt_songs.append(Song(item, requester, platform="YouTube"))
                if len(yt_songs) >= 5:
                    break
    except Exception as e:
        print(f"[Music Search] YouTube search failed: {e}. Fallback to SoundCloud...")
        yt_failed = True

    if yt_songs and not yt_failed:
        return "YouTube", yt_songs

    # 3. 2순위 사운드클라우드 안전망 검색 (상위 5곡)
    def _search_soundcloud():
        return ytdl_sc.extract_info(f"scsearch5:{query}", download=False)

    try:
        sc_res = await loop.run_in_executor(None, _search_soundcloud)
        sc_songs: List[Song] = []
        if sc_res and "entries" in sc_res:
            for item in sc_res["entries"]:
                if item and item.get("title"):
                    sc_songs.append(Song(item, requester, platform="SoundCloud (대체)"))
                if len(sc_songs) >= 5:
                    break
        if sc_songs:
            return "SoundCloud (대체)", sc_songs
    except Exception as e:
        print(f"[Music Search] SoundCloud search also failed: {e}")

    return "None", []


def create_search_embed(query: str, platform: str, songs: List[Song], requester: discord.Member) -> discord.Embed:
    """검색 결과 상위 5곡 및 출처 뱃지를 표시하는 임베드 생성"""
    number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    if "대체" in platform:
        color = 0xFF5500
        title = "🟠 SoundCloud 검색 결과 (YouTube 대체 엔진)"
    elif "soundcloud" in platform.lower():
        color = 0xFF7700
        title = "🟠 SoundCloud 검색 결과"
    else:
        color = 0xFF0000
        title = "🔴 YouTube 검색 결과"

    embed = discord.Embed(
        title=title,
        description=f"🔍 **'{query}'** 검색 결과 상위 {len(songs)}곡입니다.\n아래 **이모지 반응**을 눌러 재생할 곡을 선택해주세요! (60초 제한)\n\n",
        color=color
    )

    lines = []
    for idx, song in enumerate(songs[:5]):
        lines.append(
            f"{number_emojis[idx]} **[{song.title}]({song.webpage_url})**\n"
            f"　⏱️ `{format_duration(song.duration)}` | 👤 {song.uploader}\n"
        )
    embed.description += "\n".join(lines)

    if songs and songs[0].thumbnail:
        embed.set_thumbnail(url=songs[0].thumbnail)

    embed.set_footer(text=f"신청자: {requester.display_name} • 취소하려면 ❌를 눌러주세요 🎵")
    return embed


class DuckingAudioSource(discord.AudioSource):
    """
    음악 소스와 TTS 소스를 동적으로 결합/믹싱하는 고성능 AudioSource.
    - duck 모드: 음악 볼륨을 duck_volume_ratio(기본 0.2, 즉 20%)로 감쇠하고 TTS PCM을 오버레이 합산.
    - pause 모드: 음악 PCM 읽기를 일시 멈추고 TTS PCM만 읽어서 출력, 종료 시 다시 음악 PCM 읽기 복귀.
    """
    def __init__(self, music_source: discord.AudioSource, player: "GuildMusicPlayer", loop: Optional[asyncio.AbstractEventLoop] = None):
        self.music_source = music_source
        self.player = player
        self.loop = loop or getattr(player, "loop", None) or getattr(player.bot, "_loop", None)
        self.current_tts_source: Optional[discord.AudioSource] = None
        self.tts_done_event: Optional[asyncio.Event] = None
        self.tts_mode: str = "duck"
        self.duck_volume_ratio: float = 0.2
        self._lock = threading.Lock()

    def set_tts(self, tts_source: discord.AudioSource, done_event: asyncio.Event, mode: str = "duck", duck_ratio: float = 0.2, loop: Optional[asyncio.AbstractEventLoop] = None):
        with self._lock:
            if self.current_tts_source:
                try:
                    self.current_tts_source.cleanup()
                except Exception:
                    pass
            self.current_tts_source = tts_source
            self.tts_done_event = done_event
            self.tts_mode = mode
            self.duck_volume_ratio = max(0.0, min(1.0, duck_ratio))
            if loop:
                self.loop = loop
            print(f"[DuckingAudioSource] set_tts active: mode={mode}, duck_ratio={self.duck_volume_ratio}")

    def _finish_tts(self):
        with self._lock:
            had_tts = bool(self.current_tts_source or self.tts_done_event)
            if self.current_tts_source:
                try:
                    self.current_tts_source.cleanup()
                except Exception:
                    pass
                self.current_tts_source = None
            if self.tts_done_event:
                target_loop = getattr(self, "loop", None) or getattr(self.player, "loop", None)
                if target_loop and not target_loop.is_closed():
                    try:
                        target_loop.call_soon_threadsafe(self.tts_done_event.set)
                    except Exception:
                        self.tts_done_event.set()
                else:
                    try:
                        self.tts_done_event.set()
                    except Exception:
                        pass
                self.tts_done_event = None
            if had_tts:
                print("[DuckingAudioSource] _finish_tts: TTS completed, music resumed.")

    def read(self) -> bytes:
        with self._lock:
            tts_source = self.current_tts_source
            tts_mode = self.tts_mode

        # 1. TTS가 없는 일반 음악 재생
        if tts_source is None:
            music_data = self.music_source.read()
            if not music_data:
                return b""
            if self.player.volume != 1.0:
                music_data = audioop.mul(music_data, 2, self.player.volume)
            return music_data

        # 2. TTS가 존재하는 경우: 일시정지(pause) 모드
        if tts_mode == "pause":
            tts_data = tts_source.read()
            if not tts_data:
                self._finish_tts()
                music_data = self.music_source.read()
                if not music_data:
                    return b""
                if self.player.volume != 1.0:
                    music_data = audioop.mul(music_data, 2, self.player.volume)
                return music_data

            if len(tts_data) < 3840:
                tts_data = tts_data.ljust(3840, b"\x00")
            return tts_data

        # 3. TTS가 존재하는 경우: 덕킹(duck) 모드
        music_data = self.music_source.read()
        tts_data = tts_source.read()

        if not tts_data:
            self._finish_tts()
            if not music_data:
                return b""
            if self.player.volume != 1.0:
                music_data = audioop.mul(music_data, 2, self.player.volume)
            return music_data

        if not music_data:
            if len(tts_data) < 3840:
                tts_data = tts_data.ljust(3840, b"\x00")
            return tts_data

        effective_vol = max(0.0, self.player.volume * self.duck_volume_ratio)
        ducked_music = audioop.mul(music_data, 2, effective_vol)

        target_len = max(len(ducked_music), len(tts_data))
        if len(ducked_music) < target_len:
            ducked_music = ducked_music.ljust(target_len, b"\x00")
        if len(tts_data) < target_len:
            tts_data = tts_data.ljust(target_len, b"\x00")

        mixed = audioop.add(ducked_music, tts_data, 2)
        if len(mixed) < 3840:
            mixed = mixed.ljust(3840, b"\x00")
        elif len(mixed) > 3840:
            mixed = mixed[:3840]
        return mixed

    def cleanup(self):
        self._finish_tts()
        try:
            self.music_source.cleanup()
        except Exception:
            pass


def create_now_playing_embed(song: Song, player: Optional["GuildMusicPlayer"] = None) -> discord.Embed:
    is_radio = getattr(song, "is_radio", False) or (player and player.radio_mode)

    title = "📻 스텔라이브 라디오 On-Air 🎵" if is_radio else "🎵 지금 재생 중 (Now Playing)"
    color = getattr(song, "radio_theme_color", 0x27C7FE) if is_radio else 0x27C7FE

    embed = discord.Embed(
        title=title,
        description=f"### [{song.title}]({song.webpage_url})",
        color=color
    )
    embed.add_field(name="길이", value=format_duration(song.duration), inline=True)
    if not is_radio:
        req_name = song.requester.display_name if getattr(song, "requester", None) else "알 수 없음"
        embed.add_field(name="신청자", value=req_name, inline=True)
    if "대체" in song.platform or "SoundCloud" in song.platform:
        embed.add_field(name="플랫폼", value=song.platform_badge, inline=True)

    if is_radio:
        type_str = "💿 오리지널 / 발매곡" if getattr(song, "radio_type", "") == "original" else "🎤 커버곡"
        embed.add_field(name="음원 유형", value=type_str, inline=True)
        artists_meta = load_stellive_db().get("artists", {})
        artist_labels = []
        for a_key in getattr(song, "radio_artists", []):
            meta = artists_meta.get(a_key)
            if meta:
                artist_labels.append(f"{meta.get('emoji', '🌟')} {meta.get('name', a_key)}")
            else:
                artist_labels.append(a_key)
        if artist_labels:
            embed.add_field(name="참여 아티스트", value=", ".join(artist_labels), inline=True)
    else:
        embed.add_field(name="채널 / 아티스트", value=song.uploader, inline=True)

    if player:
        vc = player.voice_client
        status_text = "⏸️ 일시정지됨" if (vc and vc.is_paused()) else "▶️ 재생 중"
        loop_names = {"off": "꺼짐", "song": "한곡 반복 🔂", "queue": "전체 반복 🔁"}
        embed.add_field(name="재생 상태", value=status_text, inline=True)
        embed.add_field(name="현재 볼륨", value=f"🔊 {int(player.volume * 100)}%", inline=True)
        if player.radio_mode:
            rem_str = "무한 On-Air" if player.radio_remaining is None else f"남은 곡: {player.radio_remaining}곡"
            embed.add_field(name="라디오 모드", value=f"📻 ON ({rem_str})", inline=True)
        else:
            embed.add_field(name="반복 모드", value=loop_names.get(player.loop_mode, player.loop_mode.upper()), inline=True)
        embed.add_field(name="대기 곡 수", value=f"{len(player.queue)}곡", inline=True)

    if song.thumbnail:
        embed.set_thumbnail(url=song.thumbnail)
    footer_text = "스텔라이브 24시간 라디오 🌟 • 하단 버튼으로 실시간 제어" if is_radio else "Discord Music & TTS Bot 🎵 • 하단 버튼으로 실시간 제어 가능"
    embed.set_footer(text=footer_text)
    return embed


class MusicControlView(discord.ui.View):
    """실시간 음악 제어 대화형 버튼 뷰"""
    def __init__(self, player: "GuildMusicPlayer", timeout: Optional[float] = None):
        super().__init__(timeout=timeout)
        self.player = player
        self.update_button_states()

    def update_button_states(self):
        vc = self.player.voice_client
        is_paused = bool(vc and vc.is_paused())

        self.btn_play_pause.label = "재개" if is_paused else "일시정지"
        self.btn_play_pause.emoji = "⏯️"
        self.btn_play_pause.style = discord.ButtonStyle.success if is_paused else discord.ButtonStyle.primary

        loop_labels = {
            "off": ("반복: 꺼짐", discord.ButtonStyle.secondary),
            "song": ("한곡 반복 🔂", discord.ButtonStyle.primary),
            "queue": ("전체 반복 🔁", discord.ButtonStyle.primary),
        }
        lbl, stl = loop_labels.get(self.player.loop_mode, ("반복: 꺼짐", discord.ButtonStyle.secondary))
        self.btn_loop.label = lbl
        self.btn_loop.style = stl

    async def _check_user_voice(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 음성 채널에 먼저 접속해주세요.", ephemeral=True)
            return False
        if interaction.guild.me.voice and interaction.user.voice.channel != interaction.guild.me.voice.channel:
            await interaction.response.send_message("❌ 봇과 같은 음성 채널에 있어야 컨트롤러를 조작할 수 있습니다.", ephemeral=True)
            return False
        return True

    @discord.ui.button(emoji="⏯️", label="일시정지", style=discord.ButtonStyle.primary, row=0)
    async def btn_play_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        vc = self.player.voice_client
        if not vc:
            await interaction.response.send_message("❌ 연결된 음성 채널이 없습니다.", ephemeral=True)
            return

        if vc.is_paused():
            vc.resume()
            self.update_button_states()
            embed = create_now_playing_embed(self.player.current, self.player) if self.player.current else None
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                pass
        elif vc.is_playing():
            vc.pause()
            self.update_button_states()
            embed = create_now_playing_embed(self.player.current, self.player) if self.player.current else None
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                pass
        else:
            await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", label="스킵", style=discord.ButtonStyle.secondary, row=0)
    async def btn_skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        vc = self.player.voice_client
        if not vc or (not vc.is_playing() and not vc.is_paused()):
            await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)
            return

        if self.player.loop_mode == "song":
            self.player.loop_mode = "off"
        vc.stop()
        await interaction.response.send_message("⏭️ 음악을 건너뛰었습니다!", ephemeral=True)

    @discord.ui.button(emoji="🔁", label="반복: 꺼짐", style=discord.ButtonStyle.secondary, row=0)
    async def btn_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        next_mode = {"off": "song", "song": "queue", "queue": "off"}
        self.player.loop_mode = next_mode.get(self.player.loop_mode, "off")
        self.update_button_states()
        embed = create_now_playing_embed(self.player.current, self.player) if self.player.current else None
        try:
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            pass

    @discord.ui.button(emoji="📜", label="대기열", style=discord.ButtonStyle.secondary, row=0)
    async def btn_queue(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        embed = self._create_queue_embed()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(emoji="⏹️", label="정지", style=discord.ButtonStyle.danger, row=0)
    async def btn_stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        self.player.queue.clear()
        self.player.loop_mode = "off"
        self.player.radio_mode = False
        self.player.radio_remaining = None
        self.player.current = None
        self.player.now_playing_message = None

        tts_cog = self.player.bot.get_cog("TTSCog")
        is_tts = tts_cog and hasattr(tts_cog, "is_tts_active") and tts_cog.is_tts_active(interaction.guild.id)

        vc = self.player.voice_client
        if vc:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            if not is_tts:
                await vc.disconnect()

        for child in self.children:
            child.disabled = True
        try:
            await interaction.response.edit_message(content="⏹️ 음악 재생을 중단하고 대기열을 정리했습니다.", embed=None, view=self)
        except Exception:
            pass

    @discord.ui.button(emoji="🔀", label="셔플", style=discord.ButtonStyle.secondary, row=1)
    async def btn_shuffle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        if len(self.player.queue) < 2:
            await interaction.response.send_message("❌ 셔플하려면 대기열에 최소 2곡 이상 있어야 합니다.", ephemeral=True)
            return

        temp = list(self.player.queue)
        random.shuffle(temp)
        self.player.queue = deque(temp)
        await interaction.response.send_message(f"🔀 대기열 {len(self.player.queue)}곡을 무작위로 섞었습니다!", ephemeral=True)

    @discord.ui.button(emoji="🔉", label="볼륨 -10%", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        self.player.volume = max(0.1, round(self.player.volume - 0.1, 2))
        embed = create_now_playing_embed(self.player.current, self.player) if self.player.current else None
        if embed:
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                pass
        else:
            await interaction.response.send_message(f"🔉 볼륨: **{int(self.player.volume * 100)}%**", ephemeral=True)

    @discord.ui.button(emoji="🔊", label="볼륨 +10%", style=discord.ButtonStyle.secondary, row=1)
    async def btn_vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._check_user_voice(interaction):
            return
        self.player.volume = min(1.5, round(self.player.volume + 0.1, 2))
        embed = create_now_playing_embed(self.player.current, self.player) if self.player.current else None
        if embed:
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                pass
        else:
            await interaction.response.send_message(f"🔊 볼륨: **{int(self.player.volume * 100)}%**", ephemeral=True)

    @discord.ui.button(emoji="🔄", label="새로고침", style=discord.ButtonStyle.secondary, row=1)
    async def btn_refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.update_button_states()
        embed = create_now_playing_embed(self.player.current, self.player) if self.player.current else None
        if embed:
            try:
                await interaction.response.edit_message(embed=embed, view=self)
            except Exception:
                pass
        else:
            await interaction.response.send_message("🔄 상태를 새로고침했습니다.", ephemeral=True)

    def _create_queue_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📜 현재 음악 대기열 (Queue)",
            color=0x27C7FE
        )
        if self.player.current:
            cur_radio = " `[📻라디오]`" if getattr(self.player.current, "is_radio", False) else ""
            embed.add_field(
                name="▶️ 현재 재생 중",
                value=f"[{self.player.current.title}]({self.player.current.webpage_url}) (`{format_duration(self.player.current.duration)}`){cur_radio}",
                inline=False
            )
        if self.player.queue:
            lines = []
            for i, s in enumerate(list(self.player.queue)[:10], start=1):
                badge = f" - {s.platform_badge}" if ("대체" in s.platform or "SoundCloud" in s.platform) else ""
                radio_tag = " `[📻라디오]`" if getattr(s, "is_radio", False) else ""
                lines.append(f"`{i}.` [{s.title}]({s.webpage_url}) (`{format_duration(s.duration)}`){radio_tag}{badge}")
            desc = "\n".join(lines)
            if len(self.player.queue) > 10:
                desc += f"\n... 외 {len(self.player.queue) - 10}곡 더 있음"
            embed.add_field(name=f"대기 목록 ({len(self.player.queue)}곡)", value=desc, inline=False)
        else:
            embed.add_field(name="대기 목록", value="대기 중인 다음 곡이 없습니다.", inline=False)
        embed.set_footer(text="Discord Music & TTS Bot 🎵")
        return embed


# ----------------------------------------------------
# 길드별 플레이어 매니저
# ----------------------------------------------------
class GuildMusicPlayer:
    """서버별 음악 재생 상태 관리기"""

    def __init__(self, bot: commands.Bot, guild: discord.Guild, auto_leave: bool = True):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Song] = deque()
        self.current: Optional[Song] = None
        self.loop_mode: str = "off"  # "off", "song", "queue"
        self.voice_client: Optional[discord.VoiceClient] = None
        self.next_event = asyncio.Event()
        self.play_task: Optional[asyncio.Task] = None
        self.volume: float = 0.8
        self.text_channel: Optional[discord.TextChannel] = None
        self.skip_next_now_playing_message: bool = False
        self.auto_leave: bool = auto_leave
        self.auto_leave_task: Optional[asyncio.Task] = None
        self.current_source: Optional[DuckingAudioSource] = None
        self.now_playing_message: Optional[discord.Message] = None
        self.radio_mode: bool = False
        self.radio_filter: str = "all"
        self.radio_history: deque[str] = deque(maxlen=50)
        self.radio_remaining: Optional[int] = None
        self.radio_requester: Optional[discord.Member] = None

    def _refill_radio_queue(self):
        """라디오 모드가 활성화되어 있을 때 대기열에 곡을 최소 3곡 유지하도록 자동 보충"""
        if not self.radio_mode:
            return

        while len(self.queue) < 3:
            if self.radio_remaining is not None and self.radio_remaining <= 0:
                self.radio_mode = False
                break

            songs, db = get_stellive_songs(self.radio_filter)
            if not songs:
                songs, db = get_stellive_songs("all")
            if not songs:
                self.radio_mode = False
                break

            candidates = [s for s in songs if s["id"] not in self.radio_history]
            if not candidates:
                current_id = getattr(self.current, "data", {}).get("id")
                self.radio_history.clear()
                if current_id:
                    self.radio_history.append(current_id)
                candidates = [s for s in songs if s["id"] not in self.radio_history] or songs

            picked = random.choice(candidates)
            self.radio_history.append(picked["id"])
            if self.radio_remaining is not None:
                self.radio_remaining -= 1

            req = self.radio_requester or self.guild.me
            artists_meta = db.get("artists", {})
            radio_song = create_radio_song(picked, req, artists_meta)
            self.queue.append(radio_song)

    def start_player(self):
        if not self.play_task or self.play_task.done():
            self.play_task = self.bot.loop.create_task(self.audio_player_loop())

    async def play_tts_overlay(self, file_path: str, mode: str = "duck", duck_ratio: float = 0.2) -> bool:
        """음악 재생 중에 TTS 오디오를 덕킹/믹싱 또는 일시정지 형태로 출력"""
        vc = self.voice_client or (self.guild.voice_client if self.guild else None)
        if vc:
            self.voice_client = vc
        if not vc or not vc.is_connected():
            print("[Music TTS Overlay] Failed: Voice client not connected.")
            return False

        source = self.current_source
        if not source and vc and isinstance(vc.source, DuckingAudioSource):
            source = vc.source
            self.current_source = source

        if not source or not isinstance(source, DuckingAudioSource):
            print(f"[Music TTS Overlay] Failed: Source is not DuckingAudioSource (source={type(source)}).")
            return False

        was_paused = vc.is_paused()
        done_event = asyncio.Event()

        try:
            tts_source = discord.FFmpegPCMAudio(file_path, executable=FFMPEG_EXECUTABLE)
            actual_mode = "pause" if was_paused else mode
            print(f"[Music TTS Overlay] Starting overlay playback (mode={actual_mode}, duck_ratio={duck_ratio})")
            running_loop = asyncio.get_running_loop()
            source.set_tts(tts_source, done_event, mode=actual_mode, duck_ratio=duck_ratio, loop=running_loop)

            if was_paused:
                vc.resume()

            await done_event.wait()
            print("[Music TTS Overlay] Overlay playback finished successfully.")
            return True
        except Exception as e:
            print(f"[Music TTS Overlay Error]: {e}")
            return False
        finally:
            if was_paused and vc.is_connected() and vc.is_playing():
                vc.pause()

    async def audio_player_loop(self):
        while True:
            self.next_event.clear()

            if self.radio_mode:
                self._refill_radio_queue()

            if self.loop_mode == "song" and self.current:
                song = self.current
            else:
                if self.loop_mode == "queue" and self.current:
                    self.queue.append(self.current)

                if not self.queue:
                    self.current = None
                    try:
                        await asyncio.wait_for(self.next_event.wait(), timeout=300)
                    except asyncio.TimeoutError:
                        if self.voice_client and self.voice_client.is_connected():
                            await self.voice_client.disconnect()
                        break
                    continue

                song = self.queue.popleft()
                self.current = song

            try:
                # 스트림 URL 유효성 검사 및 지연 추출
                if not song.url:
                    await song.ensure_stream_url(self.bot.loop)

                if not discord.opus.is_loaded():
                    try:
                        discord.opus._load_default()
                    except Exception:
                        try:
                            discord.opus.load_opus("libopus.so.0")
                        except Exception as e:
                            print(f"[Music Warning] Opus load failed: {e}")

                # 플랫폼별 FFmpeg 옵션 분기: 유튜브는 Privoxy 프록시 필수 경유
                ffmpeg_opts = FFMPEG_YT_OPTIONS if "youtube" in song.platform.lower() else FFMPEG_SC_OPTIONS

                raw_source = discord.FFmpegPCMAudio(
                    song.url,
                    executable=FFMPEG_EXECUTABLE,
                    **ffmpeg_opts
                )
                running_loop = asyncio.get_running_loop()
                ducking_source = DuckingAudioSource(raw_source, self, loop=running_loop)
                self.current_source = ducking_source

                def after_playback(error):
                    if error:
                        print(f"[Music Error] {error}")
                    self.current_source = None
                    try:
                        running_loop.call_soon_threadsafe(self.next_event.set)
                    except Exception:
                        self.next_event.set()

                # TTS가 음성을 출력 중이면 잠시 대기 (최대 3초)
                tts_cog = self.bot.get_cog("TTSCog")
                if tts_cog and hasattr(tts_cog, "is_tts_playing") and tts_cog.is_tts_playing(self.guild.id):
                    for _ in range(30):
                        if not tts_cog.is_tts_playing(self.guild.id):
                            break
                        await asyncio.sleep(0.1)

                self.voice_client.play(ducking_source, after=after_playback)

                if self.text_channel and self.loop_mode != "song":
                    if self.skip_next_now_playing_message:
                        self.skip_next_now_playing_message = False
                    else:
                        embed = create_now_playing_embed(song, self)
                        view = MusicControlView(self)

                        # 라디오 모드: 매번 새 메시지를 보내지 않고 기존 재생 카드를 무한 수정(Edit)
                        if self.radio_mode and self.now_playing_message:
                            try:
                                await self.now_playing_message.edit(embed=embed, view=view)
                            except discord.NotFound:
                                # 이전 메시지가 삭제된 경우에만 새로 전송
                                try:
                                    self.now_playing_message = await self.text_channel.send(embed=embed, view=view)
                                except Exception as e:
                                    print(f"[Music Error sending radio now playing message]: {e}")
                            except Exception as edit_err:
                                print(f"[Music Error editing radio message]: {edit_err}")
                                try:
                                    self.now_playing_message = await self.text_channel.send(embed=embed, view=view)
                                except Exception:
                                    pass
                        else:
                            # 일반 모드: 이전 메시지의 컨트롤 뷰 제거 후 새 메시지 전송
                            if self.now_playing_message:
                                try:
                                    await self.now_playing_message.edit(view=None)
                                except Exception:
                                    pass
                            try:
                                self.now_playing_message = await self.text_channel.send(embed=embed, view=view)
                            except Exception as e:
                                print(f"[Music Error sending now playing message]: {e}")

            except Exception as e:
                print(f"[Error playing song]: {e}")
                if self.text_channel:
                    await self.text_channel.send(f"⚠️ `{song.title}` 재생 중 오류가 발생했습니다: {e}")
                self.next_event.set()

            await self.next_event.wait()


class Music(commands.Cog):
    """유튜브/사운드클라우드 음악 재생 및 대기열/플레이리스트 Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: Dict[int, GuildMusicPlayer] = {}
        self.playlists: Dict[str, Any] = {}
        self.settings: Dict[str, Any] = {}
        self.load_playlists()
        self.load_settings()

    def load_settings(self):
        try:
            if MUSIC_SETTINGS_PATH.exists():
                with open(MUSIC_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    self.settings = json.load(f)
            else:
                self.settings = {}
        except Exception as e:
            print(f"[Error] Failed to load music settings: {e}")
            self.settings = {}

    def save_settings(self):
        try:
            with open(MUSIC_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Error] Failed to save music settings: {e}")

    def load_playlists(self):
        try:
            if PLAYLISTS_PATH.exists():
                with open(PLAYLISTS_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.playlists = data.get("playlists", {})
            else:
                self.playlists = {}
        except Exception as e:
            print(f"[Error] Failed to load playlists: {e}")
            self.playlists = {}

    def save_playlists(self):
        try:
            with open(PLAYLISTS_PATH, "w", encoding="utf-8") as f:
                json.dump({"playlists": self.playlists}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Error] Failed to save playlists: {e}")

    def get_player(self, guild: discord.Guild) -> GuildMusicPlayer:
        if guild.id not in self.players:
            guild_auto_leave = self.settings.get(str(guild.id), {}).get("auto_leave", True)
            self.players[guild.id] = GuildMusicPlayer(self.bot, guild, auto_leave=guild_auto_leave)
        return self.players[guild.id]

    def is_music_playing(self, guild_id: int) -> bool:
        """해당 길드에서 음악이 재생 중(또는 일시정지 중)인지 여부 반환"""
        player = self.players.get(guild_id)
        if not player:
            return False
        vc = player.voice_client or (player.guild.voice_client if player.guild else None)
        if not vc:
            return False
        player.voice_client = vc
        return bool(player.current and (vc.is_playing() or vc.is_paused()))

    # ----------------------------------------------------
    # 음성 채널 상태 감지 (혼자 남았을 때 자동 퇴장)
    # ----------------------------------------------------
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # 1. 봇 자신이 연결 끊겼을 때 대기열 및 플레이어 상태 정리
        if member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None:
                player = self.players.get(member.guild.id)
                if player:
                    player.current = None
                    player.voice_client = None
                    player.radio_mode = False
                    player.radio_remaining = None
                    player.queue.clear()
                    if player.auto_leave_task and not player.auto_leave_task.done():
                        player.auto_leave_task.cancel()
                        player.auto_leave_task = None
            return

        # 2. 다른 멤버가 이동/퇴장했을 때: 봇이 있는 채널에 사람이 남았는지 확인
        guild = member.guild
        vc: Optional[discord.VoiceClient] = guild.voice_client
        if not vc or not vc.channel:
            return

        # 봇이 참여 중인 채널과 무관한 이벤트면 패스
        if before.channel != vc.channel and after.channel != vc.channel:
            return

        player = self.get_player(guild)

        # 1. 라디오 모드인 경우: 음성 채널에 아무도 없어도 24시간 연속 On-Air 방송 유지 (자동 퇴장 안 함)
        if player.radio_mode:
            if player.auto_leave_task and not player.auto_leave_task.done():
                player.auto_leave_task.cancel()
                player.auto_leave_task = None
            return

        # 2. 일반 재생 중 autoleave 설정이 꺼져있는 경우: 퇴장하지 않음
        if not player.auto_leave:
            if player.auto_leave_task and not player.auto_leave_task.done():
                player.auto_leave_task.cancel()
                player.auto_leave_task = None
            return

        # 봇을 제외한 실제 유저 수 카운트
        human_members = [m for m in vc.channel.members if not m.bot]

        if len(human_members) == 0:
            # 채널에 아무도 없으면 30초 대기 후 자동 퇴장 예약
            if not player.auto_leave_task or player.auto_leave_task.done():
                player.auto_leave_task = self.bot.loop.create_task(self._auto_leave_delayed(player, vc))
        else:
            # 유저가 다시 채널에 남아있으면 자동 퇴장 예약 취소
            if player.auto_leave_task and not player.auto_leave_task.done():
                player.auto_leave_task.cancel()
                player.auto_leave_task = None

    async def _auto_leave_delayed(self, player: GuildMusicPlayer, vc: discord.VoiceClient, delay: int = 30):
        try:
            await asyncio.sleep(delay)
            if vc and vc.is_connected() and vc.channel:
                human_members = [m for m in vc.channel.members if not m.bot]
                # 라디오 모드가 켜져있거나 auto_leave가 꺼져있으면 절대 퇴장하지 않음
                if len(human_members) == 0 and player.auto_leave and not player.radio_mode:
                    player.queue.clear()
                    player.loop_mode = "off"
                    player.radio_mode = False
                    player.radio_remaining = None
                    player.current = None
                    await vc.disconnect()

                    tts_cog = self.bot.get_cog("TTSCog")
                    if tts_cog and hasattr(tts_cog, "cleanup_session"):
                        tts_cog.cleanup_session(player.guild.id)
                    if player.text_channel:
                        embed = discord.Embed(
                            title="👋 음성 채널 자동 퇴장",
                            description="음성 채널에 아무도 남아있지 않아 자동으로 퇴장하고 대기열을 정리했습니다.",
                            color=0x95A5A6
                        )
                        embed.set_footer(text="Discord Music & TTS Bot 🎵")
                        try:
                            await player.text_channel.send(embed=embed)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        finally:
            player.auto_leave_task = None

    async def ensure_voice(self, interaction: discord.Interaction) -> Optional[discord.VoiceClient]:
        """음성 채널 연결 확인 및 자동 참여"""
        if not interaction.user.voice or not interaction.user.voice.channel:
            if interaction.response.is_done():
                await interaction.followup.send("❌ 먼저 음성 채널에 입장해주세요!", ephemeral=True)
            else:
                await interaction.response.send_message("❌ 먼저 음성 채널에 입장해주세요!", ephemeral=True)
            return None

        voice_channel = interaction.user.voice.channel
        voice_client = interaction.guild.voice_client

        try:
            if voice_client is None:
                voice_client = await voice_channel.connect(timeout=10.0, self_deaf=True)
            elif voice_client.channel != voice_channel:
                await voice_client.move_to(voice_channel)

            # 음성 채널 음질 자동 최적화 (봇에 채널 관리 권한이 있고 96kbps 미만일 시 상향)
            if interaction.guild and interaction.guild.me.guild_permissions.manage_channels and voice_channel.bitrate < 96000:
                try:
                    await voice_channel.edit(bitrate=96000, reason="음악 고음질 최적화 (96kbps)")
                except Exception:
                    pass
        except Exception as e:
            err_msg = f"❌ 음성 채널 연결 실패: {e}"
            print(f"[Music Error] Voice connect error: {e}")
            if interaction.response.is_done():
                await interaction.followup.send(err_msg, ephemeral=True)
            else:
                await interaction.response.send_message(err_msg, ephemeral=True)
            return None

        return voice_client

    # ----------------------------------------------------
    # 재생 및 우선 예약 처리 공통 메서드 (이모지 반응 방식)
    # ----------------------------------------------------
    async def handle_play_command(self, interaction: discord.Interaction, 검색어: str, is_playnext: bool = False):
        # 1. 음성 채널 입장 여부 즉시 확인 (무한 로딩 스피너 방지)
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 먼저 음성 채널에 입장해주세요!", ephemeral=True)
            return

        await interaction.response.defer()

        voice_client = await self.ensure_voice(interaction)
        if not voice_client:
            return

        player = self.get_player(interaction.guild)
        player.voice_client = voice_client
        player.text_channel = interaction.channel

        try:
            platform, songs = await search_songs(검색어, interaction.user, self.bot.loop)
        except Exception as e:
            await interaction.followup.send(f"❌ 검색 및 음원 탐색 실패: {e}")
            return

        if not songs:
            await interaction.followup.send("❌ 검색 결과를 찾을 수 없습니다.")
            return

        # 2. URL 직접 입력인 경우 이모지 선택 없이 즉시 재생/우선 예약
        if 검색어.strip().startswith(("http://", "https://")):
            song = songs[0]
            try:
                await song.ensure_stream_url(self.bot.loop)
            except Exception as e:
                await interaction.followup.send(f"❌ 음원 스트림 추출 실패: {e}")
                return

            is_first_track = (
                player.current is None
                and len(player.queue) == 0
                and (player.voice_client is None or (not player.voice_client.is_playing() and not player.voice_client.is_paused()))
            )

            if is_playnext:
                player.queue.appendleft(song)
            else:
                player.queue.append(song)

            if is_first_track:
                player.skip_next_now_playing_message = True
            player.start_player()

            if is_first_track:
                embed = create_now_playing_embed(song, player)
                view = MusicControlView(player)
                msg = await interaction.followup.send(embed=embed, view=view)
                player.now_playing_message = msg
            else:
                if is_playnext:
                    embed = discord.Embed(
                        title="⚡ 우선 예약 등록 완료 (Play Next)",
                        description=f"[{song.title}]({song.webpage_url})\n\n현재 곡 재생이 끝나면 바로 이 곡이 재생됩니다!",
                        color=0xF1C40F
                    )
                else:
                    embed = discord.Embed(
                        title="🎶 대기열 추가 완료",
                        description=f"[{song.title}]({song.webpage_url})",
                        color=0x27C7FE
                    )
                    embed.add_field(name="현재 대기 번호", value=f"{len(player.queue)}번째", inline=True)

                embed.add_field(name="길이", value=format_duration(song.duration), inline=True)
                embed.add_field(name="신청자", value=interaction.user.display_name, inline=True)
                if "대체" in song.platform or "SoundCloud" in song.platform:
                    embed.add_field(name="플랫폼", value=song.platform_badge, inline=True)
                if song.thumbnail:
                    embed.set_thumbnail(url=song.thumbnail)
                embed.set_footer(text="Discord Music & TTS Bot 🎵")
                await interaction.followup.send(embed=embed)
            return

        # 3. 검색어인 경우: 상위 5곡 목록 임베드 + 이모지 반응 선택 제공
        embed = create_search_embed(검색어, platform, songs, interaction.user)
        message = await interaction.followup.send(embed=embed)

        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
        cancel_emoji = "❌"
        active_emojis = number_emojis[:len(songs)] + [cancel_emoji]

        # 백그라운드 태스크로 이모지 순차 등록 (반응 추가 중에도 사용자가 바로 클릭 가능)
        async def add_reactions_task():
            for emoji in active_emojis:
                try:
                    await message.add_reaction(emoji)
                except Exception:
                    break

        reaction_bg_task = asyncio.create_task(add_reactions_task())

        def reaction_check(reaction: discord.Reaction, user: discord.User | discord.Member):
            return (
                reaction.message.id == message.id
                and user.id == interaction.user.id
                and str(reaction.emoji) in active_emojis
            )

        try:
            reaction, user = await self.bot.wait_for("reaction_add", timeout=60.0, check=reaction_check)
        except asyncio.TimeoutError:
            try:
                await message.clear_reactions()
            except Exception:
                pass
            try:
                await message.edit(content="⏰ 곡 선택 시간이 초과되었습니다 (60초).", embed=None)
            except Exception:
                pass
            return
        finally:
            if not reaction_bg_task.done():
                reaction_bg_task.cancel()

        try:
            await message.clear_reactions()
        except Exception:
            pass

        chosen_emoji = str(reaction.emoji)
        if chosen_emoji == cancel_emoji:
            await message.edit(content="❌ 곡 선택이 취소되었습니다.", embed=None)
            return

        if chosen_emoji in number_emojis:
            index = number_emojis.index(chosen_emoji)
            selected_song = songs[index]

            await message.edit(content=f"⏳ **{index + 1}번** 곡을 불러오는 중입니다...", embed=None)

            try:
                await selected_song.ensure_stream_url(self.bot.loop)
            except Exception as e:
                await message.edit(content=f"⚠️ `{selected_song.title}` 음원 추출 실패: {e}", embed=None)
                return

            is_first_track = (
                player.current is None
                and len(player.queue) == 0
                and (player.voice_client is None or (not player.voice_client.is_playing() and not player.voice_client.is_paused()))
            )

            if is_playnext:
                player.queue.appendleft(selected_song)
            else:
                if player.radio_mode:
                    first_radio_idx = None
                    for idx, s in enumerate(player.queue):
                        if getattr(s, "is_radio", False):
                            first_radio_idx = idx
                            break
                    if first_radio_idx is not None:
                        player.queue.insert(first_radio_idx, selected_song)
                    else:
                        player.queue.append(selected_song)
                else:
                    player.queue.append(selected_song)

            if is_first_track:
                player.skip_next_now_playing_message = True
            player.start_player()

            # 자동 TTS 세션 연결
            tts_cog = self.bot.get_cog("TTSCog")
            if tts_cog and hasattr(tts_cog, "bind_session") and voice_client and voice_client.channel:
                tts_cog.bind_session(interaction.guild.id, interaction.channel_id, voice_client.channel.id)

            notice = " (⚠️ 유튜브 봇 방지로 사운드클라우드 대체 음원 재생)" if selected_song.fallback_used else ""

            if is_first_track:
                embed = create_now_playing_embed(selected_song, player)
                view = MusicControlView(player)
                await message.edit(content=f"✅ **{index + 1}번** 곡을 선택하여 즉시 재생합니다!{notice}", embed=embed, view=view)
                player.now_playing_message = message
            else:
                if is_playnext:
                    embed = discord.Embed(
                        title="⚡ 우선 예약 등록 완료 (Play Next)",
                        description=f"[{selected_song.title}]({selected_song.webpage_url})\n\n현재 곡 재생이 끝나면 바로 이 곡이 재생됩니다!",
                        color=0xF1C40F
                    )
                else:
                    embed = discord.Embed(
                        title="🎶 대기열 추가 완료",
                        description=f"[{selected_song.title}]({selected_song.webpage_url})",
                        color=0x27C7FE
                    )
                    embed.add_field(name="현재 대기 번호", value=f"{len(player.queue)}번째", inline=True)

                embed.add_field(name="길이", value=format_duration(selected_song.duration), inline=True)
                embed.add_field(name="신청자", value=interaction.user.display_name, inline=True)
                if "대체" in selected_song.platform or "SoundCloud" in selected_song.platform:
                    embed.add_field(name="플랫폼", value=selected_song.platform_badge, inline=True)
                if selected_song.thumbnail:
                    embed.set_thumbnail(url=selected_song.thumbnail)
                embed.set_footer(text="Discord Music & TTS Bot 🎵")

                await message.edit(content=f"✅ **{index + 1}번** 곡이 등록되었습니다!{notice}", embed=embed)

    # ----------------------------------------------------
    # 재생 명령어 (/play)
    # ----------------------------------------------------
    @app_commands.command(name="play", description="음악을 검색(상위 5곡 이모지 선택)하거나 URL을 대기열에 추가합니다.")
    @app_commands.describe(검색어="노래 제목 또는 유튜브/사운드클라우드 링크")
    async def play(self, interaction: discord.Interaction, 검색어: str):
        await self.handle_play_command(interaction, 검색어, is_playnext=False)

    # ----------------------------------------------------
    # 우선 예약 재생 명령어 (/playnext)
    # ----------------------------------------------------
    @app_commands.command(name="playnext", description="곡을 대기열 맨 앞에 우선 예약하여 현재 곡 다음으로 즉시 재생합니다.")
    @app_commands.describe(검색어="우선 예약할 노래 제목 또는 유튜브/사운드클라우드 링크")
    async def playnext(self, interaction: discord.Interaction, 검색어: str):
        await self.handle_play_command(interaction, 검색어, is_playnext=True)

    # ----------------------------------------------------
    # 재생 제어 명령어들
    # ----------------------------------------------------
    @app_commands.command(name="skip", description="현재 재생 중인 노래를 건너뜁니다.")
    async def skip(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if not player.voice_client or not player.voice_client.is_playing():
            await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)
            return

        player.loop_mode = "off" if player.loop_mode == "song" else player.loop_mode
        player.voice_client.stop()
        await interaction.response.send_message("⏭️ 현재 곡을 건너뛰었습니다!")

    @app_commands.command(name="pause", description="음악 재생을 일시정지합니다.")
    async def pause(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if player.voice_client and player.voice_client.is_playing():
            player.voice_client.pause()
            await interaction.response.send_message("⏸️ 음악을 일시정지했습니다.")
        else:
            await interaction.response.send_message("❌ 재생 중인 음악이 없거나 이미 일시정지 상태입니다.", ephemeral=True)

    @app_commands.command(name="resume", description="일시정지된 음악을 다시 재생합니다.")
    async def resume(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if player.voice_client and player.voice_client.is_paused():
            player.voice_client.resume()
            await interaction.response.send_message("▶️ 음악 재생을 다시 시작합니다.")
        else:
            await interaction.response.send_message("❌ 일시정지된 음악이 없습니다.", ephemeral=True)

    @app_commands.command(name="stop", description="재생을 중단하고 대기열을 모두 비운 뒤 퇴장합니다.")
    async def stop(self, interaction: discord.Interaction):
        await interaction.response.defer()
        player = self.get_player(interaction.guild)
        player.queue.clear()
        player.loop_mode = "off"
        player.radio_mode = False
        player.radio_remaining = None
        player.current = None
        if player.now_playing_message:
            try:
                await player.now_playing_message.edit(view=None)
            except Exception:
                pass
            player.now_playing_message = None

        tts_cog = self.bot.get_cog("TTSCog")
        is_tts = tts_cog and hasattr(tts_cog, "is_tts_active") and tts_cog.is_tts_active(interaction.guild.id)

        if player.voice_client:
            if player.voice_client.is_playing() or player.voice_client.is_paused():
                player.voice_client.stop()

            if is_tts:
                await interaction.followup.send("⏹️ 음악을 중지하고 대기열을 비웠습니다. (TTS 세션은 유지됩니다. 봇 퇴장은 `/tts leave` 사용)")
            else:
                await player.voice_client.disconnect()
                await interaction.followup.send("⏹️ 음악을 중지하고 음성 채널에서 퇴장했습니다.")
        else:
            await interaction.followup.send("❌ 봇이 음성 채널에 연결되어 있지 않습니다.", ephemeral=True)

    @app_commands.command(name="queue", description="현재 음악 대기열을 확인합니다.")
    async def queue(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)

        if not player.current and not player.queue:
            await interaction.response.send_message("📭 현재 대기열에 곡이 없습니다.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📜 음악 재생 대기열 (Music Queue)",
            color=0x27C7FE
        )

        if player.current:
            cur_badge = f" - {player.current.platform_badge}" if ("대체" in player.current.platform or "SoundCloud" in player.current.platform) else ""
            is_cur_radio = bool(getattr(player.current, "is_radio", False) or player.radio_mode)
            cur_radio = " `[📻라디오]`" if is_cur_radio else ""
            req_str = "" if is_cur_radio else f" | 신청: {player.current.requester.display_name}"
            embed.add_field(
                name="▶️ 현재 재생 중",
                value=f"[{player.current.title}]({player.current.webpage_url}) (`{format_duration(player.current.duration)}`){cur_radio}{cur_badge}{req_str}",
                inline=False
            )

        if player.queue:
            queue_list = []
            for i, song in enumerate(list(player.queue)[:10], start=1):
                badge = f" - {song.platform_badge}" if ("대체" in song.platform or "SoundCloud" in song.platform) else ""
                radio_tag = " `[📻라디오]`" if getattr(song, "is_radio", False) else ""
                queue_list.append(f"`{i}.` [{song.title}]({song.webpage_url}) (`{format_duration(song.duration)}`){radio_tag}{badge}")

            desc = "\n".join(queue_list)
            if len(player.queue) > 10:
                desc += f"\n... 외 {len(player.queue) - 10}곡 더 있음"

            embed.add_field(name=f"대기 목록 (총 {len(player.queue)}곡)", value=desc, inline=False)
        else:
            embed.add_field(name="대기 목록", value="대기 중인 다음 곡이 없습니다.", inline=False)

        if player.radio_mode:
            rem_str = "무한 방송" if player.radio_remaining is None else f"{player.radio_remaining}곡 남음"
            embed.add_field(name="라디오 모드", value=f"📻 ON ({rem_str})", inline=True)
        else:
            embed.add_field(name="반복 모드", value=f"`{player.loop_mode.upper()}`", inline=True)
        embed.set_footer(text="Discord Music & TTS Bot 🎵")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="nowplaying", description="현재 재생 중인 곡의 정보와 대화형 컨트롤러 버튼을 확인합니다.")
    async def nowplaying(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if not player.current or not player.voice_client or (not player.voice_client.is_playing() and not player.voice_client.is_paused()):
            await interaction.response.send_message("❌ 현재 재생 중인 음악이 없습니다.", ephemeral=True)
            return

        song = player.current
        embed = create_now_playing_embed(song, player)
        view = MusicControlView(player)
        await interaction.response.send_message(embed=embed, view=view)

        if player.radio_mode:
            try:
                msg = await interaction.original_response()
                if player.now_playing_message and player.now_playing_message.id != msg.id:
                    try:
                        await player.now_playing_message.edit(view=None)
                    except Exception:
                        pass
                player.now_playing_message = msg
            except Exception:
                pass

    @app_commands.command(name="volume", description="음악 볼륨을 조절합니다 (1 ~ 150%).")
    @app_commands.describe(수치="볼륨 크기 (1~150 사이 정수, 기본값 80%)")
    async def volume_cmd(self, interaction: discord.Interaction, 수치: app_commands.Range[int, 1, 150]):
        player = self.get_player(interaction.guild)
        player.volume = 수치 / 100.0
        await interaction.response.send_message(f"🔊 음악 볼륨이 **{수치}%**(으)로 설정되었습니다.")

    @app_commands.command(name="shuffle", description="대기열의 곡 순서를 무작위로 섞습니다.")
    async def shuffle(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if len(player.queue) < 2:
            await interaction.response.send_message("❌ 셔플하려면 대기열에 최소 2곡 이상 있어야 합니다.", ephemeral=True)
            return

        temp_list = list(player.queue)
        random.shuffle(temp_list)
        player.queue = deque(temp_list)
        await interaction.response.send_message(f"🔀 대기열의 {len(player.queue)}곡을 성공적으로 섞었습니다!")

    @app_commands.command(name="loop", description="반복 재생 모드를 설정합니다.")
    @app_commands.choices(모드=[
        app_commands.Choice(name="반복 끄기 (Off)", value="off"),
        app_commands.Choice(name="한 곡 반복 (Song)", value="song"),
        app_commands.Choice(name="전체 대기열 반복 (Queue)", value="queue"),
    ])
    async def loop_cmd(self, interaction: discord.Interaction, 모드: app_commands.Choice[str]):
        player = self.get_player(interaction.guild)
        player.loop_mode = 모드.value
        await interaction.response.send_message(f"🔁 반복 모드가 **{모드.name}**으로 설정되었습니다.")

    # ----------------------------------------------------
    # 자동 퇴장 토글 명령어 (/autoleave)
    # ----------------------------------------------------
    @app_commands.command(name="autoleave", description="음성 채널에 아무도 없을 때 봇 자동 퇴장 여부를 설정하거나 토글합니다.")
    @app_commands.describe(상태="자동 퇴장 켜기(On) 또는 끄기(Off) 선택 (미선택 시 현재 상태 토글)")
    @app_commands.choices(상태=[
        app_commands.Choice(name="켜기 (On) - 아무도 없으면 자동 퇴장", value="on"),
        app_commands.Choice(name="끄기 (Off) - 혼자 남아도 퇴장하지 않음", value="off"),
    ])
    async def toggle_autoleave(self, interaction: discord.Interaction, 상태: Optional[app_commands.Choice[str]] = None):
        player = self.get_player(interaction.guild)

        if 상태 is not None:
            new_state = (상태.value == "on")
        else:
            new_state = not player.auto_leave

        player.auto_leave = new_state
        self.settings.setdefault(str(interaction.guild.id), {})["auto_leave"] = new_state
        self.save_settings()

        if not new_state and player.auto_leave_task and not player.auto_leave_task.done():
            player.auto_leave_task.cancel()
            player.auto_leave_task = None

        state_str = "🟢 **켜짐 (On)**" if new_state else "🔴 **꺼짐 (Off)**"
        desc = "음성 채널에 아무도 없으면 30초 후 자동으로 퇴장합니다." if new_state else "음성 채널에 혼자 남아도 자동으로 퇴장하지 않습니다."

        embed = discord.Embed(
            title="⚙️ 자동 퇴장 (Auto-Leave) 설정 변경",
            description=(
                f"자동 퇴장 기능이 {state_str}으로 설정되었습니다.\n{desc}\n\n"
                f"📻 **참고**: 스텔라이브 라디오(`/radio`) 모드는 24시간 On-Air 방송 특성상 "
                f"이 설정과 무관하게 채널에 사람이 없어도 퇴장하지 않고 항상 계속 재생됩니다."
            ),
            color=0x2ECC71 if new_state else 0xE74C3C
        )
        embed.set_footer(text="Discord Music & TTS Bot 🎵")
        await interaction.response.send_message(embed=embed)

    # ----------------------------------------------------
    # 플레이리스트 관리 그룹 (/playlist)
    # ----------------------------------------------------
    playlist_group = app_commands.Group(name="playlist", description="자체 플레이리스트 관리 및 재생")

    @playlist_group.command(name="list", description="저장된 플레이리스트 목록을 확인합니다.")
    async def pl_list(self, interaction: discord.Interaction):
        self.load_playlists()
        if not self.playlists:
            await interaction.response.send_message("📂 등록된 플레이리스트가 없습니다. `/playlist create`로 만들어보세요!", ephemeral=True)
            return

        embed = discord.Embed(
            title="📂 저장된 플레이리스트 목록",
            description="`/playlist load [이름]` 명령어로 대기열에 바로 불러올 수 있습니다.",
            color=0x27C7FE
        )
        for key, pl in self.playlists.items():
            songs = pl.get("songs", [])
            desc = pl.get("description", "설명 없음")
            embed.add_field(
                name=f"🎵 {pl.get('name', key)} (`{key}`)",
                value=f"**곡 수**: {len(songs)}곡 | **설명**: {desc}",
                inline=False
            )
        embed.set_footer(text="playlists.json 파일에서 직접 수정할 수도 있습니다 🌸")
        await interaction.response.send_message(embed=embed)

    @playlist_group.command(name="load", description="플레이리스트의 모든 곡을 대기열에 추가합니다.")
    @app_commands.describe(이름="불러올 플레이리스트의 식별자(코드명)")
    async def pl_load(self, interaction: discord.Interaction, 이름: str):
        self.load_playlists()
        key = 이름.strip().lower()
        if key not in self.playlists:
            await interaction.response.send_message(f"❌ '{이름}' 플레이리스트를 찾을 수 없습니다. `/playlist list`를 확인해주세요.", ephemeral=True)
            return

        await interaction.response.defer()
        voice_client = await self.ensure_voice(interaction)
        if not voice_client:
            return

        player = self.get_player(interaction.guild)
        player.voice_client = voice_client
        player.text_channel = interaction.channel

        pl = self.playlists[key]
        songs_data = pl.get("songs", [])
        if not songs_data:
            await interaction.followup.send("⚠️ 해당 플레이리스트에 등록된 곡이 없습니다.")
            return

        added_count = 0
        for item in songs_data:
            url = item.get("url")
            title = item.get("title", url)
            dummy_data = {
                "title": title,
                "url": None,  # 재생 시점에 추출/대체
                "webpage_url": url,
                "duration": item.get("duration", 0),
                "thumbnail": None,
                "uploader": "Playlist",
            }
            platform = "SoundCloud" if "soundcloud" in (url or "").lower() else "YouTube"
            song = Song(dummy_data, interaction.user, platform=platform)
            player.queue.append(song)
            added_count += 1

        player.start_player()

        embed = discord.Embed(
            title="🎉 플레이리스트 로드 완료!",
            description=f"**{pl.get('name', key)}**의 **{added_count}곡**이 대기열에 추가되었습니다.",
            color=0x2ECC71
        )
        embed.set_footer(text="Discord Music & TTS Bot 🎵")
        await interaction.followup.send(embed=embed)

    @playlist_group.command(name="create", description="새로운 플레이리스트를 생성합니다.")
    @app_commands.describe(코드명="영문/숫자 식별자 (예: my_playlist)", 이름="표시할 이름", 설명="플레이리스트 설명")
    async def pl_create(self, interaction: discord.Interaction, 코드명: str, 이름: str, 설명: Optional[str] = "내 커스텀 플레이리스트"):
        self.load_playlists()
        key = 코드명.strip().lower()
        if key in self.playlists:
            await interaction.response.send_message(f"❌ '{key}' 코드명의 플레이리스트가 이미 존재합니다.", ephemeral=True)
            return

        self.playlists[key] = {
            "name": 이름,
            "description": 설명,
            "songs": []
        }
        self.save_playlists()
        await interaction.response.send_message(f"✅ 새 플레이리스트 **{이름}** (`{key}`)이 생성되었습니다!\n`/playlist add {key} <음악 검색어 또는 URL>`로 곡을 추가해보세요.")

    @playlist_group.command(name="add", description="플레이리스트에 유튜브/사운드클라우드 곡을 추가합니다.")
    @app_commands.describe(코드명="추가할 플레이리스트 식별자", 검색어="추가할 노래 제목 또는 링크")
    async def pl_add(self, interaction: discord.Interaction, 코드명: str, 검색어: str):
        self.load_playlists()
        key = 코드명.strip().lower()
        if key not in self.playlists:
            await interaction.response.send_message(f"❌ '{코드명}' 플레이리스트를 찾을 수 없습니다.", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            song = await Song.from_query(검색어, interaction.user, self.bot.loop)
        except Exception as e:
            await interaction.followup.send(f"❌ 곡 정보 추출 실패: {e}")
            return

        if not song:
            await interaction.followup.send("❌ 곡을 찾을 수 없습니다.")
            return

        self.playlists[key]["songs"].append({
            "title": song.title,
            "url": song.webpage_url,
            "duration": song.duration
        })
        self.save_playlists()

        badge = f" - {song.platform_badge}" if ("대체" in song.platform or "SoundCloud" in song.platform) else ""
        await interaction.followup.send(
            f"✅ **{self.playlists[key]['name']}**에 곡이 추가되었습니다!\n🎵 [{song.title}]({song.webpage_url}) (`{format_duration(song.duration)}`){badge}"
        )

    # ----------------------------------------------------
    # 스텔라이브 라디오 관리 그룹 (/radio)
    # ----------------------------------------------------
    radio_group = app_commands.Group(name="radio", description="스텔라이브 24시간 라디오 및 음원 방송")

    @radio_group.command(name="play", description="스텔라이브 오리지널 및 커버곡 24시간 라디오를 재생합니다.")
    @app_commands.describe(
        선택="재생할 스텔라이브 음원 카테고리 또는 아티스트",
        재생모드="무한 연속 라디오 또는 지정 곡 수 재생"
    )
    @app_commands.choices(선택=RADIO_CATEGORY_CHOICES, 재생모드=RADIO_MODE_CHOICES)
    async def radio_play(
        self,
        interaction: discord.Interaction,
        선택: Optional[app_commands.Choice[str]] = None,
        재생모드: Optional[app_commands.Choice[str]] = None
    ):
        await interaction.response.defer()

        voice_client = await self.ensure_voice(interaction)
        if not voice_client:
            return

        filter_key = 선택.value if 선택 else "all"
        mode_val = 재생모드.value if 재생모드 else "infinite"

        songs, db = get_stellive_songs(filter_key)
        if not songs:
            await interaction.followup.send(f"❌ '{filter_key}' 카테고리에 해당하는 곡을 찾을 수 없습니다.")
            return

        player = self.get_player(interaction.guild)
        player.voice_client = voice_client
        player.text_channel = interaction.channel
        player.radio_mode = True
        player.radio_filter = filter_key
        player.radio_requester = interaction.user
        player.radio_remaining = None if mode_val == "infinite" else int(mode_val)

        # 라디오 곡 즉시 보충 및 재생 시작
        player._refill_radio_queue()
        player.next_event.set()
        player.start_player()

        # 자동 TTS 세션 연결 (라디오 채팅방과 TTS 즉시 연동)
        tts_cog = self.bot.get_cog("TTSCog")
        if tts_cog and hasattr(tts_cog, "bind_session") and voice_client and voice_client.channel:
            tts_cog.bind_session(interaction.guild.id, interaction.channel_id, voice_client.channel.id)

        filter_name, theme_color = RADIO_CHOICES_DESC.get(filter_key, ("스텔라이브 음원", 0x9B59B6))
        mode_str = "24시간 무한 연속 On-Air" if mode_val == "infinite" else f"{mode_val}곡 재생 후 자동 종료"

        embed = discord.Embed(
            title="📻 스텔라이브 라디오 방송 시작! (On-Air)",
            description=(
                "스텔라이브 공식 및 멤버들의 음원을 24시간 라디오 형식으로 송출합니다.\n"
                "• **24시간 무중단 On-Air**: 음성 채널에 아무도 없어도 퇴장하지 않고 계속 방송이 유지됩니다 📡\n"
                "• **TTS 자동 연동**: 이 채팅방에 메시지를 입력하면 라디오 방송 중 실시간으로 읽어줍니다 🎧"
            ),
            color=theme_color
        )
        embed.add_field(name="선택 카테고리", value=f"**{filter_name}** ({len(songs)}곡 보유)", inline=True)
        embed.add_field(name="방송 모드", value=mode_str, inline=True)
        embed.add_field(name="라디오 DJ", value=interaction.user.display_name, inline=True)
        embed.set_footer(text="스텔라이브 24시간 라디오 🌟 • /radio stop으로 라디오 종료")

        await interaction.followup.send(embed=embed)

    @radio_group.command(name="stop", description="스텔라이브 라디오 자동 선곡 모드를 종료합니다.")
    async def radio_stop(self, interaction: discord.Interaction):
        player = self.get_player(interaction.guild)
        if not player.radio_mode:
            await interaction.response.send_message("❌ 현재 이 서버에서 라디오가 켜져 있지 않습니다.", ephemeral=True)
            return

        player.radio_mode = False
        player.radio_remaining = None
        await interaction.response.send_message("📻 스텔라이브 라디오 자동 선곡 모드가 종료되었습니다.\n대기열에 남은 곡이 모두 재생되면 정지됩니다. (즉시 정지는 `/stop`)")

    @radio_group.command(name="channels", description="등록된 스텔라이브 아티스트 채널 및 공식 플레이리스트 정보를 확인합니다.")
    async def radio_channels(self, interaction: discord.Interaction):
        db = load_stellive_db()
        artists = db.get("artists", {})
        playlists = db.get("official_playlists", {})

        embed = discord.Embed(
            title="🌟 스텔라이브 아티스트 공식 유튜브 채널 목록",
            description="데이터베이스에 등록된 스텔라이브 멤버들의 공식 채널 및 플레이리스트 링크입니다.",
            color=0x9B59B6
        )

        gen_groups = {
            "공식": ["official"],
            "1기생 (Everys)": ["yuni", "kanna", "fuya"],
            "2기생 (UNIVERSE)": ["hina", "mashiro", "lize", "tabi"],
            "3기생 (CLICHÉ)": ["shibuki", "rin", "nana", "riko"],
        }

        for gen_title, keys in gen_groups.items():
            lines = []
            for k in keys:
                art = artists.get(k)
                if art:
                    lines.append(f"{art['emoji']} **[{art['name']}]({art['channel_url']})** (`{art['handle']}`)")
            if lines:
                embed.add_field(name=f"📌 {gen_title}", value="\n".join(lines), inline=False)

        pl_lines = []
        if playlists.get("original"):
            pl_lines.append(f"💿 **[스텔라이브 공식 오리지널 플레이리스트]({playlists['original']})**")
        if playlists.get("cover"):
            pl_lines.append(f"🎤 **[스텔라이브 공식 커버곡 플레이리스트]({playlists['cover']})**")
        if playlists.get("fuya_cover"):
            pl_lines.append(f"💜 **[사키하네 후야 공식 커버곡 플레이리스트]({playlists['fuya_cover']})**")
        if pl_lines:
            embed.add_field(name="🎶 공식 플레이리스트", value="\n".join(pl_lines), inline=False)

        embed.set_footer(text="`/radio play`로 해당 채널들의 모든 음원을 24시간 감상할 수 있습니다 🎵")
        await interaction.response.send_message(embed=embed)

    @radio_group.command(name="stats", description="스텔라이브 음원 통계 및 현재 서버 라디오 상태를 확인합니다.")
    async def radio_stats(self, interaction: discord.Interaction):
        db = load_stellive_db()
        songs = db.get("songs", [])
        artists = db.get("artists", {})
        player = self.players.get(interaction.guild.id)

        orig_cnt = sum(1 for s in songs if s.get("type") == "original")
        cover_cnt = sum(1 for s in songs if s.get("type") == "cover")

        embed = discord.Embed(
            title="📊 스텔라이브 라디오 데이터베이스 통계",
            color=0x9B59B6
        )
        embed.add_field(name="총 등록 음원", value=f"🎵 **{len(songs)}곡**", inline=True)
        embed.add_field(name="오리지널 / 발매곡", value=f"💿 **{orig_cnt}곡**", inline=True)
        embed.add_field(name="커버곡", value=f"🎤 **{cover_cnt}곡**", inline=True)

        member_counts = []
        for k, a in artists.items():
            if k == "official":
                continue
            cnt = sum(1 for s in songs if k in s.get("artists", []))
            member_counts.append(f"{a['emoji']} {a['korean_name']}: `{cnt}곡`")
        embed.add_field(name="멤버별 보유 곡 수", value=" • ".join(member_counts), inline=False)

        if player and player.radio_mode:
            f_name, _ = RADIO_CHOICES_DESC.get(player.radio_filter, (player.radio_filter, 0))
            rem_str = "무한 방송" if player.radio_remaining is None else f"{player.radio_remaining}곡 남음"
            radio_status = f"🔴 **On-Air 송출 중** ({f_name} / {rem_str})"
        else:
            radio_status = "⚪ **라디오 대기 중** (`/radio play`로 시작)"
        embed.add_field(name="현재 서버 상태", value=radio_status, inline=False)
        embed.add_field(name="최종 업데이트", value=db.get("last_updated", "알 수 없음"), inline=True)
        embed.set_footer(text="Discord Music & TTS Bot 🎵")

        await interaction.response.send_message(embed=embed)

    @radio_group.command(name="add", description="스텔라이브 신곡/커버곡 유튜브 링크를 데이터베이스에 직접 추가합니다.")
    @app_commands.describe(
        유튜브_링크="추가할 유튜브 영상 링크",
        음원_타입="오리지널 또는 커버곡 선택",
        아티스트="참여 아티스트 (기본값: 자동 판별)"
    )
    @app_commands.choices(
        음원_타입=[
            app_commands.Choice(name="💿 오리지널 / 발매곡", value="original"),
            app_commands.Choice(name="🎤 커버곡", value="cover"),
        ],
        아티스트=[
            app_commands.Choice(name="🌟 자동 감지", value="auto"),
            app_commands.Choice(name="🦄 유니", value="yuni"),
            app_commands.Choice(name="🐉 칸나", value="kanna"),
            app_commands.Choice(name="💜 후야", value="fuya"),
            app_commands.Choice(name="❄️ 히나", value="hina"),
            app_commands.Choice(name="🐱 마시로", value="mashiro"),
            app_commands.Choice(name="🍷 리제", value="lize"),
            app_commands.Choice(name="🐾 타비", value="tabi"),
            app_commands.Choice(name="🦊 시부키", value="shibuki"),
            app_commands.Choice(name="☁️ 린", value="rin"),
            app_commands.Choice(name="🌸 나나", value="nana"),
            app_commands.Choice(name="🍋 리코", value="riko"),
            app_commands.Choice(name="👥 단체 / 공식", value="official"),
        ]
    )
    async def radio_add(
        self,
        interaction: discord.Interaction,
        유튜브_링크: str,
        음원_타입: app_commands.Choice[str],
        아티스트: Optional[app_commands.Choice[str]] = None
    ):
        await interaction.response.defer()
        url = 유튜브_링크.strip()

        def _extract():
            y = yt_dlp.YoutubeDL({**YTDL_YT_OPTIONS, 'quiet': True, 'extract_flat': True})
            return y.extract_info(url, download=False)

        try:
            info = await self.bot.loop.run_in_executor(None, _extract)
        except Exception as e:
            await interaction.followup.send(f"❌ 유튜브 영상 정보를 불러오지 못했습니다: {e}")
            return

        vid = info.get("id")
        title = info.get("title")
        duration = info.get("duration")

        if not vid or not title:
            await interaction.followup.send("❌ 유효한 유튜브 동영상이 아닙니다.")
            return

        db = load_stellive_db()
        songs = db.get("songs", [])

        existing = next((s for s in songs if s["id"] == vid), None)
        if existing:
            await interaction.followup.send(f"⚠️ 이미 데이터베이스에 등록된 곡입니다: **{existing['title']}**")
            return

        artist_val = 아티스트.value if 아티스트 else "auto"
        if artist_val == "auto":
            artist_list = detect_artists(title)
        else:
            artist_list = [artist_val]

        new_song = {
            "id": vid,
            "title": title,
            "url": f"https://www.youtube.com/watch?v={vid}",
            "type": 음원_타입.value,
            "artists": artist_list,
            "duration": duration
        }
        songs.append(new_song)
        db["songs"] = songs
        db["last_updated"] = time.strftime("%Y-%m-%d %H:%M:%S")

        try:
            with open(STELLIVE_DB_PATH, "w", encoding="utf-8") as f:
                json.dump(db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            await interaction.followup.send(f"❌ DB 저장 실패: {e}")
            return

        embed = discord.Embed(
            title="✅ 스텔라이브 음원 추가 완료!",
            description=f"### [{title}](https://www.youtube.com/watch?v={vid})",
            color=0x2ECC71
        )
        embed.set_thumbnail(url=f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
        embed.add_field(name="음원 유형", value=음원_타입.name, inline=True)
        embed.add_field(name="길이", value=format_duration(duration), inline=True)
        embed.add_field(name="등록 아티스트", value=", ".join(artist_list), inline=True)
        embed.set_footer(text=f"현재 총 {len(songs)}곡 등록됨 🌸")
        await interaction.followup.send(embed=embed)

    @radio_group.command(name="sync", description="스텔라이브 공식 플레이리스트에서 최신 곡 목록을 다시 동기화합니다.")
    async def radio_sync(self, interaction: discord.Interaction):
        await interaction.response.defer()

        def _sync_worker():
            import sys
            proj_dir = str(DATA_DIR.parent)
            if proj_dir not in sys.path:
                sys.path.insert(0, proj_dir)
            import build_stellive_db
            build_stellive_db.build_database()
            return load_stellive_db()

        try:
            updated_db = await self.bot.loop.run_in_executor(None, _sync_worker)
            songs_count = len(updated_db.get("songs", []))
            embed = discord.Embed(
                title="🔄 스텔라이브 데이터베이스 동기화 완료!",
                description=f"공식 유튜브 오리지널 및 커버곡 플레이리스트를 다시 검색하여 최신 곡 목록을 갱신했습니다.\n\n📊 **현재 총 등록 곡 수**: `{songs_count}곡`",
                color=0x2ECC71
            )
            embed.set_footer(text=f"동기화 시각: {updated_db.get('last_updated', '')}")
            await interaction.followup.send(embed=embed)
        except Exception as e:
            await interaction.followup.send(f"❌ 동기화 중 오류가 발생했습니다: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
