import asyncio
import discord
from discord import app_commands
from discord.ext import commands
from collections import defaultdict
from petey import config, database, deapi_client, gemini_api, utils
import traceback
import io
import aiohttp

# ── Per-user generation throttle (max 3 concurrent jobs) ──
MAX_CONCURRENT_GENS = 3
_user_gen_slots = defaultdict(int)  # user_id -> active generation count


async def _download_and_send(ctx, url, filename, text_content, message_to_edit=None):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) < 25 * 1024 * 1024:
                        if message_to_edit:
                            try:
                                await message_to_edit.edit(content=text_content, embed=None, view=None, attachments=[discord.File(io.BytesIO(data), filename=filename)])
                                return
                            except Exception as edit_err:
                                print(f"[ERROR] Failed to edit message with attachment: {edit_err}")
                                # fallback to sending purely
                        await ctx.send(content=text_content, file=discord.File(io.BytesIO(data), filename=filename))
                        return
    except Exception as e:
        print(f"[ERROR] Failed to download {url}: {e}")
    
    if message_to_edit:
        await message_to_edit.edit(content=f"{text_content}\n{url}", embed=None, view=None, attachments=[])
    else:
        await ctx.send(content=f"{text_content}\n{url}")


async def _get_attachment_bytes(ctx, explicit_attachment: discord.Attachment = None):
    """Gets image bytes from an attachment OR auto-loads the most recent stored image."""
    attachment = explicit_attachment
    if not attachment and ctx.message and ctx.message.attachments:
        attachment = ctx.message.attachments[0]
        
    if attachment:
        if not attachment.content_type or not attachment.content_type.startswith("image/"):
            await ctx.send("❌ The attachment must be an image file.")
            return None
        raw_bytes = await attachment.read()
        if len(raw_bytes) > 9 * 1024 * 1024:
            try:
                from PIL import Image
            except ImportError:
                await ctx.send("❌ Image is too large (over 9MB). Please downsize your image or ask Server Admin to `pip install Pillow` for auto-compression.")
                return None
            try:
                img = Image.open(io.BytesIO(raw_bytes))
                if img.mode != "RGB":
                    img = img.convert("RGB")
                out_bytes = io.BytesIO()
                img.save(out_bytes, format="JPEG", quality=80)
                raw_bytes = out_bytes.getvalue()
            except Exception as e:
                print(f"[ERROR] Failed to compress large image upload: {e}")
                
        database.save_recent_image(ctx.author.id, raw_bytes)
        return raw_bytes
        
    # No active attachment - try to load from history
    recent = database.get_recent_image(ctx.author.id)
    if recent:
        return recent
        
    await ctx.send("❌ You need to attach an image file. Once you do, it will be saved for automatic reuse next time!")
    return None


async def _get_video_attachment_bytes(ctx, explicit_attachment: discord.Attachment = None):
    """Gets video bytes from an explicit attachment param (slash) or ctx.message.attachments (prefix)."""
    attachment = explicit_attachment
    if not attachment:
        if not ctx.message or not ctx.message.attachments:
            await ctx.send("❌ You need to attach a video file to use this command!")
            return None
        attachment = ctx.message.attachments[0]
    if not attachment.filename.lower().endswith(('.mp4', '.mov', '.webm', '.avi')):
        await ctx.send("❌ The attachment must be a video file (.mp4, .mov, etc).")
        return None
    return await attachment.read()


# ═══════════════════════════════════════════════════════════════════
#  Shared Modals & Selects
# ═══════════════════════════════════════════════════════════════════



class ImageStyleModal(discord.ui.Modal, title='Edit Visual Prompt'):
    prompt_input = discord.ui.TextInput(
        label='Image Subject & Style',
        style=discord.TextStyle.paragraph,
        placeholder='e.g. A cyberpunk city scene at night, neon lights...',
        required=True,
        max_length=1500
    )
    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.prompt_input.default = view.state['prompt']

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.state['prompt'] = self.prompt_input.value
        await self.view_ref.update_embed(interaction)


class ImageAspectRatioSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Square (1:1)', value='1024x1024', emoji='🟩', description='Best for general art and avatars'),
            discord.SelectOption(label='Landscape (16:9)', value='1024x576', emoji='🖥️', description='Best for desktop/YouTube'),
            discord.SelectOption(label='Portrait (9:16)', value='576x1024', emoji='📱', description='Best for mobile/TikTok/Reels')
        ]
        super().__init__(placeholder='Select Image Aspect Ratio...', min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        w, h = map(int, self.values[0].split('x'))
        self.view.state['width'] = w
        self.view.state['height'] = h
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await self.view.update_embed(interaction)


# ═══════════════════════════════════════════════════════════════════
#  Image Generation Views
# ═══════════════════════════════════════════════════════════════════

class GenImgSetupView(discord.ui.View):
    def __init__(self, ctx, initial_prompt):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.state = {
            'prompt': initial_prompt,
            'width': 1024,
            'height': 1024
        }
        self.message = None
        self.add_item(ImageAspectRatioSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message("❌ This dashboard is locked to the original caller.", ephemeral=True)
        return False

    def build_embed(self):
        emb = discord.Embed(title="🖼️ Image Generation Dashboard", color=discord.Color.brand_green())
        emb.add_field(name="🎨 Prompt", value=f"```\n{self.state['prompt'][:800]}\n```", inline=False)
        emb.add_field(name="📐 Resolution", value=f"`{self.state['width']}x{self.state['height']}`", inline=True)
        return emb

    async def update_embed(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Edit Prompt", style=discord.ButtonStyle.secondary, emoji="🎨", row=0)
    async def edit_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImageStyleModal(self))

    @discord.ui.button(label="✨ Enhance Prompt (AI)", style=discord.ButtonStyle.primary, emoji="🪄", row=0)
    async def auto_enhance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(title="🖼️ Image Generation Dashboard", description="🧠 *Hold on while I rewrite your prompt...*", color=discord.Color.brand_green()), view=self)
        try:
            self.state['prompt'] = await asyncio.to_thread(gemini_api.call_gemini, self.state['prompt'], "Rewrite this to be a highly detailed, vivid, professional text-to-image prompt. Output ONLY the raw prompt.")
        except Exception: pass
        await self.message.edit(embed=self.build_embed(), view=self)

    @discord.ui.button(label="🚀 Generate Image!", style=discord.ButtonStyle.success, row=2)
    async def generate_img(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        button.label = "Synthesizing..."
        await interaction.response.edit_message(view=self)
        asyncio.create_task(self.run_generation())

    async def run_generation(self):
        ctx, st, uid = self.ctx, self.state, self.ctx.author.id
        try:
            await self.message.edit(embed=self.build_embed(), content="🖼️ *Rendering Image...*")
            result = await deapi_client.deapi.generate_image(
                st['prompt'], width=st['width'], height=st['height'], guild_id=(ctx.guild.id if ctx.guild else None)
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} Here is your image for: `{st['prompt'][:1000]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_image.png", content, message_to_edit=self.message)
        except Exception as e:
            await self.message.edit(content=f"❌ Error in generation: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)


class Img2ImgSetupView(discord.ui.View):
    def __init__(self, ctx, initial_prompt, image_bytes):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.state = {
            'prompt': initial_prompt,
            'width': 1024,
            'height': 1024,
            'image_bytes': image_bytes
        }
        self.message = None
        self.add_item(ImageAspectRatioSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message("❌ This dashboard is locked to the original caller.", ephemeral=True)
        return False

    def build_embed(self):
        emb = discord.Embed(title="🖼️ Img2Img Dashboard", color=discord.Color.orange())
        emb.add_field(name="🎨 Prompt", value=f"```\n{self.state['prompt'][:800]}\n```", inline=False)
        emb.add_field(name="📐 Resolution", value=f"`{self.state['width']}x{self.state['height']}`", inline=True)
        emb.add_field(name="📸 Source Image", value="`Attached` ✅", inline=True)
        return emb

    async def update_embed(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Edit Prompt", style=discord.ButtonStyle.secondary, emoji="🎨", row=0)
    async def edit_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImageStyleModal(self))

    @discord.ui.button(label="🚀 Generate Image!", style=discord.ButtonStyle.success, row=2)
    async def generate_img(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        button.label = "Synthesizing..."
        await interaction.response.edit_message(view=self)
        asyncio.create_task(self.run_generation())

    async def run_generation(self):
        ctx, st, uid = self.ctx, self.state, self.ctx.author.id
        try:
            await self.message.edit(embed=self.build_embed(), content="🖼️ *Transmitting to DEAPI... Restyling Image...*")
            result = await deapi_client.deapi.generate_image_to_image(
                st['prompt'], st['image_bytes'], width=st['width'], height=st['height'], guild_id=(ctx.guild.id if ctx.guild else None)
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} Here is your restyled masterpiece for: `{st['prompt'][:1000]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_img2img.png", content, message_to_edit=self.message)
        except Exception as e:
            await self.message.edit(content=f"❌ Error in img2img generation: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)


# ═══════════════════════════════════════════════════════════════════
#  Video Generation Views
# ═══════════════════════════════════════════════════════════════════

class VideoConfigModal(discord.ui.Modal, title='Video Frames'):
    frames_input = discord.ui.TextInput(label='Frames', style=discord.TextStyle.short, default='241', max_length=4)

    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.frames_input.default = str(view.state.get('frames', 241))

    async def on_submit(self, interaction: discord.Interaction):
        try:
            self.view_ref.state['frames'] = max(24, min(241, int(self.frames_input.value)))
            await self.view_ref.update_embed(interaction)
        except ValueError:
            await interaction.response.send_message("❌ Invalid numbers.", ephemeral=True)


class VideoAspectRatioSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label='Square (1:1)', value='512x512', emoji='🟩', description='Standard Default Video'),
            discord.SelectOption(label='Landscape (16:9)', value='1024x576', emoji='🖥️', description='Cinematic Widescreen'),
            discord.SelectOption(label='Portrait (9:16)', value='576x1024', emoji='📱', description='TikTok / Shorts Format')
        ]
        super().__init__(placeholder='Select Video Aspect Ratio...', min_values=1, max_values=1, options=options, row=1)

    async def callback(self, interaction: discord.Interaction):
        w, h = map(int, self.values[0].split('x'))
        self.view.state['width'] = w
        self.view.state['height'] = h
        for opt in self.options:
            opt.default = (opt.value == self.values[0])
        await self.view.update_embed(interaction)


class GenVidSetupView(discord.ui.View):
    def __init__(self, ctx, initial_prompt):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.state = {
            'prompt': initial_prompt,
            'width': 512,
            'height': 512,
            'frames': 241,
            'fps': 24
        }
        self.message = None
        self.add_item(VideoAspectRatioSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message("❌ This dashboard is locked to the original caller.", ephemeral=True)
        return False

    def build_embed(self):
        emb = discord.Embed(title="🎥 Video Generation Dashboard", color=discord.Color.red())
        emb.add_field(name="🎨 Prompt", value=f"```\n{self.state['prompt'][:800]}\n```", inline=False)
        emb.add_field(name="📐 Settings", value=f"`{self.state['width']}x{self.state['height']}` | `{self.state['frames']} frames` @ `{self.state['fps']} FPS`", inline=True)
        return emb

    async def update_embed(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Edit Prompt", style=discord.ButtonStyle.secondary, emoji="🎨", row=0)
    async def edit_prompt(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ImageStyleModal(self))

    @discord.ui.button(label="Edit Frames", style=discord.ButtonStyle.secondary, emoji="⚙️", row=0)
    async def edit_res(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(VideoConfigModal(self))

    @discord.ui.button(label="✨ Enhance Prompt (AI)", style=discord.ButtonStyle.primary, emoji="🪄", row=0)
    async def auto_enhance(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=discord.Embed(title="🎥 Video Generation Dashboard", description="🧠 *Please wait while I enhance your prompt...*", color=discord.Color.red()), view=self)
        try:
            self.state['prompt'] = await asyncio.to_thread(gemini_api.call_gemini, self.state['prompt'], "Rewrite this to be a highly detailed, vivid, professional text-to-video prompt. Output ONLY the raw prompt.")
        except Exception: pass
        await self.message.edit(embed=self.build_embed(), view=self)

    @discord.ui.button(label="🚀 Synthesize Video!", style=discord.ButtonStyle.success, row=2)
    async def generate_vid(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children: child.disabled = True
        button.label = "Synthesizing..."
        await interaction.response.edit_message(view=self)
        asyncio.create_task(self.run_generation())

    async def run_generation(self):
        ctx, st, uid = self.ctx, self.state, self.ctx.author.id
        try:
            await self.message.edit(embed=self.build_embed(), content="🎥 *Rendering Video (This takes a while)...*")
            result = await deapi_client.deapi.generate_video(
                st['prompt'], width=st['width'], height=st['height'], frames=st['frames'], fps=st['fps'], guild_id=(ctx.guild.id if ctx.guild else None)
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} Here is your video for: `{st['prompt'][:1000]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_video.mp4", content, message_to_edit=self.message)
        except Exception as e:
            await self.message.edit(content=f"❌ Error in video generation: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)


class Img2VidSetupView(GenVidSetupView):
    def __init__(self, ctx, initial_prompt, image_bytes):
        super().__init__(ctx, initial_prompt)
        self.state['image_bytes'] = image_bytes

    def build_embed(self):
        emb = super().build_embed()
        emb.title = "🎥 Img2Vid Dashboard"
        emb.add_field(name="📸 Starting Frame", value="`Attached` ✅", inline=True)
        return emb

    async def run_generation(self):
        ctx, st, uid = self.ctx, self.state, self.ctx.author.id
        try:
            await self.message.edit(embed=self.build_embed(), content="🎥 *Animating Image (This takes a while)...*")
            result = await deapi_client.deapi.generate_image_to_video(
                st['prompt'], st['image_bytes'], width=st['width'], height=st['height'], frames=st['frames'], fps=st['fps'], guild_id=(ctx.guild.id if ctx.guild else None)
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} Here is your video for: `{st['prompt'][:1000]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_img2vid.mp4", content, message_to_edit=self.message)
        except Exception as e:
            await self.message.edit(content=f"❌ Error in img2vid: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)


class Vid2VidSetupView(GenVidSetupView):
    def __init__(self, ctx, initial_prompt, video_bytes):
        super().__init__(ctx, initial_prompt)
        self.state['video_bytes'] = video_bytes

    def build_embed(self):
        emb = super().build_embed()
        emb.title = "🎬 Vid2Vid (Replace) Dashboard"
        emb.color = discord.Color.purple()
        emb.add_field(name="🎞️ Source Video", value="`Attached` ✅", inline=True)
        return emb

    async def run_generation(self):
        ctx, st, uid = self.ctx, self.state, self.ctx.author.id
        try:
            await self.message.edit(embed=self.build_embed(), content="🎬 *Transmitting to DEAPI... Restyling Video (This takes a while)...*")
            result = await deapi_client.deapi.generate_video_to_video(
                st['prompt'], st['video_bytes'], width=st['width'], height=st['height'], frames=st['frames'], fps=st['fps'], guild_id=(ctx.guild.id if ctx.guild else None)
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} Here is your restyled video masterpiece for: `{st['prompt'][:1000]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_vid2vid.mp4", content, message_to_edit=self.message)
        except Exception as e:
            await self.message.edit(content=f"❌ Error in vid2vid: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)


# ═══════════════════════════════════════════════════════════════════
#  Music Generation Views
# ═══════════════════════════════════════════════════════════════════

class MusicStyleModal(discord.ui.Modal, title='Edit Style'):
    style_input = discord.ui.TextInput(
        label='Music Style & Vibe',
        style=discord.TextStyle.paragraph,
        placeholder='e.g. Energetic pop-electronic anthem with punchy synths...',
        required=True,
        max_length=500
    )
    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.style_input.default = view.state['style']

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.state['style'] = self.style_input.value
        await self.view_ref.update_embed(interaction)


class MusicLyricsModal(discord.ui.Modal, title='Edit Lyrics'):
    lyrics_input = discord.ui.TextInput(
        label='Vocals (Type ++ for AI Auto-Lyrics)',
        style=discord.TextStyle.paragraph,
        placeholder='[Instrumental]\n\nOR type lyrics here\nOR type ++ to let Gemini write them.',
        required=True,
        max_length=2000
    )
    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.lyrics_input.default = view.state['lyrics']

    async def on_submit(self, interaction: discord.Interaction):
        self.view_ref.state['lyrics'] = self.lyrics_input.value
        await self.view_ref.update_embed(interaction)


class MusicDurationModal(discord.ui.Modal, title='Set Duration (Seconds)'):
    duration_input = discord.ui.TextInput(
        label='Seconds (30 - 300)',
        style=discord.TextStyle.short,
        placeholder='30',
        required=True,
        max_length=3
    )
    def __init__(self, view):
        super().__init__()
        self.view_ref = view
        self.duration_input.default = str(view.state['duration'])

    async def on_submit(self, interaction: discord.Interaction):
        try:
            dur = int(self.duration_input.value.strip())
            self.view_ref.state['duration'] = max(30, min(300, dur))
            await self.view_ref.update_embed(interaction)
        except ValueError:
            await interaction.response.send_message("❌ Invalid duration. Must be an integer.", ephemeral=True)


class MusicSetupView(discord.ui.View):
    def __init__(self, ctx, initial_prompt, ref_audio_bytes):
        super().__init__(timeout=600)
        self.ctx = ctx
        self.state = {
            'style': initial_prompt,
            'lyrics': '[Instrumental]',
            'duration': 30,
            'reference_audio': ref_audio_bytes
        }
        self.message = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user == self.ctx.author:
            return True
        await interaction.response.send_message("❌ This dashboard is locked to the original caller.", ephemeral=True)
        return False

    def build_embed(self):
        emb = discord.Embed(title="🎵 Music Generation Dashboard", color=discord.Color.blurple())
        emb.add_field(name="🎼 Style", value=f"```\n{self.state['style'][:500]}\n```", inline=False)
        emb.add_field(name="🎤 Lyrics", value=f"```\n{self.state['lyrics'][:1000]}\n```", inline=False)
        emb.add_field(name="⏱️ Duration", value=f"`{self.state['duration']}s`", inline=True)
        emb.add_field(name="🎧 Reference Audio", value="`Attached` ✅" if self.state['reference_audio'] else "`None` ❌", inline=True)
        return emb

    async def update_embed(self, interaction: discord.Interaction):
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Edit Style", style=discord.ButtonStyle.secondary, emoji="🎼", row=0)
    async def edit_style(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MusicStyleModal(self))

    @discord.ui.button(label="Edit Lyrics", style=discord.ButtonStyle.secondary, emoji="🎤", row=0)
    async def edit_lyrics(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MusicLyricsModal(self))

    @discord.ui.button(label="Set Duration", style=discord.ButtonStyle.secondary, emoji="⏱️", row=0)
    async def set_duration(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MusicDurationModal(self))

    @discord.ui.button(label="🚀 Generate!", style=discord.ButtonStyle.success, row=1)
    async def generate_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Lock UI
        for child in self.children:
            child.disabled = True
        button.label = "Synthesizing..."
        await interaction.response.edit_message(view=self)

        # Fire background task so we don't block interaction timeout
        asyncio.create_task(self.run_generation())

    async def run_generation(self):
        ctx = self.ctx
        st = self.state
        uid = ctx.author.id

        # Auto-Lyrics pipeline
        final_lyrics = st['lyrics']
        if final_lyrics.strip() == "++":
            try:
                await self.message.edit(content="🧠 *Gemini is writing the perfect lyrics...*")
                prompt_for_llm = f"Write 4 to 8 lines of vivid song lyrics perfectly matching this musical style setting: '{st['style']}'. DO NOT write any intro, titles, or formatting, JUST output the raw lyrical text directly. Keep it short and impactful."
                final_lyrics = await asyncio.to_thread(gemini_api.call_gemini, prompt_for_llm)
                st['lyrics'] = final_lyrics.strip()
            except Exception as e:
                print(f"[ERROR] Gemini lyrics failed: {e}")
                final_lyrics = "[Instrumental]"

        guild_id = ctx.guild.id if ctx.guild else None

        try:
            await self.message.edit(embed=self.build_embed(), content="🎵 *Transmitting to DEAPI... Synthesis initializing...*")
            result = await deapi_client.deapi.generate_music(
                caption=st['style'],
                lyrics=final_lyrics,
                duration=st['duration'],
                reference_audio=st['reference_audio'],
                guild_id=guild_id
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} Here is your `{st['duration']}s` track for: `{st['style'][:1000]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_music.mp3", content, message_to_edit=self.message)
        except Exception as e:
            await self.message.edit(content=f"❌ Error in generation: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)


# ═══════════════════════════════════════════════════════════════════
#  Media Cog
# ═══════════════════════════════════════════════════════════════════

class MediaCog(commands.Cog, name="Media"):
    """AI media generation commands: images, videos, and music."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ── Image Generation ──────────────────────────────────────────

    @commands.hybrid_command(name="genimg", description="Spawns an interactive Image Generation Dashboard.")
    @app_commands.describe(prompt="The image prompt to generate")
    async def genimg(self, ctx: commands.Context, *, prompt: str = "A beautiful surreal landscape"):
        """Spawns an interactive Image Generation Dashboard."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
        _user_gen_slots[uid] += 1

        view = GenImgSetupView(ctx, prompt)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="img2img", description="Spawns an interactive Img2Img Dashboard.")
    @app_commands.describe(prompt="The restyle prompt", image="The image to restyle")
    async def img2img(self, ctx: commands.Context, image: discord.Attachment = None, *, prompt: str = "A highly detailed surreal masterpiece"):
        """Spawns an interactive Img2Img Dashboard."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        
        image_bytes = await _get_attachment_bytes(ctx, image)
        if not image_bytes: return

        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
            
        _user_gen_slots[uid] += 1

        view = Img2ImgSetupView(ctx, prompt, image_bytes)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="removebg", description="Strips the background from an attached image.")
    @app_commands.describe(image="The image to remove the background from")
    async def removebg(self, ctx: commands.Context, image: discord.Attachment = None):
        """Immediately strips the background from an attached image."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        
        image_bytes = await _get_attachment_bytes(ctx, image)
        if not image_bytes: return

        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
            
        _user_gen_slots[uid] += 1

        msg = await ctx.send("✂️ *Extracting subject and removing background...*")
        try:
            result = await deapi_client.deapi.generate_remove_bg(image_bytes, guild_id=(ctx.guild.id if ctx.guild else None))
            if result and result.get("result_url"):
                await _download_and_send(ctx, result.get("result_url"), "petey_nobg.png", f"{ctx.author.mention} Background ripped successfully:", message_to_edit=msg)
        except Exception as e:
            await msg.edit(content=f"❌ Error removing background: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)

    @commands.hybrid_command(name="upscale", description="Upscales and enhances an attached low-resolution image.")
    @app_commands.describe(image="The image to upscale")
    async def upscale(self, ctx: commands.Context, image: discord.Attachment = None):
        """Immediately upscales and enhances an attached image using AI."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
        image_bytes = await _get_attachment_bytes(ctx, image)
        if not image_bytes: return
        _user_gen_slots[uid] += 1

        msg = await ctx.send("🔎 *Transmitting to DEAPI... AI Upscaling image...*")
        try:
            result = await deapi_client.deapi.generate_upscale(image_bytes, guild_id=(ctx.guild.id if ctx.guild else None))
            if result and result.get("result_url"):
                await _download_and_send(ctx, result.get("result_url"), "petey_upscale.png", f"{ctx.author.mention} Image successfully upscaled:", message_to_edit=msg)
        except Exception as e:
            await msg.edit(content=f"❌ Error upscaling image: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)

    # ── Text-to-Speech ─────────────────────────────────────────────

    @commands.hybrid_command(name="speak", description="Converts text into AI-generated speech audio.")
    @app_commands.describe(
        text="The text to convert to speech",
        voice="Voice to use (default: af_sky)",
        speed="Speech speed multiplier (0.5-2.0, default: 1.0)"
    )
    @app_commands.choices(voice=[
        app_commands.Choice(name="Sky (Female, US)", value="af_sky"),
        app_commands.Choice(name="Bella (Female, US)", value="af_bella"),
        app_commands.Choice(name="Nicole (Female, US)", value="af_nicole"),
        app_commands.Choice(name="Sarah (Female, US)", value="af_sarah"),
        app_commands.Choice(name="Adam (Male, US)", value="am_adam"),
        app_commands.Choice(name="Michael (Male, US)", value="am_michael"),
        app_commands.Choice(name="Emma (Female, UK)", value="bf_emma"),
        app_commands.Choice(name="George (Male, UK)", value="bm_george"),
    ])
    async def speak(self, ctx: commands.Context, text: str, voice: str = "af_sky", speed: float = 1.0):
        """Converts text to AI speech. Pick a voice and speed."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
        if not text or len(text.strip()) < 2:
            await ctx.send("❌ Please provide some text to convert to speech!")
            return
        if len(text) > 5000:
            await ctx.send("❌ Text is too long! Please keep it under 5000 characters.")
            return

        speed = max(0.5, min(2.0, speed))
        _user_gen_slots[uid] += 1

        msg = await ctx.send(f"🗣️ *Generating speech with voice `{voice}` at `{speed}x` speed...*")
        try:
            result = await deapi_client.deapi.generate_speech(
                text, voice=voice, speed=speed,
                guild_id=(ctx.guild.id if ctx.guild else None)
            )
            if result and result.get("result_url"):
                content = f"{ctx.author.mention} 🔊 Here is your TTS audio for: `{text[:200]}`"
                await _download_and_send(ctx, result.get("result_url"), "petey_tts.mp3", content, message_to_edit=msg)
        except Exception as e:
            await msg.edit(content=f"❌ Error generating speech: {e}")
        finally:
            _user_gen_slots[uid] = max(0, _user_gen_slots[uid] - 1)

    # ── Video Generation ──────────────────────────────────────────

    @commands.hybrid_command(name="genvid", description="Spawns an interactive Video Generation Dashboard.")
    @app_commands.describe(prompt="The video prompt")
    async def genvid(self, ctx: commands.Context, *, prompt: str = "A cinematic sweeping shot"):
        """Spawns an interactive Video Generation Dashboard."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
        _user_gen_slots[uid] += 1
        view = GenVidSetupView(ctx, prompt)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="img2vid", description="Spawns an interactive Img2Vid Dashboard.")
    @app_commands.describe(prompt="The animation prompt", image="The image to animate")
    async def img2vid(self, ctx: commands.Context, image: discord.Attachment = None, *, prompt: str = "Animate this image perfectly"):
        """Spawns an interactive Img2Vid Dashboard."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        
        image_bytes = await _get_attachment_bytes(ctx, image)
        if not image_bytes: return

        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
            
        _user_gen_slots[uid] += 1
        view = Img2VidSetupView(ctx, prompt, image_bytes)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    @commands.hybrid_command(name="vid2vid", description="Spawns an interactive Vid2Vid Dashboard.")
    @app_commands.describe(prompt="The restyle prompt for the video", video="The video to restyle")
    async def vid2vid(self, ctx: commands.Context, video: discord.Attachment = None, *, prompt: str = "A highly detailed masterpiece restyle"):
        """Spawns an interactive Vid2Vid Dashboard."""
        if not await utils.check_channel_assignment(ctx, "media_generation"): return
        uid = ctx.author.id
        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return
        video_bytes = await _get_video_attachment_bytes(ctx, video)
        if not video_bytes: return
        _user_gen_slots[uid] += 1
        view = Vid2VidSetupView(ctx, prompt, video_bytes)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    # ── Music Generation ──────────────────────────────────────────

    @commands.hybrid_command(name="genmusic", description="Spawns an interactive Music Generation Dashboard.")
    @app_commands.describe(prompt="The music style/vibe prompt", audio="Optional reference audio file")
    async def genmusic(self, ctx: commands.Context, audio: discord.Attachment = None, *, prompt: str = "An energetic electronic pop anthem"):
        """Spawns an interactive Music Generation Dashboard."""
        if not await utils.check_channel_assignment(ctx, "music"): return
        uid = ctx.author.id
        if _user_gen_slots[uid] >= MAX_CONCURRENT_GENS:
            await ctx.send(f"⏳ You already have {MAX_CONCURRENT_GENS} generations running. Wait for one to finish!")
            return

        _user_gen_slots[uid] += 1

        # Parse Attachments for Audio — check explicit param first, then message attachments
        ref_audio_bytes = None
        att = audio  # From slash command parameter
        if not att and ctx.message and ctx.message.attachments:
            att = ctx.message.attachments[0]  # From prefix command
        if att and att.filename.lower().endswith(('.mp3', '.wav', '.ogg', '.m4a', '.flac')):
            try:
                ref_audio_bytes = await att.read()
            except Exception as e:
                print(f"[WEB] Failed to read audio attachment: {e}")

        # Launch View
        view = MusicSetupView(ctx, prompt, ref_audio_bytes)
        view.message = await ctx.send(embed=view.build_embed(), view=view)

    # ── Web Search ──────────────────────────────────────────────

    @commands.hybrid_command(name="search", description="Searches the web and summarizes the results.")
    @app_commands.describe(query="What to search for")
    async def web_search(self, ctx: commands.Context, *, query: str):
        """Searches the web for the given query and summarizes the results."""
        if not await utils.check_channel_assignment(ctx, "research"): return

        # Defer so we don't hit the 3-second interaction timeout on slash commands
        await ctx.defer()
        await ctx.send(f"🔍 Searching the web for: `{query}`...")

        try:
            search_results = await utils.perform_web_search(query)

            conf = config.load_server_config(ctx.guild.id) if ctx.guild else {}
            personality = conf.get("personality", "You are Petey, a helpful assistant with internet access.")
            
            channel_personalities = conf.get("channel_personalities", {})
            channel_id_str = str(ctx.channel.id)
            if channel_id_str in channel_personalities and channel_personalities[channel_id_str].strip():
                personality = channel_personalities[channel_id_str]

            prompt_for_llm = (
                f"System: {personality}\n"
                f"Based on the following internet search results, answer the user's query.\n\n"
                f"User Query: {query}\n\n"
                f"== Internet Search Results ==\n"
                f"{search_results}\n\n"
                f"Petey's Answer:"
            )

            response_text = await asyncio.to_thread(gemini_api.call_gemini, prompt_for_llm)

            if len(response_text) > 2000:
                await utils.send_long_message(ctx.channel, response_text)
            else:
                await ctx.send(response_text)

        except Exception as e:
            error_message = f"❌ Error performing web search: {e}"
            print(f"[ERROR] Error in search command execution: {e}")
            traceback.print_exc()
            await ctx.send(error_message)


async def setup(bot: commands.Bot):
    await bot.add_cog(MediaCog(bot))
