import discord
from discord import app_commands
from discord.ext import commands
from chode import config
import traceback


class AdminCog(commands.Cog, name="Admin"):
    """Server administration and bot management commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="setup", description="Sets the bot's personality for the server or DMs.")
    @app_commands.describe(personality="The personality prompt for PETEY in this server")
    async def setup(self, ctx: commands.Context, *, personality: str):
        """Sets the bot's personality for the server or DMs. Requires Owner, CHODEADMIN, or PETEYADMIN role in servers."""
        if ctx.guild:
            is_owner = ctx.author == ctx.guild.owner
            is_bizzo = ctx.author.name.lower() == "bizzo"
            has_role = hasattr(ctx.author, 'roles') and any(role.name in ["CHODEADMIN", "PETEYADMIN"] for role in ctx.author.roles)

            if is_owner or has_role or is_bizzo:
                try:
                    conf = config.load_server_config(ctx.guild.id)
                    conf["personality"] = personality
                    config.save_server_config(ctx.guild.id, conf)
                    await ctx.send("Personality has been updated for this server!")
                except Exception as e:
                    await ctx.send(f"Failed to save personality settings. Error: {e}")
                    print(f"[ERROR] Failed saving server config for {ctx.guild.id}: {e}")
                    traceback.print_exc()
            else:
                await ctx.send("You do not have permission to use this command here (Requires Server Owner, 'CHODEADMIN', or 'PETEYADMIN' role).")
        else:
            dm_key = f"DM-{ctx.author.id}"
            try:
                conf = config.load_server_config(dm_key)
                conf["personality"] = personality
                config.save_server_config(dm_key, conf)
                await ctx.send("Personality has been updated for your DMs!")
            except Exception as e:
                await ctx.send(f"Failed to save personality settings for DMs. Error: {e}")
                print(f"[ERROR] Failed saving DM config for {ctx.author.id}: {e}")
                traceback.print_exc()

    @commands.hybrid_command(name="shutdown", description="[DISABLED] Safely shuts down the bot.")
    async def shutdown(self, ctx: commands.Context):
        """[DISABLED] Shutdown via Discord has been disabled for security."""
        await ctx.send("❌ Remote shutdown has been disabled. Use the server console to manage the bot process.")

    @commands.hybrid_command(name="sync", description="Manually syncs the slash command tree. Owner-only.")
    async def sync_commands(self, ctx: commands.Context, mode: str = None):
        """Manually syncs the slash command tree. Owner-only. Use '@PETEY sync clear' to wipe ghost commands."""
        if ctx.author.name.lower() != "bizzo":
            is_owner = ctx.guild and ctx.author == ctx.guild.owner
            if not is_owner:
                await ctx.send("❌ Only the bot owner can sync commands.")
                return

        status_msg = "Clearing and Syncing..." if mode == "clear" else "Syncing command tree..."
        msg = await ctx.send(f"🔄 {status_msg}")

        try:
            if mode == "clear":
                self.bot.tree.clear_commands(guild=None)
                if ctx.guild:
                    self.bot.tree.clear_commands(guild=ctx.guild)
                    await self.bot.tree.sync(guild=ctx.guild)

            # Always use raw HTTP sync to preserve the Entry Point command (type 4).
            # Discord error 50240 fires if you bulk-upsert without including it.
            existing = await self.bot.http.get_global_commands(self.bot.application_id)
            entry_points = [cmd for cmd in existing if cmd.get("type") == 4]

            # Build the payload discord.py would normally send
            payload = [cmd.to_dict(self.bot.tree) for cmd in self.bot.tree.get_commands()]

            # Inject any Entry Point commands that aren't already in the payload
            payload_names = {p.get("name") for p in payload}
            for ep in entry_points:
                if ep.get("name") not in payload_names:
                    payload.append(ep)

            synced_raw = await self.bot.http.bulk_upsert_global_commands(self.bot.application_id, payload)

            guild_msg = ""
            if ctx.guild:
                self.bot.tree.copy_global_to(guild=ctx.guild)
                synced_guild = await self.bot.tree.sync(guild=ctx.guild)
                guild_msg = f" and registered **{len(synced_guild)}** commands instantly to this server"

            if mode == "clear":
                await msg.edit(content=f"✅ **Brain Wiped & Re-synced!**\nCleaned ghost commands, registered **{len(synced_raw)}** commands globally{guild_msg}.")
            else:
                await msg.edit(content=f"✅ Synced **{len(synced_raw)}** commands globally{guild_msg}!")

        except Exception as e:
            await msg.edit(content=f"❌ Sync failed: {e}")
            traceback.print_exc()


    @commands.hybrid_command(name="ingest", description="Uploads a file directly into Petey's long-term memory. Config channel only.")
    @app_commands.describe(attachment="The file to ingest (.txt, .md, .pdf, or .json)")
    async def ingest(self, ctx: commands.Context, attachment: discord.Attachment):
        """Uploads a file directly into PETEY's long-term memory. Config channel only."""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.")
            return

        conf = config.load_server_config(ctx.guild.id)
        config_channel = conf.get("config_channel")

        if not config_channel:
            await ctx.send("❌ A config channel has not been set up yet. Please set one up in the dashboard wizard!")
            return

        if str(ctx.channel.id) != str(config_channel):
            await ctx.send(f"❌ This command can only be run in the designated config channel (<#{config_channel}>).")
            return

        # Check permissions: owner or petey_admin / CHODEADMIN / PETEYADMIN roles
        is_owner = ctx.author == ctx.guild.owner
        is_bizzo = ctx.author.name.lower() == "bizzo"
        has_role = hasattr(ctx.author, 'roles') and any(role.name in ["CHODEADMIN", "PETEYADMIN", "petey_admin"] for role in ctx.author.roles)

        if not (is_owner or has_role or is_bizzo):
            await ctx.send("❌ You do not have permission to run this command (Requires Server Owner, or 'petey_admin' / 'CHODEADMIN' / 'PETEYADMIN' role).")
            return

        # Validate file extension
        filename = attachment.filename.lower()
        if not any(filename.endswith(ext) for ext in ['.txt', '.md', '.pdf', '.json']):
            await ctx.send("❌ Only .txt, .md, .pdf, and .json files are allowed.")
            return

        await ctx.defer(ephemeral=False)

        try:
            from chode import database
            # Download file content
            file_bytes = await attachment.read()
            
            # Parse text
            if filename.endswith('.pdf'):
                import io
                try:
                    import PyPDF2
                    pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_bytes))
                    content = ""
                    for page in pdf_reader.pages:
                        extracted = page.extract_text()
                        if extracted:
                            content += extracted + "\n"
                except ImportError:
                    await ctx.send("❌ PyPDF2 is not installed on the server. Please run 'pip install PyPDF2'.")
                    return
                except Exception as e:
                    await ctx.send(f"❌ Failed to parse PDF file: {e}")
                    return
            else:
                content = file_bytes.decode('utf-8', errors='ignore')

            if not content.strip():
                await ctx.send("❌ The uploaded file is empty.")
                return

            # Store in long-term memory
            database.store_document(ctx.guild.id, attachment.filename, content)
            await ctx.send(f"✅ Successfully queued **{attachment.filename}** for long-term memory ingestion!")
        except Exception as e:
            await ctx.send(f"❌ Error during ingestion: {e}")
            print(f"[INGEST ERROR] {e}")
            traceback.print_exc()

    @commands.hybrid_command(name="schedule", aliases=["task"], description="[DISABLED] Schedule a recurring task.")
    @commands.has_permissions(manage_messages=True)
    async def schedule(self, ctx: commands.Context, *, prompt: str):
        """[DISABLED] Schedule a recurring task using a natural language prompt."""
        await ctx.send("❌ Scheduling features are currently disabled.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
