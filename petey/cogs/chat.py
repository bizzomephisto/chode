import asyncio
import datetime
import re
import aiohttp
import discord
import random
from discord.ext import commands
from petey import config, database, gemini_api, utils, flow_engine
import traceback

class ChatCog(commands.Cog, name="Chat"):
    """Handles LLM chat responses to @mentions and DMs, memory storage, and flow engine triggers."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Proactive tracking: channel_id -> last_run_datetime
        self._last_proactive_time = {}
        # Proactive tracking: server_id -> list of run_datetimes (in last 1 hour)
        self._proactive_hourly_counts = {}

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        print(f"[EVENT] Joined new guild: {guild.name} (ID: {guild.id})")
        if guild.owner:
            try:
                guide_content = utils.read_setup_guide()
                await utils.send_long_message(
                    guild.owner,
                    f"**Thanks for inviting PETEY to {guild.name}!**\n\nHere is a blueprint for the optimal server setup:\n\n{guide_content}"
                )
                print(f"[EVENT] Sent setup guide to owner of {guild.name}")
            except discord.Forbidden:
                print(f"[ERROR] Forbidden! Cannot DM owner of {guild.name}")
            except Exception as e:
                print(f"[ERROR] Failed to DM setup guide to {guild.owner}: {e}")

    def should_trigger_proactive(self, server_id, channel_id, persona_obj, conf):
        """
        Check if we should trigger a proactive message in this channel.
        """
        # Proactive pct must be set (only slot 1 by default unless unlocked, which we get from persona_obj)
        proactive_pct = persona_obj.get("proactive_pct", 0)
        if not proactive_pct or proactive_pct <= 0:
            return False

        now = datetime.datetime.now(datetime.timezone.utc)

        # 1. Check Channel Cooldown
        last_time = self._last_proactive_time.get(channel_id)
        if last_time:
            # global_settings.proactive_cooldown_minutes (default 15)
            cooldown_min = conf.get("global_settings", {}).get("proactive_cooldown_minutes", 15)
            elapsed = (now - last_time).total_seconds() / 60.0
            if elapsed < cooldown_min:
                print(f"[PROACTIVE] Cooldown active for channel {channel_id}: {elapsed:.1f}/{cooldown_min} minutes elapsed.")
                return False

        # 2. Check Server-Global Hourly Cap
        server_key = str(server_id)
        if server_key not in self._proactive_hourly_counts:
            self._proactive_hourly_counts[server_key] = []
        
        # Prune entries older than 1 hour
        self._proactive_hourly_counts[server_key] = [
            t for t in self._proactive_hourly_counts[server_key]
            if (now - t).total_seconds() < 3600
        ]
        
        max_per_hour = conf.get("global_settings", {}).get("proactive_max_per_hour", 6)
        if len(self._proactive_hourly_counts[server_key]) >= max_per_hour:
            print(f"[PROACTIVE] Hourly limit reached for server {server_id}: {len(self._proactive_hourly_counts[server_key])}/{max_per_hour} replies.")
            return False

        # 3. Roll the dice!
        roll = random.randint(1, 100)
        if roll > proactive_pct:
            print(f"[PROACTIVE] Roll failed: {roll} > {proactive_pct}%")
            return False

        # Update timestamps
        self._last_proactive_time[channel_id] = now
        self._proactive_hourly_counts[server_key].append(now)
        print(f"[PROACTIVE] Triggered! Roll: {roll} <= {proactive_pct}%")
        return True

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        """Handles incoming messages for LLM interactions, memory storage, and flow triggers."""
        # Log incoming messages for diagnostics
        msg_prefix = f"[CHAT-ON-MESSAGE] [{message.guild.name if message.guild else 'DM'}]"
        print(f"{msg_prefix} Received from {message.author.name} (Bot: {message.author.bot}): '{message.content[:100]}'")

        if message.author == self.bot.user or message.author.bot:
            return

        # Check Node Flow Builder message triggers
        flow_engine.register_message_trigger(self.bot, message)

        # Determine server identifier
        server_id = message.guild.id if message.guild else f"DM-{message.author.id}"
        conf = {}
        persona_obj = {}
        try:
            conf = config.load_server_config(server_id)
            persona_obj = config.get_persona_object_for_channel(conf, message.channel.id)
            personality = persona_obj["system_prompt"]
        except Exception as e:
            print(f"[ERROR] Failed loading config for {server_id} in on_message: {e}")
            personality = "You are Petey, a friendly chatbot."
            persona_obj = {"slot": 1, "is_default": True, "name": "Petey", "system_prompt": personality, "proactive_pct": 0}

        # Store every message from real users (fire-and-forget, non-blocking)
        if not message.content.startswith('!'):
            def _store_safe():
                try: database.store_memory(server_id, message.channel.id, message.author.id, message.content)
                except Exception as e: print(f"[MEMORY] store_memory failed: {e}")
            asyncio.create_task(asyncio.to_thread(_store_safe))

        # --- Skip if a command was invoked ---
        # discord.py's default on_message already calls process_commands before Cog listeners fire.
        # We check the context to see if a valid command was found.
        ctx = await self.bot.get_context(message)
        if ctx.command:
            print(f"{msg_prefix} Skipping LLM response, valid command/prefix found: {ctx.command.name}")
            return  # Already handled by a command Cog

        # --- LLM Interaction & Specific Mention Logic ---
        should_respond_llm = False
        is_dm = message.guild is None
        
        # Robust mention check (direct mention, role mention, or plain text mention)
        is_mention = self.bot.user in message.mentions
        
        if not is_mention and message.guild:
            # Check role mentions
            bot_name_lower = self.bot.user.name.lower()
            bot_nick_lower = message.guild.me.display_name.lower() if message.guild.me else ""
            for role in message.role_mentions:
                role_name_lower = role.name.lower()
                if role_name_lower == "petey" or role_name_lower == bot_name_lower or (bot_nick_lower and role_name_lower == bot_nick_lower):
                    is_mention = True
                    print(f"{msg_prefix} Bot mentioned via role: @{role.name}")
                    break
        
        if not is_mention:
            # Check plain text content for mentions
            content_lower = message.content.lower()
            trigger_words = ["@petey", f"@{self.bot.user.name.lower()}"]
            if message.guild and message.guild.me:
                trigger_words.append(f"@{message.guild.me.display_name.lower()}")
            for word in trigger_words:
                if word in content_lower:
                    is_mention = True
                    print(f"{msg_prefix} Bot mentioned via plain text: {word}")
                    break

        if is_dm:
            should_respond_llm = True
            print(f"{msg_prefix} DM conversation. Responding via LLM.")
        elif is_mention:
            should_respond_llm = True
            print(f"{msg_prefix} Mention detected. Responding via LLM.")
            content_lower = message.content.lower()
            
            # Clean mention text using regex for robust case-insensitive stripping
            cleaned_mention_content = message.clean_content
            if message.guild and message.guild.me:
                cleaned_mention_content = re.sub(rf"@?{re.escape(message.guild.me.display_name)}", "", cleaned_mention_content, flags=re.IGNORECASE)
            cleaned_mention_content = re.sub(rf"@?{re.escape(self.bot.user.name)}", "", cleaned_mention_content, flags=re.IGNORECASE)
            cleaned_mention_content = re.sub(r"@?petey", "", cleaned_mention_content, flags=re.IGNORECASE)
            cleaned_mention_content = cleaned_mention_content.strip()

            # Activity status mention
            if "what is" in content_lower and "playing" in content_lower:
                target_members = [m for m in message.mentions if m != self.bot.user]
                if target_members and hasattr(utils, 'get_member_info'):
                    member_info = utils.get_member_info(target_members[0])
                    await message.channel.send(member_info)
                    return

            # Mention-based image generation
            elif ("generate" in content_lower and any(word in content_lower for word in ["photo", "image", "picture"])):
                prompt_text = cleaned_mention_content
                prompt_text = prompt_text.replace("generate", "").replace("photo", "").replace("image", "").replace("picture", "").strip()

                print(f"[DEBUG] Mention-based genimg triggered by {message.author.name} with prompt: {prompt_text}")
                command = self.bot.get_command("genimg")
                if command:
                    await command(ctx, prompt=prompt_text)
                else:
                    await message.channel.send("Sorry, the image generation command is unavailable.")
                return

            # Mention-based web search ("look up", "search for", etc.)
            elif any(phrase in content_lower for phrase in ["look up", "search for", "search about", "find out about", "look into"]):
                # Extract the search query by stripping trigger phrases
                search_query = cleaned_mention_content
                for phrase in ["look up", "search for", "search about", "find out about", "look into"]:
                    search_query = search_query.lower().replace(phrase, "")
                search_query = search_query.strip().strip("\"'")

                if not search_query:
                    await message.channel.send("What do you want me to search for? Try: `@PETEY look up <topic>`")
                    return

                processing_msg = await message.channel.send(f"🔍 Searching the web for: `{search_query}`...")
                try:
                    search_results = await utils.perform_web_search(search_query)
                    prompt_for_llm = (
                        f"System: {personality}\n"
                        f"Based on the following internet search results, answer the user's query.\n\n"
                        f"User Query: {search_query}\n\n"
                        f"== Internet Search Results ==\n"
                        f"{search_results}\n\n"
                        f"Petey's Answer:"
                    )
                    response_text = await asyncio.to_thread(gemini_api.call_gemini, prompt_for_llm)
                    if len(response_text) > 2000:
                        await processing_msg.delete()
                        await utils.send_long_message(message.channel, response_text)
                    else:
                        await processing_msg.edit(content=response_text)
                except Exception as e:
                    await processing_msg.edit(content=f"❌ Error performing search: {e}")
                return

            # Server info mention
            elif "what server" in content_lower:
                prompt_for_llm = (
                    f"System: {personality}\n"
                    f"The user {message.author.name} asked: '{message.clean_content}'. Respond in character about the current server.\n"
                    f"Server Info: Name: {message.guild.name}, ID: {message.guild.id}, Member Count: {message.guild.member_count}"
                )
                response_text = await asyncio.to_thread(gemini_api.call_gemini, prompt_for_llm)
                if len(response_text) > 2000: await utils.send_long_message(message.channel, response_text)
                else: await message.channel.send(response_text)
                return

            else:
                should_respond_llm = True

        if not is_dm and not is_mention and not should_respond_llm:
            # Check proactive triggers for messages in text channels
            if self.should_trigger_proactive(server_id, message.channel.id, persona_obj, conf):
                should_respond_llm = True
                print(f"[PROACTIVE] Triggering response to {message.author.name} in #{message.channel.name}")

        # --- Generic LLM Response Logic ---
        if should_respond_llm:
            print(f"[DEBUG] Initiating LLM generic response workflow for user {message.author.name}...")

            # Clean mention text using regex for robust case-insensitive stripping
            cleaned_content = message.clean_content
            if message.guild and message.guild.me:
                cleaned_content = re.sub(rf"@?{re.escape(message.guild.me.display_name)}", "", cleaned_content, flags=re.IGNORECASE)
            cleaned_content = re.sub(rf"@?{re.escape(self.bot.user.name)}", "", cleaned_content, flags=re.IGNORECASE)
            cleaned_content = re.sub(r"@?petey", "", cleaned_content, flags=re.IGNORECASE)
            cleaned_content = cleaned_content.strip()

            # Check for Long-Term Storage trigger (+++)
            force_long_term = False
            if cleaned_content.endswith("+++"):
                force_long_term = True
                cleaned_content = cleaned_content[:-3].strip()
                print(f"[DEBUG] User triggered Long-Term Archive Storage (+++ mode)")

            print(f"[DEBUG] Final LLM cleaned user prompt: '{cleaned_content}'")

            async with message.channel.typing():
                # --- Detect and process image attachments ---
                image_description = None
                if message.attachments:
                    for att in message.attachments:
                        fn = att.filename.lower()
                        if any(fn.endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".webp", ".gif"]) or (att.content_type and att.content_type.startswith("image/")):
                            try:
                                print(f"[IMAGE-TO-TEXT] Processing image {att.filename}...")
                                from petey.deapi_client import deapi
                                image_bytes = await att.read()
                                extracted_text = await deapi.image_to_text(image_bytes, guild_id=message.guild.id if message.guild else None)
                                if extracted_text:
                                    image_description = extracted_text.strip()
                                    print(f"[IMAGE-TO-TEXT] Successfully read image: {image_description[:100]}")
                            except Exception as img_err:
                                print(f"[IMAGE-TO-TEXT ERROR] Failed to process image: {img_err}")
                            break  # Only process the first image attachment

                # --- Fetch context concurrently ---
                async def get_chat_history():
                    h = []
                    try:
                        cutoff_time = message.created_at - datetime.timedelta(minutes=30)
                        async for msg in message.channel.history(limit=8, before=message):
                            if msg.created_at < cutoff_time: break
                            if msg.content.startswith('!'): continue
                            role = "assistant" if msg.author.id == self.bot.user.id else "user"

                            content = msg.clean_content.replace(f"@{self.bot.user.name}", "").strip()
                            if message.guild and message.guild.me:
                                content = content.replace(f"@{message.guild.me.display_name}", "").strip()

                            if content:
                                h.append({"role": role, "content": f"User {msg.author.display_name} said: {content}" if role == "user" else content})
                        h.reverse()
                    except Exception as e:
                        print(f"[ERROR] Failed getting working memory from Discord: {e}")
                    return h

                async def get_rag_memory():
                    try:
                        return await asyncio.to_thread(database.search_memories, cleaned_content, server_id, 5)
                    except Exception as e:
                        print(f"[MEMORY] search_memories failed: {e}")
                        return ""

                conversation_history, semantic_memory = await asyncio.gather(get_chat_history(), get_rag_memory())
                if semantic_memory:
                    print(f"[MEMORY] RAG returned {len(semantic_memory)} chars of context")

                # Assemble the final prompt
                system_prompt = f"System: {personality}"
                server_info_prompt = ""
                
                if image_description:
                    if cleaned_content:
                        user_info_prompt = f"User {message.author.display_name} said: {cleaned_content}\n[Attachment (Image Content / Description): {image_description}]"
                    else:
                        user_info_prompt = f"User {message.author.display_name} sent an image.\n[Attachment (Image Content / Description): {image_description}]"
                else:
                    user_info_prompt = f"User {message.author.display_name} said: {cleaned_content}"

                if message.guild:
                    server_info_prompt = f"Server Info: Name: {message.guild.name}, ID: {message.guild.id}, Member Count: {message.guild.member_count}"

                rag_block = f"== Relevant Past Context ==\n{semantic_memory}" if semantic_memory else ""

                capability_block = (
                    "== Your Capabilities (v3.0) ==\n"
                    "- You exclusively support Slash Commands (/).\n"
                    "- You can generate AI Images, Videos, and Music via specialized dashboards.\n"
                    "- You have a DJ system for playing YouTube music/playlists in voice channels.\n"
                    "- You have real-time Web Research access. If users ask you to 'look up' or 'search' for something, you use a dedicated search tool.\n"
                    "- You have long-term memory and remember past conversations via RAG."
                )

                system_parts = [
                    system_prompt,
                    server_info_prompt,
                    rag_block,
                    capability_block,
                    "You are PETEY. Only respond to the last User message. Do NOT generate a response on behalf of the user.",
                    "If you have already greeted the user in the Working Memory, do NOT greet them again. Just continue the conversation naturally.",
                    "CRITICAL: Never address yourself, do not prefix your messages with your own name, and do not say 'Yo @PETEY'. You ARE PETEY. Talk directly to the user.",
                    "If you want to send a GIF or meme, include the exact text [GIF: <search query>] anywhere in your response."
                ]
                final_system_message = "\n".join(filter(None, system_parts))

                final_user_prompt = user_info_prompt + "\nRespond as Petey:"

                try:
                    print(f"[DEBUG] About to call LLM provider...", flush=True)
                    response_text = await asyncio.to_thread(gemini_api.call_gemini, final_user_prompt, system_message=final_system_message, history=conversation_history)
                    print(f"[DEBUG] LLM returned {len(response_text)} chars", flush=True)

                    # Strip leaked model control tokens before sending
                    response_text = re.sub(r'<\|[^|>]+\|>', '', response_text)
                    response_text = re.sub(r'<\|start\|>.*', '', response_text, flags=re.DOTALL)
                    response_text = re.sub(r'<\|channel\|>.*?(?=\n|$)', '', response_text)
                    response_text = re.sub(r'<\|constrain\|>[\d\s.]+', '', response_text)
                    response_text = response_text.strip()

                    # ---- GIF Extraction Logic ----
                    gif_query = None
                    gif_match = re.search(r'\[GIF:\s*(.*?)\]', response_text, re.IGNORECASE)
                    if gif_match:
                        gif_query = gif_match.group(1).strip()
                        response_text = re.sub(r'\[GIF:\s*.*?\]', '', response_text, flags=re.IGNORECASE).strip()

                    gif_url = None
                    if gif_query:
                        try:
                            async with aiohttp.ClientSession() as session:
                                url = f"https://api.giphy.com/v1/gifs/random?api_key=dc6zaTOxFJmzC&tag={gif_query}&rating=r"
                                async with session.get(url) as resp:
                                    if resp.status == 200:
                                        data = await resp.json()
                                        gif_url = data.get("data", {}).get("images", {}).get("original", {}).get("url")
                        except Exception as e:
                            print(f"[DEBUG] Failed to fetch Giphy for query {gif_query}: {e}")

                    # Send response
                    if len(response_text) > 2000:
                        await utils.send_long_message(message.channel, response_text)
                    elif len(response_text) > 0:
                        await message.channel.send(response_text)
                    elif not gif_url:
                        print(f"[WARNING] LLM returned empty response for prompt: {final_user_prompt}")
                        await message.channel.send("*(I don't have anything to say right now, my conversational engine drew a blank!)*")

                    if gif_url:
                        await message.channel.send(gif_url)

                except Exception as e:
                    print(f"[ERROR] Failed to get LLM response or send message: {e}")
                    traceback.print_exc()
                    await message.channel.send("Sorry, I had trouble processing that.")

            return

        # --- Fallback/Other Processing ---
        if message.guild and not is_mention:
            asyncio.create_task(utils.add_reaction_if_interesting(message))

    async def cog_load(self):
        # Start the proactive idle channel loop when the cog is loaded
        self.bot.loop.create_task(self.proactive_idle_channel_loop())

    async def proactive_idle_channel_loop(self):
        await self.bot.wait_until_ready()
        print("[PROACTIVE] Background idle channel loop started.")
        while not self.bot.is_closed():
            try:
                await self.check_and_start_conversations()
            except Exception as e:
                print(f"[PROACTIVE-LOOP] Error: {e}")
                traceback.print_exc()
            await asyncio.sleep(60) # Check for idleness every 60 seconds

    async def check_and_start_conversations(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        for guild in self.bot.guilds:
            server_id = str(guild.id)
            try:
                conf = config.load_server_config(server_id)
            except Exception:
                continue

            personas = conf.get("personas")
            if not personas:
                continue

            # Check channels in this guild
            for channel in guild.text_channels:
                # Check bot permissions to write
                if not channel.permissions_for(guild.me).send_messages:
                    continue

                persona_obj = config.get_persona_object_for_channel(conf, channel.id)
                proactive_pct = persona_obj.get("proactive_pct", 0)
                if not proactive_pct or proactive_pct <= 0:
                    continue

                # Enforce Channel Cooldown
                cooldown_min = conf.get("global_settings", {}).get("proactive_cooldown_minutes", 15)
                last_time = self._last_proactive_time.get(channel.id)
                if last_time:
                    elapsed = (now - last_time).total_seconds() / 60.0
                    if elapsed < cooldown_min:
                        continue

                # Check last message in channel to see if it is idle
                try:
                    last_msg = None
                    async for msg in channel.history(limit=1):
                        last_msg = msg
                        break
                    
                    if last_msg:
                        # If the last message was sent by the bot, don't spam
                        if last_msg.author.id == self.bot.user.id:
                            continue
                        
                        # Idle threshold = cooldown period
                        elapsed_idle = (now - last_msg.created_at).total_seconds() / 60.0
                        if elapsed_idle < cooldown_min:
                            continue
                except Exception as e:
                    print(f"[PROACTIVE-IDLE] Failed checking history for channel {channel.name}: {e}")
                    continue

                # Hourly limit check
                if server_id not in self._proactive_hourly_counts:
                    self._proactive_hourly_counts[server_id] = []
                self._proactive_hourly_counts[server_id] = [
                    t for t in self._proactive_hourly_counts[server_id]
                    if (now - t).total_seconds() < 3600
                ]
                max_per_hour = conf.get("global_settings", {}).get("proactive_max_per_hour", 6)
                if len(self._proactive_hourly_counts[server_id]) >= max_per_hour:
                    continue

                # Roll the dice!
                roll = random.randint(1, 100)
                if roll > proactive_pct:
                    continue

                # All checks pass! Let's generate a conversation starter
                print(f"[PROACTIVE-IDLE] Triggering starter in #{channel.name} (roll: {roll} <= {proactive_pct}%)")
                self._last_proactive_time[channel.id] = now
                self._proactive_hourly_counts[server_id].append(now)

                # Trigger response generation in background
                asyncio.create_task(self.send_proactive_conversation_starter(channel, persona_obj, conf))

    async def send_proactive_conversation_starter(self, channel, persona_obj, conf):
        try:
            # Let's fetch the recent history to help the LLM know what was talked about
            history = []
            try:
                cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
                async for msg in channel.history(limit=5):
                    if msg.created_at < cutoff_time: break
                    if msg.content.startswith('!'): continue
                    role = "assistant" if msg.author.id == self.bot.user.id else "user"
                    history.append({"role": role, "content": f"User {msg.author.display_name} said: {msg.clean_content}" if role == "user" else msg.clean_content})
                history.reverse()
            except Exception as e:
                print(f"[PROACTIVE-IDLE] Failed fetching history for prompt context: {e}")

            system_prompt = f"System: {persona_obj['system_prompt']}"
            server_info_prompt = f"Server Info: Name: {channel.guild.name}, ID: {channel.guild.id}"
            
            system_parts = [
                system_prompt,
                server_info_prompt,
                "You are PETEY. The channel has been quiet for a while.",
                "Generate a short, engaging, and in-character message to start a new, interesting topic of conversation.",
                "Do NOT explicitly mention that the channel was quiet or that you are starting a conversation.",
                "Just drop a creative question, share an interesting thought, or bring up a topic matching the channel's purpose.",
                "CRITICAL: Never address yourself, do not prefix your messages with your own name, and do not say 'Yo @PETEY'. Talk directly to the channel."
            ]
            final_system_message = "\n".join(filter(None, system_parts))
            final_user_prompt = "Start an interesting conversation topic in character:"

            async with channel.typing():
                response_text = await asyncio.to_thread(
                    gemini_api.call_gemini,
                    final_user_prompt,
                    system_message=final_system_message,
                    history=history
                )
                
                # Clean up response
                response_text = re.sub(r'<\|[^|>]+\|>', '', response_text)
                response_text = re.sub(r'<\|start\|>.*', '', response_text, flags=re.DOTALL)
                response_text = response_text.strip()

                if len(response_text) > 0:
                    if len(response_text) > 2000:
                        await utils.send_long_message(channel, response_text)
                    else:
                        await channel.send(response_text)
        except Exception as e:
            print(f"[PROACTIVE-IDLE] Failed sending starter: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(ChatCog(bot))

