import discord
from discord.ext import commands
import os
from dotenv import load_dotenv
from petey import scheduler as petey_scheduler
from petey import database
from petey import flow_engine as petey_flow_engine

import sys
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load environment variables
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise Exception("Discord token not found in .env file.")

# Set up intents (including voice_states for voice channel functionality)
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
intents.voice_states = True

# Cog extensions to load
COG_EXTENSIONS = [
    "petey.cogs.general",
    "petey.cogs.admin",
    "petey.cogs.media",
    "petey.cogs.music_player",
    "petey.cogs.chat",
]


class PETEYBot(commands.Bot):
    """Custom bot subclass with async setup_hook for Cog loading."""

    async def setup_hook(self):
        # Initialise memory DB tables on startup
        database.init_db()

        # Load all Cog extensions
        for ext in COG_EXTENSIONS:
            try:
                await self.load_extension(ext)
                print(f"[SETUP] Loaded extension: {ext}")
            except Exception as e:
                print(f"[ERROR] Failed to load extension {ext}: {e}")
                import traceback
                traceback.print_exc()

        # Slash commands are synced manually via @PETEY sync in Discord.
        # Auto-sync is disabled to avoid Discord error 50240 (Entry Point conflict).
        print("[SETUP] Slash command auto-sync skipped. Use @PETEY sync to sync manually.")


# Create the bot instance; allow invocation by mention to phase out '!!'
bot = PETEYBot(command_prefix=commands.when_mentioned, intents=intents)

@bot.event
async def on_ready():
    import datetime
    print(f"[{datetime.datetime.utcnow().isoformat()}] Logged in as {bot.user}")
    petey_scheduler.start_scheduler(bot)
    petey_flow_engine.start_flow_engine(bot)


# Run the bot
try:
    bot.run(TOKEN)
finally:
    print("\n[MAIN] Shutting down.")
