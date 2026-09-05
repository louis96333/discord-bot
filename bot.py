import os
import threading
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from flask import Flask
import yt_dlp

# ==========================================
# 1. 自動處理 YouTube Cookie (從環境變數寫入 cookies.txt)
# ==========================================
cookie_str = os.getenv("YOUTUBE_COOKIE_STRING")
if cookie_str:
    print("檢測到 YOUTUBE_COOKIE_STRING 環境變數，正在產生 cookies.txt...")
    with open("cookies.txt", "w", encoding="utf-8") as f:
        f.write("# Netscape HTTP Cookie File\n")
        for item in cookie_str.split("; "):
            if "=" in item:
                k, v = item.split("=", 1)
                f.write(f".youtube.com\tTRUE\t/\tFALSE\t0\t{k}\t{v}\n")
    print("cookies.txt 產生完成！")
else:
    print("警告：未設置 YOUTUBE_COOKIE_STRING 環境變數，音樂抓取可能會被 YouTube 阻擋。")
# --- 防休眠網頁伺服器 (Render Web Service 必備) ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_web():
    # 讀取 Render 自動分配的 Port，預設 5000
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = threading.Thread(target=run_web)
    t.start()

# --- 基本設定 ---
# 讀取環境變數中的 Token
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# 設定無音樂播放或頻道無人時的自動退出時間 (單位：秒)
INACTIVITY_TIMEOUT = 300  # 300 秒 = 5 分鐘

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 佇列與閒置任務管理
queues = {}
timeout_tasks = {}

# --- yt-dlp & FFmpeg 設定 ---
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'extractaudio': True,
    'audioformat': 'mp3',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    # 加上下面這段來偽裝成行動裝置客戶端：
    'cookiefile': 'cookies.txt',  # <--- 補上這行
    'extractor_args': {          # <--- 補上這段
        'youtube': {
            'player_client': ['mweb', 'tv_embedded', 'android']
        }
    }
}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn',
}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

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
            
            # 檢查是否沒有在播歌，或者語音頻道只剩機器人自己
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
        # 當佇列清空且歌曲確實播完後，才開始倒數計時離線
        cancel_timeout_task(guild_id)
        task = bot.loop.create_task(inactivity_disconnect(guild_id, text_channel))
        timeout_tasks[guild_id] = task

async def play_song_async(guild_id, text_channel, song_info):
    """實際解析網址並由 FFmpeg 播放音訊的核心處理邏輯"""
    guild = bot.get_guild(guild_id)
    if not guild or not guild.voice_client:
        return

    voice_client = guild.voice_client

    try:
        # 開始播放新歌，取消任何離線倒數
        cancel_timeout_task(guild_id)

        loop = asyncio.get_event_loop()
        
        # 針對單曲抓取真實播放串流網址
        single_ytdl = yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'quiet': True})
        data = await loop.run_in_executor(None, lambda: single_ytdl.extract_info(song_info['url'], download=False))
        stream_url = data['url'] if 'url' in data else song_info['url']

        player = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        voice_client.play(
            player,
            after=lambda e: play_next(guild_id, text_channel)
        )

        view = MusicPlayerView(voice_client, guild_id, text_channel)
        embed = discord.Embed(
            title="🎶 正在播放", 
            description=f"**{song_info['title']}**", 
            color=discord.Color.green()
        )
        await text_channel.send(embed=embed, view=view)

    except Exception as e:
        await text_channel.send(f"❌ 播放 `{song_info['title']}` 時發生錯誤：`{e}`")
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

    # 計算當前頻道內的真人數量
    human_members = [m for m in voice_client.channel.members if not m.bot]

    if len(human_members) == 0:
        # 如果頻道裡沒人了，開始離線倒數
        cancel_timeout_task(guild.id)
        task = bot.loop.create_task(inactivity_disconnect(guild.id, None))
        timeout_tasks[guild.id] = task
    elif len(human_members) > 0 and (voice_client.is_playing() or voice_client.is_paused()):
        # 如果有人進來了且正在放歌，取消離線倒數
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

@bot.tree.command(name="play", description="播放單曲或 YouTube 播放清單/合輯")
@app_commands.describe(search="輸入單曲網址、播放清單網址或關鍵字")
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
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=False))

        if not data:
            await interaction.followup.send("❌ 無法抓取音訊資訊，請確認網址或關鍵字是否正確！")
            return

        added_songs = []

        # 判斷是否為合輯 / 播放清單
        if 'entries' in data and data['entries']:
            for entry in data['entries']:
                if entry:
                    video_id = entry.get('id') or entry.get('url')
                    if video_id and not str(video_id).startswith('http'):
                        url = f"https://www.youtube.com/watch?v={video_id}"
                    else:
                        url = entry.get('url') or entry.get('webpage_url')
                    
                    title = entry.get('title', '未知歌曲')
                    if url:
                        added_songs.append({'title': title, 'url': url})
        else:
            url = data.get('webpage_url') or data.get('url') or search
            title = data.get('title', '未知歌曲')
            added_songs.append({'title': title, 'url': url})

        if not added_songs:
            await interaction.followup.send("❌ 找不到可播放的歌曲或合輯！")
            return

        queues[guild_id].extend(added_songs)

        if not voice_client.is_playing() and not voice_client.is_paused():
            first_song = queues[guild_id].pop(0)
            await play_song_async(guild_id, interaction.channel, first_song)

            if len(added_songs) > 1:
                await interaction.followup.send(f"📚 已成功將合輯加入佇列，共 **{len(added_songs)}** 首歌！第一首：**{first_song['title']}**")
            else:
                await interaction.followup.send(f"🎶 開始播放 **{first_song['title']}**！")
        else:
            if len(added_songs) > 1:
                embed = discord.Embed(
                    title="📝 已將合輯加入播放佇列", 
                    description=f"成功加入 **{len(added_songs)}** 首歌至佇列中！", 
                    color=discord.Color.blue()
                )
            else:
                embed = discord.Embed(
                    title="📝 已加入播放佇列", 
                    description=f"**{added_songs[0]['title']}** (排隊順位：#{len(queues[guild_id])})", 
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
    # 1. 啟動 Web 伺服器 (供 Render 進行健康檢查)
    keep_alive()
    # 2. 啟動 Discord 機器人
    bot.run(BOT_TOKEN)
