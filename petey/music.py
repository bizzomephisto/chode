import discord
import asyncio
from petey import utils
import yt_dlp as youtube_dl
import random

# Global dictionaries for music queue, history, and control messages.
music_queues = {}            # Key: guild.id, Value: list of song queries
music_history = {}           # Key: guild.id, Value: list of previously played song queries
music_control_messages = {}  # Key: guild.id, Value: tuple(channel_id, message_id)
music_now_playing = {}       # Key: guild.id, Value: query string of current track (survives vc.stop)
_manual_stop = set()         # Guild IDs where we manually stopped to suppress after_playing auto-advance
_status_ticker_tasks = {}    # Key: guild.id, Value: asyncio.Task for the scrolling status ticker
_track_titles = {}           # Key: url/query string, Value: human-readable title (cache)

ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'nocheckcertificate': True,
    'ignoreerrors': False,
    'logtostderr': False,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0'
}

ffmpeg_options = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}

ytdl = youtube_dl.YoutubeDL(ytdl_format_options)

# Second instance that allows playlists (flat extraction for speed)
ytdl_playlist_options = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'extract_flat': 'in_playlist',
    'ignoreerrors': True,
    'quiet': True,
    'no_warnings': True,
    'source_address': '0.0.0.0'
}
ytdl_playlist = youtube_dl.YoutubeDL(ytdl_playlist_options)


async def extract_playlist(url, *, loop=None):
    """Extracts individual video URLs from a YouTube playlist.
    Returns a list of (title, url) tuples."""
    loop = loop or asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl_playlist.extract_info(url, download=False))
    except Exception as e:
        print(f"[DEBUG] Playlist extraction error: {e}")
        import traceback
        traceback.print_exc()
        return []
    
    if data is None:
        print("[DEBUG] Playlist extraction returned None")
        return []
    
    print(f"[DEBUG] Playlist data type: {data.get('_type', 'unknown')}, title: {data.get('title', 'N/A')}, entries: {len(data.get('entries', []))}")
    
    if 'entries' not in data:
        print(f"[DEBUG] No 'entries' key in data. Keys: {list(data.keys())}")
        return []
    
    tracks = []
    for entry in data['entries']:
        if entry is None:
            continue
        vid_id = entry.get('id', '')
        video_url = entry.get('url') or entry.get('webpage_url') or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else None)
        if not video_url:
            continue
        title = entry.get('title', video_url)
        tracks.append((title, video_url))
        # Cache the title for display in dashboard
        _track_titles[video_url] = title
    
    print(f"[DEBUG] Extracted {len(tracks)} tracks from playlist")
    return tracks

class YTDLSource(discord.PCMVolumeTransformer):
    def __init__(self, source, *, data, volume=0.5):
        super().__init__(source, volume)
        self.data = data
        self.title = data.get('title')
        self.url = data.get('url')

    @classmethod
    async def from_url(cls, url, *, loop=None, stream=True):
        loop = loop or asyncio.get_event_loop()
        try:
            data = await loop.run_in_executor(None, lambda: ytdl.extract_info(url, download=not stream))
        except Exception as e:
            print(f"[DEBUG] Error extracting info: {e}")
            raise e
        if 'entries' in data:
            data = data['entries'][0]
        filename = data['url'] if stream else ytdl.prepare_filename(data)
        import os
        import platform
        import shutil
        if platform.system() == "Windows":
            ffmpeg_executable = os.path.join(os.path.dirname(__file__), "ffmpeg", "ffmpeg.exe")
            if not os.path.exists(ffmpeg_executable):
                ffmpeg_executable = shutil.which("ffmpeg") or "ffmpeg"
        else:
            ffmpeg_executable = shutil.which("ffmpeg") or "ffmpeg"
        return cls(discord.FFmpegPCMAudio(filename, executable=ffmpeg_executable, **ffmpeg_options), data=data)

class MusicQueueSelect(discord.ui.Select):
    def __init__(self, ctx):
        guild_id = ctx.guild.id
        queue = music_queues.get(guild_id, [])
        options = []
        if not queue:
            options.append(discord.SelectOption(label="Queue is empty", value="empty", emoji="👻"))
        else:
            for idx, q in enumerate(queue[:25]): # Discord select limit 25
                display = _track_titles.get(q, q)[:90]
                options.append(discord.SelectOption(label=f"{idx+1}. {display}", value=str(idx), emoji="🎧"))
                
        super().__init__(placeholder="Jump directly to a queued track...", min_values=1, max_values=1, options=options, row=1, disabled=(len(queue)==0))

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "empty":
            await interaction.response.defer()
            return

        idx = int(self.values[0])
        guild_id = interaction.guild_id
        vc = interaction.guild.voice_client
        if vc and guild_id in music_queues:
            # Re-order queue so the selected song is next, then skip!
            queue = music_queues[guild_id]
            selected_song = queue.pop(idx)
            queue.insert(0, selected_song) # Push it to the front
            await interaction.response.defer()
            vc.stop() # Triggers after_playing which magically pulls from index 0

class MusicHistorySelect(discord.ui.Select):
    """Dropdown showing previously played tracks so users can jump back."""
    def __init__(self, ctx):
        guild_id = ctx.guild.id
        history = music_history.get(guild_id, [])
        options = []
        if not history:
            options.append(discord.SelectOption(label="No history yet", value="empty", emoji="👻"))
        else:
            # Show most recent first, up to 25
            for idx, h in enumerate(reversed(history[-25:])):
                real_idx = len(history) - 1 - idx  # Map back to actual index
                display = _track_titles.get(h, h)[:90]
                options.append(discord.SelectOption(label=f"{idx+1}. {display}", value=str(real_idx), emoji="🔙"))
                
        super().__init__(placeholder="🕐 Replay a previous track...", min_values=1, max_values=1, options=options, row=2, disabled=(len(history)==0))

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "empty":
            await interaction.response.defer()
            return

        idx = int(self.values[0])
        guild_id = interaction.guild_id
        ctx = self.view.ctx
        vc = interaction.guild.voice_client
        if vc and guild_id in music_history:
            history = music_history[guild_id]
            if idx < len(history):
                selected_song = history.pop(idx)
                await interaction.response.defer()
                # Manually stop current audio without triggering auto-advance
                if vc.is_playing() or vc.is_paused():
                    _manual_stop.add(guild_id)
                    vc.stop()
                await play_song(ctx, selected_song, skip_history=False)

class MusicControlView(discord.ui.View):
    def __init__(self, ctx):
        super().__init__(timeout=None)
        self.ctx = ctx
        self.add_item(MusicQueueSelect(ctx))
        self.add_item(MusicHistorySelect(ctx))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Music player is public! Anyone in the voice channel can interact with it.
        if interaction.user.voice and interaction.user.voice.channel == self.ctx.guild.voice_client.channel:
            return True
        await interaction.response.send_message("❌ You must be in the active Voice Channel to control DJ PETEY.", ephemeral=True)
        return False

    def build_embed(self):
        vc = self.ctx.guild.voice_client
        guild_id = self.ctx.guild.id
        queue = music_queues.get(guild_id, [])
        history = music_history.get(guild_id, [])
        
        emb = discord.Embed(title="🎵 DJ PETEY Dashboard", color=discord.Color.blurple())
        
        if vc and (vc.is_playing() or vc.is_paused()) and hasattr(vc.source, 'data'):
            title = vc.source.data.get('title', 'Unknown Title')
            url = vc.source.data.get('webpage_url', '')
            uploader = vc.source.data.get('uploader', 'Unknown Artist')
            
            # Formatter for duration (YTDL usually returns integer seconds or a formatted string)
            duration_raw = vc.source.data.get('duration')
            if isinstance(duration_raw, int):
                mins, secs = divmod(duration_raw, 60)
                duration = f"{mins}:{secs:02d}"
            else:
                duration = vc.source.data.get('duration_string', '??:??')
                
            thumbnail = vc.source.data.get('thumbnail', '')
            
            status = "⏸️ **PAUSED:** " if vc.is_paused() else "▶️ **NOW PLAYING:** "
            emb.description = f"{status}\n**[{title}]({url})**\n*By {uploader}* | ⏱️ `{duration}`"
            if thumbnail:
                emb.set_thumbnail(url=thumbnail)
        else:
            emb.description = "**Idle. Queue up some tracks!**"

        # Show recently played tracks (last 5, reversed so most recent is first)
        if history:
            h_list = ""
            recent = list(reversed(history[-5:]))
            for i, h in enumerate(recent):
                display = _track_titles.get(h, h)[:50]
                h_list += f"`{i+1}.` {display}\n"
            if len(history) > 5:
                h_list += f"*...and {len(history)-5} older tracks.*"
            emb.add_field(name="🕐 Previously Played", value=h_list, inline=False)

        if queue:
            q_list = ""
            for i, q in enumerate(queue[:5]): # Show up to 5 next tracks
                display = _track_titles.get(q, q)[:50]
                q_list += f"`{i+1}.` {display}\n"
            if len(queue) > 5:
                q_list += f"*...and {len(queue)-5} more tracks.*"
            emb.add_field(name="📋 Up Next", value=q_list, inline=False)
        else:
            emb.add_field(name="📋 Up Next", value="*Queue is empty.*", inline=False)

        emb.set_footer(text=f"🎧 {len(history)} played | {len(queue)} queued")
        return emb

    @discord.ui.button(label="⏮️ Prev", style=discord.ButtonStyle.secondary, row=0)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await prev_command(self.ctx)

    @discord.ui.button(label="⏯️ Pause/Play", style=discord.ButtonStyle.primary, row=0)
    async def pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await pause_command(self.ctx)

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, row=0)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await next_command(self.ctx)

    @discord.ui.button(label="⏹️ Stop", style=discord.ButtonStyle.danger, row=0)
    async def stop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await stop_command(self.ctx)


async def _run_status_ticker(channel, full_text, guild_id, title, artist, requester):
    """Background task that alternates voice channel status between song info and requester."""
    status_a = f"🎵 {title} — {artist}"
    status_b = f"🎧 Requested by: {requester}"
    
    try:
        while True:
            await channel.edit(status=status_a[:500])
            await asyncio.sleep(30)
            await channel.edit(status=status_b[:500])
            await asyncio.sleep(30)
    except asyncio.CancelledError:
        return
    except Exception as e:
        print(f"[DEBUG] Status ticker error: {e}")


async def resync_dashboard(ctx):
    """Fetches and updates the active Music Dashboard embed."""
    guild_id = ctx.guild.id
    if guild_id in music_control_messages:
        chan_id, msg_id = music_control_messages[guild_id]
        channel = ctx.guild.get_channel(chan_id)
        if channel:
            try:
                msg = await channel.fetch_message(msg_id)
                view = MusicControlView(ctx)
                await msg.edit(embed=view.build_embed(), view=view)
            except discord.NotFound:
                pass


async def play_song(ctx, query: str, skip_history=False):
    """Plays the song immediately and sets up a new control message.
    skip_history: If True, don't push current track to history (used by prev_command to avoid duplicates).
    """
    vc = ctx.voice_client
    guild_id = ctx.guild.id
    
    # Push the PREVIOUS now-playing track to history before we overwrite it
    if not skip_history and guild_id in music_now_playing and music_now_playing[guild_id]:
        music_history.setdefault(guild_id, []).append(music_now_playing[guild_id])
        
    try:
        player = await YTDLSource.from_url(query, loop=ctx.bot.loop, stream=True)
    except Exception as e:
        import traceback
        await ctx.send("❌ Error retrieving audio. Please try a different query.")
        print(f"[DEBUG] Error in YTDLSource.from_url: {e}")
        traceback.print_exc()
        return
    
    # Cache the resolved title for this query so queue/history shows proper names
    _track_titles[query] = player.data.get('title', query)

    def after_playing(error):
        if error:
            print(f"[DEBUG] Player error: {error}")
        print("[DEBUG] after_playing callback triggered.")
        # If this was a manual stop (prev/history jump), don't auto-advance
        if ctx.guild.id in _manual_stop:
            _manual_stop.discard(ctx.guild.id)
            return
        if ctx.guild.id in music_queues:
            coro = play_next(ctx)
            import asyncio
            asyncio.run_coroutine_threadsafe(coro, ctx.bot.loop)

    vc.play(player, after=after_playing)
    
    # Track what's currently playing so we can push it to history later
    music_now_playing[guild_id] = query
    
    # Start alternating voice channel status ticker
    title = player.data.get('title', 'Unknown')
    artist = player.data.get('uploader', 'Unknown')
    requester = ctx.author.display_name
    
    # Cancel any existing ticker for this guild
    if guild_id in _status_ticker_tasks:
        _status_ticker_tasks[guild_id].cancel()
    _status_ticker_tasks[guild_id] = asyncio.create_task(_run_status_ticker(vc.channel, None, guild_id, title, artist, requester))
    
    # Delete old control message if it exists to keep chat clean
    if guild_id in music_control_messages:
        chan_id, msg_id = music_control_messages[guild_id]
        ancient_channel = ctx.guild.get_channel(chan_id)
        if ancient_channel:
            try:
                old_msg = await ancient_channel.fetch_message(msg_id)
                await old_msg.delete()
            except discord.NotFound:
                pass

    # Send brand new control message synced to the bottom of the chat
    view = MusicControlView(ctx)
    control_msg = await ctx.send(embed=view.build_embed(), view=view)
    music_control_messages[guild_id] = (ctx.channel.id, control_msg.id)

async def play_next(ctx):
    """Plays the next song from the queue if available; otherwise disconnects."""
    guild_id = ctx.guild.id
    if guild_id in music_queues and music_queues[guild_id]:
        next_query = music_queues[guild_id].pop(0)
        await play_song(ctx, next_query)
    else:
        vc = ctx.voice_client
        if vc and vc.is_connected():
            await resync_dashboard(ctx)
            if guild_id in music_control_messages:
                chan_id, msg_id = music_control_messages[guild_id]
                ch = ctx.guild.get_channel(chan_id)
                if ch: await ch.send("📭 Queue finished. Use `/play` to add more tracks!")

async def prev_command(ctx):
    """Plays the previous song from history, if available."""
    guild_id = ctx.guild.id
    if guild_id in music_history and music_history[guild_id]:
        prev_query = music_history[guild_id].pop()
        vc = ctx.voice_client
        # Manually stop current audio without triggering auto-advance
        if vc and (vc.is_playing() or vc.is_paused()):
            _manual_stop.add(guild_id)
            vc.stop()
        # Use skip_history=True so we don't re-push the current song into history
        await play_song(ctx, prev_query, skip_history=True)
    else:
        await ctx.send("❌ No previous song found in memory.", delete_after=5)

async def pause_command(ctx):
    """Toggles pause/resume on the current song."""
    vc = ctx.voice_client
    if not vc: return
    if vc.is_playing():
        vc.pause()
    elif vc.is_paused():
        vc.resume()
    await resync_dashboard(ctx)

async def play_command(ctx, query: str):
    """Handles play command; if a song is already playing, adds to the queue."""
    vc = ctx.voice_client
    if vc.is_playing() or vc.is_paused():
        guild_id = ctx.guild.id
        music_queues.setdefault(guild_id, []).append(query)
        await resync_dashboard(ctx)
    else:
        await play_song(ctx, query)


async def play_playlist_command(ctx, url: str):
    """Extracts a YouTube playlist and queues all tracks."""
    msg = await ctx.send("💿 *Extracting playlist... this may take a moment...*")
    tracks = await extract_playlist(url, loop=ctx.bot.loop)
    
    if not tracks:
        await msg.edit(content="❌ Could not extract any tracks from that playlist.")
        return
    
    guild_id = ctx.guild.id
    vc = ctx.voice_client
    
    # Play the first track immediately if nothing is playing
    first_title, first_url = tracks[0]
    remaining = tracks[1:]
    
    # Queue all remaining tracks
    music_queues.setdefault(guild_id, []).extend([url for _, url in remaining])
    
    await msg.edit(content=f"🎵 Loaded **{len(tracks)}** tracks from playlist! Playing first track now...")
    
    if vc.is_playing() or vc.is_paused():
        # Something is already playing, queue the first track too
        music_queues[guild_id].insert(0, first_url)
        await resync_dashboard(ctx)
    else:
        await play_song(ctx, first_url)

async def next_command(ctx):
    """Skips to the next song."""
    vc = ctx.voice_client
    if not vc or not vc.is_connected(): return
    if vc.is_playing() or vc.is_paused():
        vc.stop()  # Stop current song. This will trigger after_playing which schedules play_next.

async def stop_command(ctx):
    """Stops the current song, clears the queue AND history, and disconnects from the voice channel."""
    vc = ctx.voice_client
    if not vc: return
    guild_id = ctx.guild.id
    
    # Nuke everything for this guild's session
    music_queues[guild_id] = []
    music_history[guild_id] = []
    music_now_playing.pop(guild_id, None)
    
    # Kill the scrolling status ticker
    if guild_id in _status_ticker_tasks:
        _status_ticker_tasks[guild_id].cancel()
        del _status_ticker_tasks[guild_id]
    
    # Clear voice channel status
    try:
        await vc.channel.edit(status=None)
    except Exception:
        pass
    
    # Try to clean up the control dashboard before dying
    if guild_id in music_control_messages:
        chan_id, msg_id = music_control_messages[guild_id]
        ch = ctx.guild.get_channel(chan_id)
        if ch:
            try:
                msg = await ch.fetch_message(msg_id)
                await msg.delete()
            except discord.NotFound: pass
        del music_control_messages[guild_id]
        
    vc.stop()
    await vc.disconnect()
