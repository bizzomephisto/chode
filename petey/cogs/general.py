import asyncio
import discord
from discord.ext import commands
from petey import config, gemini_api, utils
import traceback


class GeneralCog(commands.Cog, name="General"):
    """Informational and utility commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="peteyhelp", description="Asks PETEY to explain his capabilities.")
    async def peteyhelp(self, ctx: commands.Context):
        """Asks PETEY to explain his capabilities from peteyhelp.txt."""
        await ctx.send("Let me check my manual. Give me a second...")
        try:
            content = utils.read_peteyhelp()

            # Load server personality
            server_id = ctx.guild.id if ctx.guild else ctx.channel.id
            conf = config.load_server_config(server_id)
            personality = conf.get("personality", "You are Petey, a friendly, helpful, and concise AI assistant.")
            
            channel_personalities = conf.get("channel_personalities", {})
            channel_id_str = str(ctx.channel.id)
            if channel_id_str in channel_personalities and channel_personalities[channel_id_str].strip():
                personality = channel_personalities[channel_id_str]

            system_message = (
                f"System: {personality}\n"
                "The following text contains your user manual and command instructions. "
                "Explain how you work to the user in a natural, conversational, and helpful manner based on this text. "
                "Do NOT just regurgitate the raw text. Put it in your own words, maintaining your distinct personality."
            )
            prompt = f"{system_message}\n\n== PETEY Manual ==\n{content}"
            response_text = await asyncio.to_thread(gemini_api.call_gemini, prompt)

            if len(response_text) > 2000:
                await utils.send_long_message(ctx.channel, response_text)
            else:
                await ctx.send(response_text)
        except Exception as e:
            await ctx.send(f"Sorry, I couldn't retrieve the help information. Error: {e}")
            print(f"[ERROR] peteyhelp command failed: {e}")
            traceback.print_exc()

    @commands.hybrid_command(name="whatsnew", description="Shows the latest features and changelog.")
    async def whatsnew(self, ctx: commands.Context):
        """Asks PETEY to summarize his latest updates from whatsnew.txt."""
        await ctx.send("Let me recall what they just did to me...")
        try:
            content = utils.read_whatsnew()

            # Load server personality
            server_id = ctx.guild.id if ctx.guild else ctx.channel.id
            conf = config.load_server_config(server_id)
            personality = conf.get("personality", "You are Petey, a friendly, helpful, and concise AI assistant.")
            
            channel_personalities = conf.get("channel_personalities", {})
            channel_id_str = str(ctx.channel.id)
            if channel_id_str in channel_personalities and channel_personalities[channel_id_str].strip():
                personality = channel_personalities[channel_id_str]

            system_message = (
                f"System: {personality}\n"
                "The following text contains the latest patch notes and updates made to your code. "
                "Explain the cool new things you can do to the user in a natural, conversational, and excited manner. "
                "Do NOT just regurgitate the raw text. Put it in your own words, maintaining your distinct personality."
            )
            prompt = f"{system_message}\n\n== PETEY Update Notes ==\n{content}"
            response_text = await asyncio.to_thread(gemini_api.call_gemini, prompt)

            if len(response_text) > 2000:
                await utils.send_long_message(ctx.channel, response_text)
            else:
                await ctx.send(response_text)
        except Exception as e:
            await ctx.send(f"Sorry, I couldn't retrieve the update information. Error: {e}")
            print(f"[ERROR] whatsnew command failed: {e}")
            traceback.print_exc()

    @commands.hybrid_command(name="github", aliases=["source", "repo"], description="Provides the GitHub repository link for PETEY.")
    async def github(self, ctx: commands.Context):
        """Provides the GitHub repository link for PETEY."""
        await ctx.send("I'm proudly an **open-source project**! You can check out my brain (source code), fork me, or contribute here:\n<https://github.com/bizzomephisto/Petey>")


async def setup(bot: commands.Bot):
    await bot.add_cog(GeneralCog(bot))
