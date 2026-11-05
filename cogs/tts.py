import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

import discord
from discord import app_commands
from discord.ext import commands
import edge_tts

# ----------------------------------------------------
# 설정 및 상수
# ----------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
TTS_SETTINGS_PATH = DATA_DIR / "tts_settings.json"
TTS_CACHE_DIR = DATA_DIR / "tts_cache"
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

def find_ffmpeg_executable() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        Path(local_app_data) / "Microsoft/WinGet/Links/ffmpeg.exe",
        Path("C:/ffmpeg/bin/ffmpeg.exe"),
        Path("C:/Program Files/ffmpeg/bin/ffmpeg.exe"),
        Path("/usr/bin/ffmpeg"),
        Path("/usr/local/bin/ffmpeg"),
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    return "ffmpeg"

FFMPEG_EXECUTABLE = find_ffmpeg_executable()

VOICE_CHOICES = {
    "ko-KR-SunHiNeural": {
        "name": "선희 (여성 한국어)",
        "emoji": "🌸",
        "desc": "맑고 또렷한 한국어 여성 목소리 (기본값)",
        "lang": "ko"
    },
    "ko-KR-InJoonNeural": {
        "name": "인준 (남성 한국어)",
        "emoji": "🎙️",
        "desc": "차분하고 깔끔한 한국어 남성 목소리",
        "lang": "ko"
    },
    "ko-KR-HyunsuMultilingualNeural": {
        "name": "현수 (남성 다국어)",
        "emoji": "🌐",
        "desc": "자연스러운 한국어/다국어 남성 목소리",
        "lang": "ko"
    },
    "ja-JP-NanamiNeural": {
        "name": "나나미 (여성 일본어)",
        "emoji": "🎀",
        "desc": "귀여운 일본어 여성 목소리",
        "lang": "ja"
    },
    "ja-JP-KeitaNeural": {
        "name": "케이타 (남성 일본어)",
        "emoji": "🎮",
        "desc": "부드러운 일본어 남성 목소리",
        "lang": "ja"
    }
}
DEFAULT_VOICE = "ko-KR-SunHiNeural"
DEFAULT_SPEED = "+0%"


class TTSCog(commands.Cog):
    """음성 채널 텍스트 음성 변환 (TTS) Cog"""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.settings: Dict[str, Any] = self.load_settings()
        # guild_id -> { "channel_id": int, "voice_channel_id": int }
        self.active_sessions: Dict[int, Dict[str, Any]] = {}
        if "active_sessions" in self.settings:
            for g_id_str, sess in self.settings["active_sessions"].items():
                try:
                    self.active_sessions[int(g_id_str)] = sess
                except Exception:
                    pass
        # guild_id -> asyncio.Queue
        self.queues: Dict[int, asyncio.Queue] = {}
        # guild_id -> asyncio.Task (queue worker)
        self.workers: Dict[int, asyncio.Task] = {}
        # guild_id -> asyncio.Task (auto leave timer)
        self.empty_timers: Dict[int, asyncio.Task] = {}
        # 현재 오디오를 재생 중인 길드 집합
        self.playing_guilds: set[int] = set()
        # 캐시 폴더 정리
        self.cleanup_old_cache()

    def cog_unload(self):
        for task in self.workers.values():
            task.cancel()
        for task in self.empty_timers.values():
            task.cancel()

    def is_tts_active(self, guild_id: int) -> bool:
        """해당 길드에서 TTS 세션이 활성화되어 있는지 여부"""
        return guild_id in self.active_sessions

    def is_tts_playing(self, guild_id: int) -> bool:
        """해당 길드에서 TTS 오디오가 현재 음성 출력 중인지 여부"""
        return guild_id in self.playing_guilds

    def bind_session(self, guild_id: int, channel_id: int, voice_channel_id: int):
        """음악 재생 또는 TTS 시작 시 해당 텍스트 채널을 TTS 활성 세션으로 자동 바인딩"""
        self.active_sessions[guild_id] = {
            "channel_id": channel_id,
            "voice_channel_id": voice_channel_id
        }
        if "active_sessions" not in self.settings:
            self.settings["active_sessions"] = {}
        self.settings["active_sessions"][str(guild_id)] = {
            "channel_id": channel_id,
            "voice_channel_id": voice_channel_id
        }
        self.save_settings()
        guild = self.bot.get_guild(guild_id)
        if guild:
            self.ensure_worker(guild)
        print(f"[TTS Session] Bound guild {guild_id} to text channel {channel_id} (VC: {voice_channel_id})")

    def cleanup_session(self, guild_id: int):
        """길드의 TTS 세션 및 대기열을 안전하게 정리"""
        if guild_id in self.active_sessions:
            del self.active_sessions[guild_id]
        if "active_sessions" in self.settings and str(guild_id) in self.settings["active_sessions"]:
            del self.settings["active_sessions"][str(guild_id)]
            self.save_settings()
        if guild_id in self.workers:
            self.workers[guild_id].cancel()
            del self.workers[guild_id]
        if guild_id in self.queues:
            while not self.queues[guild_id].empty():
                try:
                    item = self.queues[guild_id].get_nowait()
                    if item and os.path.exists(item[3]):
                        os.remove(item[3])
                except Exception:
                    pass
            del self.queues[guild_id]
        if guild_id in self.empty_timers:
            self.empty_timers[guild_id].cancel()
            del self.empty_timers[guild_id]
        self.playing_guilds.discard(guild_id)

    # ----------------------------------------------------
    # 설정 파일 관리
    # ----------------------------------------------------
    def load_settings(self) -> Dict[str, Any]:
        if TTS_SETTINGS_PATH.exists():
            try:
                with open(TTS_SETTINGS_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[TTS] Error loading settings: {e}")
        return {"guilds": {}, "users": {}}

    def save_settings(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(TTS_SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.settings, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[TTS] Error saving settings: {e}")

    def cleanup_old_cache(self):
        try:
            for p in TTS_CACHE_DIR.glob("*.mp3"):
                if time.time() - p.stat().st_mtime > 3600:
                    try:
                        p.unlink()
                    except Exception:
                        pass
        except Exception:
            pass

    # ----------------------------------------------------
    # 유저 및 길드별 목소리 / 속도 조회
    # ----------------------------------------------------
    def get_user_voice(self, user_id: int) -> str:
        u_settings = self.settings.get("users", {}).get(str(user_id), {})
        return u_settings.get("voice", DEFAULT_VOICE)

    def get_user_speed(self, user_id: int) -> str:
        u_settings = self.settings.get("users", {}).get(str(user_id), {})
        return u_settings.get("speed", DEFAULT_SPEED)

    def get_guild_read_name(self, guild_id: int) -> bool:
        g_settings = self.settings.get("guilds", {}).get(str(guild_id), {})
        return g_settings.get("read_name", False)

    def get_guild_ducking_mode(self, guild_id: int) -> str:
        g_settings = self.settings.get("guilds", {}).get(str(guild_id), {})
        return g_settings.get("ducking", "duck")

    def get_guild_duck_volume(self, guild_id: int) -> int:
        g_settings = self.settings.get("guilds", {}).get(str(guild_id), {})
        return g_settings.get("duck_volume", 20)

    # ----------------------------------------------------
    # 텍스트 정제 및 음성 생성
    # ----------------------------------------------------
    def sanitize_message(self, message: discord.Message) -> str:
        text = message.clean_content.strip()

        # 명령어 제외
        if text.startswith(("/", "!", "?", ".", "$", "~", ";")):
            return ""

        # URL 치환
        text = re.sub(r'https?://\S+', '링크', text)

        # Discord 커스텀 이모지 치환 (<:peko:12345> -> peko)
        text = re.sub(r'<a?:([a-zA-Z0-9_]+):\d+>', r'\1', text)

        # 연속된 공백 및 줄바꿈 정리
        text = re.sub(r'\s+', ' ', text).strip()

        # 첨부파일만 있는 경우
        if not text and message.attachments:
            text = "사진 또는 파일 첨부"

        # 글자 수 제한 (최대 100자)
        if len(text) > 100:
            text = text[:100] + " 이하 생략"

        return text

    async def generate_speech_file(self, text: str, voice: str, speed: str) -> Optional[str]:
        timestamp = int(time.time() * 1000)
        temp_path = str(TTS_CACHE_DIR / f"tts_{timestamp}_{os.getpid()}.mp3")
        try:
            communicate = edge_tts.Communicate(text, voice, rate=speed)
            await communicate.save(temp_path)
            return temp_path
        except Exception as e:
            print(f"[TTS] Generation error: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass
            return None

    # ----------------------------------------------------
    # 큐 워커 (길드별 순차 재생)
    # ----------------------------------------------------
    async def player_worker(self, guild: discord.Guild):
        guild_id = guild.id
        q = self.queues[guild_id]

        while True:
            try:
                item = await q.get()
                if item is None:
                    break

                text, voice, speed, file_path = item

                vc = guild.voice_client
                if not vc or not vc.is_connected():
                    if os.path.exists(file_path):
                        try: os.remove(file_path)
                        except Exception: pass
                    q.task_done()
                    continue

                # 음악 재생 중인지 확인 및 덕킹 처리
                music_cog = self.bot.get_cog("Music")
                ducking_mode = self.get_guild_ducking_mode(guild_id)

                if music_cog and hasattr(music_cog, "is_music_playing") and music_cog.is_music_playing(guild_id):
                    if ducking_mode == "off":
                        print(f"[TTS] Music playing & ducking is OFF. Skipping: '{text}'")
                        if os.path.exists(file_path):
                            try: os.remove(file_path)
                            except Exception: pass
                        q.task_done()
                        continue
                    else:
                        # 덕킹(duck) 또는 일시정지(pause) 모드: 음악 플레이어에게 오버레이 재생 위임
                        player = music_cog.get_player(guild)
                        self.playing_guilds.add(guild_id)
                        try:
                            duck_ratio = self.get_guild_duck_volume(guild_id) / 100.0
                            print(f"[TTS] Delegating to music player overlay (mode={ducking_mode}, duck_ratio={duck_ratio}) for: '{text}'")
                            ok = await player.play_tts_overlay(file_path, mode=ducking_mode, duck_ratio=duck_ratio)
                            print(f"[TTS] Music overlay result: {ok}")
                        except Exception as overlay_err:
                            print(f"[TTS Overlay Exception] {overlay_err}")
                        finally:
                            self.playing_guilds.discard(guild_id)
                            if os.path.exists(file_path):
                                try: os.remove(file_path)
                                except Exception: pass
                            q.task_done()
                        continue

                # 이전 TTS 재생 완료 대기
                while vc.is_playing() or vc.is_paused():
                    await asyncio.sleep(0.08)
                    if not vc.is_connected():
                        break

                if not vc.is_connected():
                    if os.path.exists(file_path):
                        try: os.remove(file_path)
                        except Exception: pass
                    q.task_done()
                    continue

                done_event = asyncio.Event()

                def after_play(err):
                    self.playing_guilds.discard(guild_id)
                    if err:
                        print(f"[TTS Playback Error] {err}")
                    self.bot.loop.call_soon_threadsafe(done_event.set)

                try:
                    source = discord.FFmpegPCMAudio(file_path, executable=FFMPEG_EXECUTABLE)
                    self.playing_guilds.add(guild_id)
                    vc.play(source, after=after_play)
                    await done_event.wait()
                except Exception as play_err:
                    self.playing_guilds.discard(guild_id)
                    print(f"[TTS Play Exception] {play_err}")
                finally:
                    self.playing_guilds.discard(guild_id)
                    if os.path.exists(file_path):
                        try: os.remove(file_path)
                        except Exception: pass
                    q.task_done()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[TTS Worker Error]: {e}")
                await asyncio.sleep(0.5)

    def ensure_worker(self, guild: discord.Guild):
        if guild.id not in self.queues:
            self.queues[guild.id] = asyncio.Queue()
        if guild.id not in self.workers or self.workers[guild.id].done():
            self.workers[guild.id] = self.bot.loop.create_task(self.player_worker(guild))

    # ----------------------------------------------------
    # 이벤트 리스너
    # ----------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        guild_id = message.guild.id
        session = self.active_sessions.get(guild_id)

        # 봇이 음성 채널에 연결되어 있지 않으면 무시
        vc = message.guild.voice_client
        if not vc or not vc.is_connected():
            return

        # 1. 활성 세션이 이미 지정된 경우
        if session:
            is_bound_channel = (message.channel.id == session.get("channel_id"))
            is_vc_text = bool(vc.channel and message.channel.id == vc.channel.id)
            is_user_in_vc = bool(message.author.voice and message.author.voice.channel == vc.channel)

            if not is_bound_channel and not is_vc_text:
                if is_user_in_vc:
                    # 봇과 같은 음성 채널에 있는 유저의 메시지이므로 이 텍스트 채널로 세션 자동 갱신
                    self.bind_session(guild_id, message.channel.id, vc.channel.id)
                    session = self.active_sessions.get(guild_id)
                else:
                    return
        else:
            # 2. 세션 미지정 상태여도 봇과 유저가 같은 음성 채널에 있는 경우 스마트 자동 바인딩
            if message.author.voice and message.author.voice.channel == vc.channel:
                self.bind_session(guild_id, message.channel.id, vc.channel.id)
                session = self.active_sessions.get(guild_id)

        if not session:
            return

        # 음악 재생 중인지 확인: ducking_mode가 "off"일 때만 스킵
        music_cog = self.bot.get_cog("Music")
        ducking_mode = self.get_guild_ducking_mode(guild_id)
        if music_cog and hasattr(music_cog, "is_music_playing") and music_cog.is_music_playing(guild_id):
            if ducking_mode == "off":
                return

        clean_text = self.sanitize_message(message)
        if not clean_text:
            return

        # 닉네임 읽기 여부 확인
        read_name = self.get_guild_read_name(guild_id)
        author_name = message.author.display_name
        text_to_speak = f"{author_name}, {clean_text}" if read_name else clean_text

        # 유저별 맞춤 보이스 및 속도 적용
        user_voice = self.get_user_voice(message.author.id)
        user_speed = self.get_user_speed(message.author.id)

        print(f"[TTS Chat Input] from {message.author.display_name}: '{clean_text}' (mode={ducking_mode})")
        file_path = await self.generate_speech_file(text_to_speak, user_voice, user_speed)
        if file_path:
            self.ensure_worker(message.guild)
            await self.queues[guild_id].put((text_to_speak, user_voice, user_speed, file_path))

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        # 봇 자신이 연결 끊겼을 경우 세션 정리
        if member.id == self.bot.user.id and after.channel is None:
            guild_id = member.guild.id
            if guild_id in self.active_sessions:
                del self.active_sessions[guild_id]
            if guild_id in self.workers:
                self.workers[guild_id].cancel()
                del self.workers[guild_id]
            if guild_id in self.queues:
                del self.queues[guild_id]
            if guild_id in self.empty_timers:
                self.empty_timers[guild_id].cancel()
                del self.empty_timers[guild_id]
            return

        # 음성 채널에 아무도 남지 않았을 때 자동 퇴장
        guild_id = member.guild.id
        session = self.active_sessions.get(guild_id)
        if not session:
            return

        vc = member.guild.voice_client
        if not vc or not vc.channel:
            return

        # 음악 플레이어 상태 확인 (라디오 모드이거나 음악 자동퇴장이 꺼져 있으면 퇴장 안 함)
        music_cog = self.bot.get_cog("Music")
        player = music_cog.get_player(member.guild) if music_cog else None
        if player and (player.radio_mode or not player.auto_leave):
            if guild_id in self.empty_timers and not self.empty_timers[guild_id].done():
                self.empty_timers[guild_id].cancel()
                del self.empty_timers[guild_id]
            return

        # 인간 유저 수 체크
        non_bot_members = [m for m in vc.channel.members if not m.bot]
        if len(non_bot_members) == 0:
            if guild_id not in self.empty_timers or self.empty_timers[guild_id].done():
                self.empty_timers[guild_id] = self.bot.loop.create_task(self._auto_leave_timer(member.guild, session))
        else:
            # 유저가 다시 들어오면 퇴장 타이머 취소
            if guild_id in self.empty_timers and not self.empty_timers[guild_id].done():
                self.empty_timers[guild_id].cancel()
                del self.empty_timers[guild_id]

    async def _auto_leave_timer(self, guild: discord.Guild, session: Dict[str, Any]):
        try:
            await asyncio.sleep(60) # 60초 대기
            vc = guild.voice_client
            if vc and vc.channel:
                music_cog = self.bot.get_cog("Music")
                player = music_cog.get_player(guild) if music_cog else None
                if player and (player.radio_mode or not player.auto_leave):
                    return

                non_bot = [m for m in vc.channel.members if not m.bot]
                if len(non_bot) == 0:
                    text_channel = guild.get_channel(session.get("channel_id"))
                    self.cleanup_session(guild.id)
                    if vc.is_connected():
                        await vc.disconnect()
                    if text_channel and isinstance(text_channel, discord.TextChannel):
                        try:
                            embed = discord.Embed(
                                title="🚪 TTS 자동 퇴장 안내",
                                description="음성 채널에 아무도 남아있지 않아 TTS를 종료하고 음성 채널에서 퇴장했습니다.",
                                color=0x95A5A6
                            )
                            embed.set_footer(text="Discord Music & TTS Bot 📢")
                            await text_channel.send(embed=embed)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[TTS Auto Leave Error] {e}")

    # ----------------------------------------------------
    # 슬래시 커맨드 그룹: /tts
    # ----------------------------------------------------
    tts_group = app_commands.Group(name="tts", description="음성 채널 텍스트 음성 변환 (TTS) 관련 명령어")

    @tts_group.command(name="join", description="음성 채널에 봇을 부르고 현재 텍스트 채널을 TTS로 연결합니다.")
    async def tts_join(self, interaction: discord.Interaction):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 음성 채널에 먼저 입장한 후 명령어를 실행해주세요.", ephemeral=True)
            return

        target_voice_channel = interaction.user.voice.channel
        guild = interaction.guild
        vc = guild.voice_client

        await interaction.response.defer()

        # 봇 음성 채널 연결 또는 이동
        if vc is None:
            try:
                vc = await target_voice_channel.connect()
            except Exception as e:
                await interaction.followup.send(f"❌ 음성 채널에 연결할 수 없습니다: {e}", ephemeral=True)
                return
        elif vc.channel != target_voice_channel:
            try:
                await vc.move_to(target_voice_channel)
            except Exception as e:
                await interaction.followup.send(f"❌ 음성 채널 이동에 실패했습니다: {e}", ephemeral=True)
                return

        # 활성 세션 등록 (현재 텍스트 채널에 바인딩 및 영구 저장)
        self.bind_session(guild.id, interaction.channel_id, target_voice_channel.id)

        # 음악 플레이어가 이미 존재하는 경우 voice_client 동기화 보장
        music_cog = self.bot.get_cog("Music")
        if music_cog:
            player = music_cog.get_player(guild)
            player.voice_client = vc
        is_music_active = music_cog.is_music_playing(guild.id) if music_cog and hasattr(music_cog, "is_music_playing") else False

        user_voice = self.get_user_voice(interaction.user.id)
        voice_info = VOICE_CHOICES.get(user_voice, VOICE_CHOICES[DEFAULT_VOICE])
        read_name = self.get_guild_read_name(guild.id)
        read_name_text = "켜짐 (이름 + 내용)" if read_name else "꺼짐 (내용만 읽기)"

        embed = discord.Embed(
            title="🎙️ TTS 음성 읽기 시작",
            description=(
                f"**{target_voice_channel.mention}** 음성 채널과 **{interaction.channel.mention}** 텍스트 채널이 연결되었습니다!\n\n"
                f"💬 이제 이 채팅방에 메시지를 입력하면 봇이 음성으로 읽어줍니다."
            ),
            color=0x27C7FE
        )
        embed.add_field(name="기본 목소리", value=f"{voice_info['emoji']} **{voice_info['name']}**", inline=True)
        embed.add_field(name="이름 읽기", value=f"`{read_name_text}`", inline=True)
        if is_music_active:
            ducking_mode = self.get_guild_ducking_mode(guild.id)
            if ducking_mode == "duck":
                mode_desc = "현재 음악이 재생 중이며, **TTS 덕킹(Ducking)** 모드가 켜져 있어 음악 볼륨이 자동으로 줄어들며 채팅이 함께 출력됩니다."
            elif ducking_mode == "pause":
                mode_desc = "현재 음악이 재생 중이며, **일시정지(Pause)** 모드가 켜져 있어 음악이 잠시 멈추고 음성이 출력된 뒤 재개됩니다."
            else:
                mode_desc = "현재 음악이 재생 중입니다. 음악 감상을 위해 음악이 끝나거나 `/stop`된 후에 채팅 읽기가 동작합니다."
            embed.add_field(
                name="🎵 음악 연동 안내",
                value=mode_desc,
                inline=False
            )
        embed.add_field(
            name="💡 도움말",
            value=(
                "• `/tts voice` 로 내 목소리를 변경할 수 있습니다.\n"
                "• `/tts speed` 로 읽기 속도를 조절할 수 있습니다.\n"
                "• `/tts ducking` 으로 음악 연동 모드(덕킹/일시정지/꺼짐)를 변경할 수 있습니다.\n"
                "• `/tts leave` 로 TTS를 종료하고 퇴장시킬 수 있습니다."
            ),
            inline=False
        )
        embed.set_footer(text="Discord Music & TTS Bot 📢")

        await interaction.followup.send(embed=embed)

        # 시작 알림 음성 재생 (음악이 재생 중이지 않을 때만)
        if not is_music_active:
            welcome_file = await self.generate_speech_file("TTS 음성 읽기가 연결되었습니다.", user_voice, DEFAULT_SPEED)
            if welcome_file:
                await self.queues[guild.id].put(("TTS 연결 안내", user_voice, DEFAULT_SPEED, welcome_file))

    @tts_group.command(name="leave", description="TTS 음성 읽기를 종료하고 음성 채널에서 퇴장합니다.")
    async def tts_leave(self, interaction: discord.Interaction):
        guild = interaction.guild
        vc = guild.voice_client

        self.cleanup_session(guild.id)

        music_cog = self.bot.get_cog("Music")
        is_music_active = music_cog.is_music_playing(guild.id) if music_cog and hasattr(music_cog, "is_music_playing") else False

        if is_music_active:
            await interaction.response.send_message("👋 TTS 음성 읽기를 종료했습니다. (음악 재생은 계속 유지됩니다)")
            return

        if vc and vc.is_connected():
            await interaction.response.defer()
            await vc.disconnect()
            await interaction.followup.send("👋 TTS 음성 읽기를 종료하고 음성 채널에서 퇴장했습니다.")
        else:
            await interaction.response.send_message("ℹ️ 현재 연결된 음성 채널이 없습니다.", ephemeral=True)

    @tts_group.command(name="say", description="입력한 메시지를 즉시 음성 채널에서 읽어줍니다.")
    @app_commands.describe(message="봇이 음성으로 읽어줄 메시지")
    async def tts_say(self, interaction: discord.Interaction, message: str):
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("❌ 음성 채널에 먼저 입장해주세요.", ephemeral=True)
            return

        guild = interaction.guild

        # 음악 재생 중인지 확인
        music_cog = self.bot.get_cog("Music")
        ducking_mode = self.get_guild_ducking_mode(guild.id)
        if music_cog and hasattr(music_cog, "is_music_playing") and music_cog.is_music_playing(guild.id):
            if ducking_mode == "off":
                await interaction.response.send_message("⚠️ 현재 음악이 재생 중이며 TTS 덕킹 모드가 꺼져 있습니다. `/tts ducking`으로 켜거나 음악 종료 후 사용해주세요.", ephemeral=True)
                return

        vc = guild.voice_client

        # 봇이 음성 채널에 없으면 자동 연결
        if not vc:
            try:
                vc = await interaction.user.voice.channel.connect()
            except Exception as e:
                await interaction.response.send_message(f"❌ 음성 채널 연결 실패: {e}", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True)

        user_voice = self.get_user_voice(interaction.user.id)
        user_speed = self.get_user_speed(interaction.user.id)

        clean_text = message.strip()
        if len(clean_text) > 100:
            clean_text = clean_text[:100] + " 이하 생략"

        file_path = await self.generate_speech_file(clean_text, user_voice, user_speed)
        if file_path:
            self.ensure_worker(guild)
            await self.queues[guild.id].put((clean_text, user_voice, user_speed, file_path))
            await interaction.followup.send(f"📢 **[{interaction.user.display_name}]**: {clean_text}", ephemeral=True)
        else:
            await interaction.followup.send("❌ 음성 변환 중 오류가 발생했습니다.", ephemeral=True)

    @tts_group.command(name="voice", description="내 채팅을 읽어줄 TTS 목소리를 변경합니다.")
    @app_commands.describe(voice="선택할 목소리")
    @app_commands.choices(voice=[
        app_commands.Choice(name="🌸 선희 (여성 한국어, 맑고 또렷함 - 기본값)", value="ko-KR-SunHiNeural"),
        app_commands.Choice(name="🎙️ 인준 (남성 한국어, 차분하고 단정함)", value="ko-KR-InJoonNeural"),
        app_commands.Choice(name="🌐 현수 (남성 다국어, 부드러움)", value="ko-KR-HyunsuMultilingualNeural"),
        app_commands.Choice(name="🎀 나나미 (여성 일본어, 귀여운 보이스)", value="ja-JP-NanamiNeural"),
        app_commands.Choice(name="🎮 케이타 (남성 일본어, 부드러움)", value="ja-JP-KeitaNeural"),
    ])
    async def tts_voice(self, interaction: discord.Interaction, voice: str):
        user_id_str = str(interaction.user.id)
        if "users" not in self.settings:
            self.settings["users"] = {}
        if user_id_str not in self.settings["users"]:
            self.settings["users"][user_id_str] = {}

        self.settings["users"][user_id_str]["voice"] = voice
        self.save_settings()

        info = VOICE_CHOICES.get(voice, VOICE_CHOICES[DEFAULT_VOICE])
        embed = discord.Embed(
            title="🎙️ TTS 목소리 설정 완료",
            description=f"**{interaction.user.mention}**님의 TTS 목소리가 **{info['emoji']} {info['name']}**(으)로 설정되었습니다.\n*{info['desc']}*",
            color=0x27C7FE
        )
        embed.set_footer(text="Discord Music & TTS Bot 📢")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tts_group.command(name="speed", description="TTS 음성 읽기 속도를 변경합니다.")
    @app_commands.describe(rate="속도 선택")
    @app_commands.choices(rate=[
        app_commands.Choice(name="느리게 (-20%)", value="-20%"),
        app_commands.Choice(name="조금 느리게 (-10%)", value="-10%"),
        app_commands.Choice(name="보통 (기본값, +0%)", value="+0%"),
        app_commands.Choice(name="조금 빠름 (+10%)", value="+10%"),
        app_commands.Choice(name="빠름 (+20%)", value="+20%"),
        app_commands.Choice(name="매우 빠름 (+30%)", value="+30%"),
    ])
    async def tts_speed(self, interaction: discord.Interaction, rate: str):
        user_id_str = str(interaction.user.id)
        if "users" not in self.settings:
            self.settings["users"] = {}
        if user_id_str not in self.settings["users"]:
            self.settings["users"][user_id_str] = {}

        self.settings["users"][user_id_str]["speed"] = rate
        self.save_settings()

        await interaction.response.send_message(f"⚡ TTS 읽기 속도가 **`{rate}`**(으)로 설정되었습니다.", ephemeral=True)

    @tts_group.command(name="name", description="메시지를 읽을 때 사용자 이름(닉네임) 포함 여부를 토글합니다.")
    @app_commands.describe(state="이름 읽기 설정")
    @app_commands.choices(state=[
        app_commands.Choice(name="이름 안 읽기 (내용만 읽기 - 기본값)", value="off"),
        app_commands.Choice(name="이름 읽기 (닉네임 + 내용)", value="on"),
    ])
    async def tts_name(self, interaction: discord.Interaction, state: str):
        guild_id_str = str(interaction.guild.id)
        if "guilds" not in self.settings:
            self.settings["guilds"] = {}
        if guild_id_str not in self.settings["guilds"]:
            self.settings["guilds"][guild_id_str] = {}

        is_on = (state == "on")
        self.settings["guilds"][guild_id_str]["read_name"] = is_on
        self.save_settings()

        status_text = "이름과 함께 읽기 (예: 홍길동, 안녕하세요)" if is_on else "내용만 읽기 (예: 안녕하세요)"
        await interaction.response.send_message(f"🏷️ TTS 이름 읽기 모드가 **`{status_text}`**(으)로 설정되었습니다.")

    @tts_group.command(name="status", description="현재 TTS 연결 상태 및 설정을 확인합니다.")
    async def tts_status(self, interaction: discord.Interaction):
        guild = interaction.guild
        session = self.active_sessions.get(guild.id)
        vc = guild.voice_client

        if not session or not vc or not vc.is_connected():
            await interaction.response.send_message(
                "ℹ️ 현재 활성화된 TTS 세션이 없습니다.\n`/tts join`을 입력하여 음성 채널과 연결해보세요!",
                ephemeral=True
            )
            return

        text_chan = guild.get_channel(session.get("channel_id"))
        voice_chan = vc.channel
        user_voice = self.get_user_voice(interaction.user.id)
        user_speed = self.get_user_speed(interaction.user.id)
        voice_info = VOICE_CHOICES.get(user_voice, VOICE_CHOICES[DEFAULT_VOICE])
        read_name = self.get_guild_read_name(guild.id)

        q_size = self.queues[guild.id].qsize() if guild.id in self.queues else 0

        embed = discord.Embed(
            title="📋 TTS 현재 상태",
            color=0x27C7FE
        )
        embed.add_field(name="🔊 음성 채널", value=voice_chan.mention if voice_chan else "알 수 없음", inline=True)
        embed.add_field(name="💬 텍스트 채널", value=text_chan.mention if text_chan else "알 수 없음", inline=True)
        embed.add_field(name="대기 중인 텍스트", value=f"`{q_size}개`", inline=True)
        embed.add_field(name="내 목소리", value=f"{voice_info['emoji']} {voice_info['name']}", inline=True)
        embed.add_field(name="내 재생 속도", value=f"`{user_speed}`", inline=True)
        embed.add_field(name="이름 읽기 설정", value=f"`{'켜짐' if read_name else '꺼짐'}`", inline=True)

        ducking_mode = self.get_guild_ducking_mode(guild.id)
        duck_vol = self.get_guild_duck_volume(guild.id)
        ducking_names = {
            "duck": f"🦆 덕킹 (음악 볼륨 {duck_vol}%로 감쇠)",
            "pause": "⏸️ 일시정지 (음악 멈춤 후 재개)",
            "off": "❌ 꺼짐 (음악 재생 중 생략)"
        }
        embed.add_field(name="음악 연동 모드", value=f"`{ducking_names.get(ducking_mode, ducking_mode)}`", inline=True)
        embed.set_footer(text="Discord Music & TTS Bot 📢")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tts_group.command(name="ducking", description="음악 재생 중 TTS 음성 출력 방식을 설정합니다.")
    @app_commands.describe(
        mode="덕킹 모드 선택",
        duck_volume="덕킹 모드 시 음악 볼륨 비율 (5% ~ 50%, 기본값 20%)"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="🦆 덕킹 (음악 볼륨 감쇠 후 TTS 오버레이 동시 재생 - 기본 권장)", value="duck"),
        app_commands.Choice(name="⏸️ 일시정지 (음악 일시정지 후 TTS 완료 시 재개)", value="pause"),
        app_commands.Choice(name="❌ 꺼짐 (음악 재생 중에는 채팅 TTS 생략)", value="off"),
    ])
    async def tts_ducking(self, interaction: discord.Interaction, mode: str, duck_volume: Optional[app_commands.Range[int, 5, 50]] = None):
        guild_id_str = str(interaction.guild.id)
        if "guilds" not in self.settings:
            self.settings["guilds"] = {}
        if guild_id_str not in self.settings["guilds"]:
            self.settings["guilds"][guild_id_str] = {}

        self.settings["guilds"][guild_id_str]["ducking"] = mode
        if duck_volume is not None:
            self.settings["guilds"][guild_id_str]["duck_volume"] = duck_volume
        self.save_settings()

        vol_pct = self.settings["guilds"][guild_id_str].get("duck_volume", 20)
        mode_names = {
            "duck": f"🦆 덕킹 (음악 볼륨 {vol_pct}%로 감쇠 후 TTS 오버레이 동시 재생)",
            "pause": "⏸️ 일시정지 (음악을 잠시 멈추고 TTS 완료 후 재개)",
            "off": "❌ 꺼짐 (음악 재생 중에는 채팅 TTS 생략)"
        }
        await interaction.response.send_message(f"🎧 TTS 음악 연동 모드가 **`{mode_names.get(mode, mode)}`**(으)로 설정되었습니다.")

    @tts_group.command(name="stop", description="현재 재생 중인 TTS를 멈추고 대기열을 초기화합니다.")
    async def tts_stop(self, interaction: discord.Interaction):
        guild = interaction.guild
        vc = guild.voice_client

        # 대기열 비우기
        if guild.id in self.queues:
            while not self.queues[guild.id].empty():
                try:
                    item = self.queues[guild.id].get_nowait()
                    if item and os.path.exists(item[3]):
                        os.remove(item[3])
                except Exception:
                    pass

        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏹️ TTS 재생을 중지하고 대기열을 모두 비웠습니다.")
        else:
            await interaction.response.send_message("ℹ️ 대기열을 비웠습니다.")


async def setup(bot: commands.Bot):
    await bot.add_cog(TTSCog(bot))
