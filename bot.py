import os
import threading
import asyncio
import re
import requests
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask

# --- 防休眠網頁伺服器 (Render Web Service 必備) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

# --- 基本設定 ---
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
INACTIVITY_TIMEOUT = 300  # 5 分鐘

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 佇列與閒置任務管理
queues = {}
timeout_tasks = {}

# --- 多重第三方 API 解析工具 ---
PIPED_INSTANCES = [
    "https://pipedapi.kavin.rocks",
    "https://api.piped.private.coffee",
    "https://pipedapi.mha.fi",
    "https://pipedapi.drgns.space",
    "https://piped-api.garudalinux.org"
]

INVIDIOUS_INSTANCES = [
    "https://inv.riverside.rocks",
    "https://invidious.nerdvpn.de",
    "https://invidious.drgns.space"
]

def extract_video_id(url_or_query):
    """從 URL 或搜尋文字中提取 YouTube Video ID"""
    match = re.search(r"(?:v=|\/|vi=)([0-9A-Za-z_-]{11})", url_or_query)
    if match:
        return match.group(1)
    return None

def fetch_from_piped(endpoint):
    """嘗試多個 Piped 實例"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for base_url in PIPED_INSTANCES:
        try:
            res = requests.get(f"{base_url}{endpoint}", headers=headers, timeout=4)
            if res.status_code == 200:
                return res.json()
        except Exception:
            continue
    return None

def fetch_from_invidious(video_id):
    """備用：從 Invidious API 取得音訊串流"""
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    for base_url in INVIDIOUS_INSTANCES:
        try:
            res = requests.get(f"{base_url}/api/v1/videos/{video_id}", headers=headers, timeout=4)
            if res.status_code == 200:
                data = res.json()
                title = data.get("title", "未知歌曲")
                adaptive_formats = data.get("adaptiveFormats", [])
                audio_streams = [f for f in adaptive_formats if f.get("type", "").startswith("audio/")]
                if audio_streams:
                    return title, audio_streams[0]["url"]
        except Exception:
            continue
    return None, None

def get_audio_stream_and_info(search_query):
    """雙機制取得音訊網址與標題 (Piped -> Invidious)"""
    video_id = extract_video_id(search_query)
    
    # 若不是網址則進行 Piped 搜尋
    if not video_id:
        data = fetch_from_piped(f"/search?q={requests.utils.quote(search_query)}&filter=videos")
        if data and data.get("items"):
            first_item = data["items"][0]
            url = first_item.get("url", "")
            video_id = extract_video_id(url)
            
    if not video_id:
        raise Exception("找不到相關歌曲資訊或影片 ID！")

    # 1. 優先嘗試 Piped API
    stream_data = fetch_from_piped(f"/streams/{video_id}")
    if stream_data and stream_data.get("audioStreams"):
        title = stream_data.get("title", "未知歌曲")
        audio_url = stream_data["audioStreams"][0]["url"]
        return title, audio_url

    # 2. 備用方案：嘗試 Invidious API
    title, audio_url = fetch_from_invidious(video_id)
    if audio_url:
        return title, audio_url

    raise Exception("所有第三方 API 節點皆暫時無法取得該影片串流，請稍後再試或更換歌曲！")
# --- FFmpeg 設定 ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

# --- 閒置與退場邏輯 ---
def cancel_timeout_task(guild_id):
    """如果有新歌開始播放或有人回到頻道，取消離線倒數"""
    if guild_id in timeout_tasks and not timeout_tasks[guild_id].done():
        timeout_tasks[guild_id].cancel()
        del timeout_tasks[guild_id]

async def inactivity_disconnect(guild_id, text_channel):
    """閒置倒數計時器：計時結束後自動離開語音頻道"""
    try:
        await asyncio.sleep(INACTIVITY_TIMEOUT)
        guild = bot.get_guild(guild_id)
        if guild and guild.voice_client:
            voice_client = guild.voice_client
            
            human_members = [m for m in voice_client.channel.members if not m.bot]
            is_idle = not voice_client.is_playing() and not voice_client.is_paused()
            
            if is_idle or len(human_members) == 0:
                await voice_client.disconnect()
                if guild_id in queues:
                    queues[guild_id].clear()
                
                if text_channel:
                    reason = "頻道內已無人收聽" if len(human_members) == 0 else f"已經超過 {INACTIVITY_TIMEOUT // 60} 分鐘未播放音樂"
                    await text_channel.send(f"💤 {reason}，機器人已自動退出語音頻道！")
    except asyncio.CancelledError:
        pass

# --- 音樂控制面板 View ---
class MusicPlayerView(discord.ui.View):
    def __init__(self, voice_client, guild_id, text_channel):
        super().__init__(timeout=None)
        self.voice_client = voice_client
        self.guild_id = guild_id
        self.text_channel = text_channel

    @discord.ui.button(label="⏸️ 暫停", style=discord.ButtonStyle.secondary)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.pause()
            await interaction.response.send_message("⏸️ 音樂已暫停", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 目前沒有正在播放的音樂！", ephemeral=True)

    @discord.ui.button(label="▶️ 繼續播放", style=discord.ButtonStyle.primary)
    async def resume_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client and self.voice_client.is_paused():
            self.voice_client.resume()
            await interaction.response.send_message("▶️ 已恢復播放", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 音樂未被暫停！", ephemeral=True)

    @discord.ui.button(label="⏭️ 跳過 (下一首)", style=discord.ButtonStyle.success)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
            await interaction.response.send_message("⏭️ 已跳過當前歌曲！", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 目前沒有音樂可跳過！", ephemeral=True)

    @discord.ui.button(label="⏹️ 停止並清空", style=discord.ButtonStyle.danger)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.guild_id in queues:
            queues[self.guild_id].clear()

        if self.voice_client and (self.voice_client.is_playing() or self.voice_client.is_paused()):
            self.voice_client.stop()
            await interaction.response.send_message("⏹️ 已停止播放並清空佇列！", ephemeral=True)
        else:
            await interaction.response.send_message("⚠️ 目前沒有音樂在播放！", ephemeral=True)

# --- 核心非同步播放邏輯 ---
def play_next(guild_id, text_channel):
    """FFmpeg 播完後觸發的回呼函式"""
    if guild_id in queues and len(queues[guild_id]) > 0:
        next_song = queues[guild_id].pop(0)
        coro = play_song_async(guild_id, text_channel, next_song)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)
    else:
        cancel_timeout_task(guild_id)
        task = bot.loop.create_task(inactivity_disconnect(guild_id, text_channel))
        timeout_tasks[guild_id] = task

async def play_song_async(guild_id, text_channel, song_info):
    """實際由 API 取得音訊直鏈並由 FFmpeg 播放的核心邏輯"""
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return

    voice_client = guild.voice_client

    try:
        cancel_timeout_task(guild_id)

        # 非同步方式呼叫 Piped API 抓取 Audio Stream URL
        loop = asyncio.get_event_loop()
        title, stream_url = await loop.run_in_executor(
            None, lambda: get_audio_stream_and_info(song_info['query'])
        )

        player = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        voice_client.play(
            player,
            after=lambda e: play_next(guild_id, text_channel)
        )

        view = MusicPlayerView(voice_client, guild_id, text_channel)
        embed = discord.Embed(
            title="🎶 正在播放", 
            description=f"**{title}**", 
            color=discord.Color.green()
        )
        await text_channel.send(embed=embed, view=view)

    except Exception as e:
        await text_channel.send(f"❌ 播放 `{song_info.get('title', '歌曲')}` 時發生錯誤：`{e}`")
        play_next(guild_id, text_channel)

# --- 事件處理 ---
@bot.event
async def on_ready():
    print(f'🤖 機器人已成功登入為：{bot.user}')
    try:
        synced = await bot.tree.sync()
        print(f'✅ 已成功同步 {len(synced)} 個斜線指令！')
    except Exception as e:
        print(f'❌ 指令同步失敗：{e}')

@bot.event
async def on_voice_state_update(member, before, after):
    """監控語音頻道成員變動：如果成員離開導致頻道只剩機器人，開啟離線倒數"""
    guild = member.guild
    voice_client = guild.voice_client

    if not voice_client or not voice_client.is_connected():
        return

    human_members = [m for m in voice_client.channel.members if not m.bot]

    if len(human_members) == 0:
        cancel_timeout_task(guild.id)
        task = bot.loop.create_task(inactivity_disconnect(guild.id, None))
        timeout_tasks[guild.id] = task
    elif len(human_members) > 0 and (voice_client.is_playing() or voice_client.is_paused()):
        cancel_timeout_task(guild.id)

# --- 斜線指令集 ---
@bot.tree.command(name="join", description="讓機器人加入你目前的語音頻道")
async def join(interaction: discord.Interaction):
    if interaction.user.voice:
        channel = interaction.user.voice.channel
        if interaction.guild.voice_client:
            await interaction.guild.voice_client.move_to(channel)
        else:
            await channel.connect()
        await interaction.response.send_message(f"🔊 已加入語音頻道：**{channel.name}**")
    else:
        await interaction.response.send_message("❌ 請先加入一個語音頻道！", ephemeral=True)

@bot.tree.command(name="play", description="播放單曲或關鍵字搜尋歌曲")
@app_commands.describe(search="輸入單曲網址或關鍵字")
async def play(interaction: discord.Interaction, search: str):
    await interaction.response.defer()

    if not interaction.user.voice:
        await interaction.followup.send("❌ 請先加入一個語音頻道！", ephemeral=True)
        return

    guild_id = interaction.guild_id
    channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    if not voice_client:
        voice_client = await channel.connect()
    elif voice_client.channel != channel:
        await voice_client.move_to(channel)

    if guild_id not in queues:
        queues[guild_id] = []

    try:
        # 使用 API 預先查詢歌名（非同步）
        loop = asyncio.get_event_loop()
        title, _ = await loop.run_in_executor(
            None, lambda: get_audio_stream_and_info(search)
        )

        song_item = {'title': title, 'query': search}

        if not voice_client.is_playing() and not voice_client.is_paused():
            await play_song_async(guild_id, interaction.channel, song_item)
            await interaction.followup.send(f"🎶 開始播放 **{title}**！")
        else:
            queues[guild_id].append(song_item)
            embed = discord.Embed(
                title="📝 已加入播放佇列", 
                description=f"**{title}** (排隊順位：#{len(queues[guild_id])})", 
                color=discord.Color.blue()
            )
            await interaction.followup.send(embed=embed)

    except Exception as e:
        await interaction.followup.send(f"❌ 抓取失敗，發生錯誤：`{e}`")

@bot.tree.command(name="queue", description="查看目前的播放佇列歌單")
async def queue(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    if guild_id in queues and len(queues[guild_id]) > 0:
        msg = f"📜 **即將播放的歌單（剩餘 {len(queues[guild_id])} 首）：**\n"
        for i, song in enumerate(queues[guild_id][:10], start=1):
            msg += f"{i}. {song['title']}\n"
        if len(queues[guild_id]) - 10 > 0:
            msg += f"\n... 以及另外 {len(queues[guild_id]) - 10} 首歌"
        await interaction.response.send_message(msg)
    else:
        await interaction.response.send_message("📂 目前佇列是空的！使用 `/play` 來點歌吧。", ephemeral=True)

@bot.tree.command(name="pause", description="暫停目前播放的音樂")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await interaction.response.send_message("⏸️ 已暫停播放音樂。")
    else:
        await interaction.response.send_message("⚠️ 目前沒有正在播放的音樂！", ephemeral=True)

@bot.tree.command(name="resume", description="恢復播放暫停中的音樂")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await interaction.response.send_message("▶️ 已恢復播放音樂。")
    else:
        await interaction.response.send_message("⚠️ 目前音樂並未被暫停！", ephemeral=True)

@bot.tree.command(name="skip", description="跳過當前歌曲，播放下一首")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await interaction.response.send_message("⏭️ 已跳過歌曲！")
    else:
        await interaction.response.send_message("⚠️ 目前沒有歌曲在播放！", ephemeral=True)

@bot.tree.command(name="leave", description="讓機器人退出語音頻道")
async def leave(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    cancel_timeout_task(guild_id)
    if guild_id in queues:
        queues[guild_id].clear()
        
    voice_client = interaction.guild.voice_client
    if voice_client:
        await voice_client.disconnect()
        await interaction.response.send_message("👋 已離開語音頻道並清空歌單！")
    else:
        await interaction.response.send_message("⚠️ 機器人不在語音頻道中！", ephemeral=True)

# --- 主程式啟動點 ---
if __name__ == "__main__":
    keep_alive()
    bot.run(BOT_TOKEN)
