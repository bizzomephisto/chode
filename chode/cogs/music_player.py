import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from chode import music, utils
import traceback


class MusicPlayerCog(commands.Cog, name="Music Player"):
    """DJ CHODE — YouTube music playback commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="play", description="Plays a song from YouTube or URL, or adds it to the queue.")
    @app_commands.describe(query="Search term or YouTube URL")
    async def play(self, ctx: commands.Context, *, query: str):
        """Plays a song from YouTube or URL, or adds it to the queue."""
        if not await utils.check_channel_assignment(ctx, "music"): return
        if not ctx.author.voice:
            await ctx.send("You are not connected to a voice channel!")
            return
        vc = ctx.voice_client
        if not vc:
            try:
                await ctx.author.voice.channel.connect(timeout=30.0)
                vc = ctx.voice_client
            except asyncio.TimeoutError:
                await ctx.send(f"Timed out trying to connect to {ctx.author.voice.channel.mention}.")
                return
            except discord.ClientException as e:
                await ctx.send(f"Already connected to a voice channel or connection failed: {e}")
                return
            except Exception as e:
                await ctx.send(f"Failed to connect to the voice channel {ctx.author.voice.channel.mention}: {e}")
                print(f"[DEBUG] Voice connection error: {e}")
                traceback.print_exc()
                return

        if not vc:
            await ctx.send("Could not establish voice connection.")
            return

        # Detect YouTube playlists and handle them specially
        if query.startswith(("http://", "https://")) and "list=" in query:
            await music.play_playlist_command(ctx, query)
            return

        # Ensure yt-dlp format is used if it's not a URL
        if not query.startswith(("http://", "https://")) and "ytsearch:" not in query and "ytsearch1:" not in query:
            query = f"ytsearch1:{query}"

        await ctx.send(f"Searching/Queueing: `{query}`...")
        await music.play_command(ctx, query)

    @commands.hybrid_command(name="next", description="Plays the next song in the queue.")
    async def next_song(self, ctx: commands.Context):
        """Plays the next song in the queue."""
        if not await utils.check_channel_assignment(ctx, "music"): return
        await music.next_command(ctx)

    @commands.hybrid_command(name="prev", description="Plays the previous song from history.")
    async def prev_song(self, ctx: commands.Context):
        """Plays the previous song from history."""
        if not await utils.check_channel_assignment(ctx, "music"): return
        await music.prev_command(ctx)

    @commands.hybrid_command(name="pause", aliases=["resume"], description="Pauses or resumes the current song.")
    async def pause_song(self, ctx: commands.Context):
        """Pauses or resumes the current song."""
        if not await utils.check_channel_assignment(ctx, "music"): return
        await music.pause_command(ctx)

    @commands.hybrid_command(name="stop", description="Stops the music, clears the queue, and disconnects the bot.")
    async def stop_song(self, ctx: commands.Context):
        """Stops the music, clears the queue, and disconnects the bot."""
        if not await utils.check_channel_assignment(ctx, "music"): return
        await music.stop_command(ctx)

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """Auto-pause when everyone leaves the voice channel, auto-resume when someone joins back."""
        if not member.guild.voice_client:
            return

        bot_vc = member.guild.voice_client
        bot_channel = bot_vc.channel

        # Someone left the bot's channel (or moved away)
        if before.channel == bot_channel and after.channel != bot_channel:
            humans = [m for m in bot_channel.members if not m.bot]
            if len(humans) == 0 and bot_vc.is_playing():
                bot_vc.pause()
                try:
                    guild_id = member.guild.id
                    if guild_id in music.music_control_messages:
                        chan_id, msg_id = music.music_control_messages[guild_id]
                        channel = member.guild.get_channel(chan_id)
                        if channel:
                            await channel.send("⏸️ Everyone left — pausing playback. I'll resume when someone joins back!", delete_after=30)
                except Exception:
                    pass

        # Someone joined the bot's channel
        if after.channel == bot_channel and before.channel != bot_channel and not member.bot:
            if bot_vc.is_paused():
                bot_vc.resume()


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicPlayerCog(bot))
