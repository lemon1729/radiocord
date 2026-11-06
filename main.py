import asyncio
import json
import os
import sys
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

# .env 파일 로드
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# config.json 로드
CONFIG_PATH = BASE_DIR / "config.json"
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
except Exception as e:
    print(f"[Warning] Failed to load config.json: {e}. Using defaults.")
    config = {
        "bot_name": "MusicTTSBot",
        "prefix": "!",
        "theme": {"color_primary": 0x5865F2}
    }

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
TEST_GUILD_ID = os.getenv("DISCORD_GUILD_ID")
ENABLE_MESSAGE_CONTENT = os.getenv("ENABLE_MESSAGE_CONTENT", "true").lower() in ("true", "1", "yes")

class MusicTTSBot(commands.Bot):
    def __init__(self, enable_message_content: bool = True):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.voice_states = True
        intents.message_content = True

        super().__init__(
            command_prefix=config.get("prefix", "!"),
            intents=intents,
            help_command=None
        )

    async def setup_hook(self):
        print("--------------------------------------------------")
        # Opus 오디오 라이브러리 로드 확인 (음성 재생 필수)
        if not discord.opus.is_loaded():
            try:
                discord.opus._load_default()
                print("  🎵 [Opus Audio] discord.opus 기본 라이브러리 로드 성공!")
            except Exception:
                candidates = ["libopus.so.0", "libopus.so", "opus.dll", "libopus-0.dll"]
                loaded = False
                for c in candidates:
                    try:
                        discord.opus.load_opus(c)
                        print(f"  🎵 [Opus Audio] {c} 직접 로드 성공!")
                        loaded = True
                        break
                    except Exception:
                        pass
                if not loaded:
                    print("  ⚠️ [Opus Audio] Opus 코덱이 로드되지 않았습니다. 소리가 나지 않을 수 있습니다.")
                    print("     해결책: 'pip install PyNaCl' 또는 Linux: 'sudo apt install libopus0' 실행")

        # DAVE (E2EE Voice) 음성 종단간 암호화 패치 적용
        try:
            from dave_patch import apply_dave_patch
            apply_dave_patch()
        except Exception as e:
            print(f"  ⚠️ [DAVE Patch] DAVE E2EE 패치 적용 중 오류: {e}")

        print("🌟 음악 재생 및 TTS 디스코드 봇 Cog 로딩 시작...")
        initial_extensions = [
            "cogs.music",
            "cogs.tts",
        ]

        for ext in initial_extensions:
            try:
                await self.load_extension(ext)
                print(f"  ✅ [Cog 로드 완료] {ext}")
            except Exception as e:
                print(f"  ❌ [Cog 로드 실패] {ext}: {e}")

        # 슬래시 커맨드 전역 에러 핸들러
        @self.tree.error
        async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
            cmd_name = interaction.command.name if interaction.command else "알 수 없음"
            print(f"[Command Error] /{cmd_name} | User: {interaction.user} (ID: {interaction.user.id}) | Error: {error}")
            import traceback
            traceback.print_exception(type(error), error, error.__traceback__)

            err_msg = f"⚠️ `/{cmd_name}` 실행 중 오류가 발생했습니다: {error}"
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(err_msg, ephemeral=True)
                else:
                    await interaction.response.send_message(err_msg, ephemeral=True)
            except Exception:
                pass

        # 슬래시 커맨드 동기화
        print("📡 슬래시 커맨드 동기화 중...")
        if TEST_GUILD_ID and TEST_GUILD_ID.strip():
            try:
                guild_obj = discord.Object(id=int(TEST_GUILD_ID.strip()))
                self.tree.copy_global_to(guild=guild_obj)
                synced = await self.tree.sync(guild=guild_obj)
                print(f"  ⚡ 테스트 서버({TEST_GUILD_ID})에 {len(synced)}개의 슬래시 커맨드가 즉시 동기화되었습니다!")
            except Exception as e:
                print(f"  ⚠️ 테스트 서버 동기화 실패: {e}. 전역 동기화를 시도합니다.")
                synced = await self.tree.sync()
                print(f"  🌐 전역 {len(synced)}개의 슬래시 커맨드 동기화 완료!")
        else:
            synced = await self.tree.sync()
            print(f"  🌐 전역 {len(synced)}개의 슬래시 커맨드가 성공적으로 동기화되었습니다!")
        print("--------------------------------------------------")

    async def on_ready(self):
        print(f"✨ 로그인 완료: {self.user} (ID: {self.user.id})")
        print(f"🏰 접속한 서버 수: {len(self.guilds)}개")
        print("🎵 음악 및 TTS 디스코드 봇이 정상적으로 가동 중입니다!")

        # 봇 상태 메시지 설정
        activity = discord.Activity(
            type=discord.ActivityType.listening,
            name="🎶 /play & 📢 /tts"
        )
        await self.change_presence(status=discord.Status.online, activity=activity)


async def main():
    if not TOKEN or TOKEN.strip() == "YOUR_DISCORD_BOT_TOKEN_HERE" or not TOKEN.strip():
        print("=" * 60)
        print("❌ [오류] DISCORD_BOT_TOKEN이 설정되지 않았습니다!")
        print("프로젝트 폴더 내의 `.env` 파일을 열고, 발급받은 디스코드 봇 토큰을 입력해주세요:")
        print(f"파일 경로: {BASE_DIR / '.env'}")
        print("=" * 60)
        return

    bot = MusicTTSBot(enable_message_content=ENABLE_MESSAGE_CONTENT)
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 봇이 안전하게 종료되었습니다.")
