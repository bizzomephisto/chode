import os
import aiohttp
import asyncio
import time
import json
import io
import random
from petey import config

class DeapiError(Exception):
    pass

class RateLimitError(DeapiError):
    pass

class DeapiClient:
    def __init__(self):
        self.api_key = os.getenv("DEAPI_KEY")
        if not self.api_key:
            print("[WARNING] DEAPI_KEY not found in environment variables. deAPI features will not work.")
        
        self.base_url = "https://api.deapi.ai"
        self._session = None
        self._cache = {}
        self._cache_ts = {}
        self.cache_ttl = 86400  # 24 hours

    def _get_headers(self, content_type="application/json"):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json"
        }
        # Only set if explicitly provided, aiohttp handles multipart boundaries automatically
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session is None or getattr(self._session, "closed", True):
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session and not getattr(self._session, "closed", True):
            await self._session.close()

    async def get_models(self, inference_type=None, force_refresh=False):
        cache_key = inference_type or "__all__"
        import time
        now = time.time()

        if (not force_refresh 
            and cache_key in self._cache 
            and now - self._cache_ts.get(cache_key, 0.0) < self.cache_ttl):
            return self._cache[cache_key]

        session = await self.get_session()
        params = {}
        if inference_type:
            params["filter[inference_types]"] = inference_type

        async with session.get(f"{self.base_url}/api/v1/client/models", headers=self._get_headers(), params=params) as resp:
            try:
                resp.raise_for_status()
                data = await resp.json()
                models = data.get("data", [])
                self._cache[cache_key] = models
                self._cache_ts[cache_key] = now
                return models
            except aiohttp.ClientResponseError as e:
                print(f"[ERROR] Failed to fetch models: {e.status}")
                return []

    async def _execute_with_fallback(self, endpoint, payload_builder, inference_type, is_multipart=False, guild_id=None):
        """Try available models until one succeeds."""
        models = await self.get_models(inference_type)
        if not models:
            raise DeapiError(f"No models found for inference type: {inference_type}")

        selected_model_slug = None
        if guild_id:
            try:
                server_cfg = config.load_server_config(guild_id)
                config_key = f"selected_{inference_type}_model"
                selected_model_slug = server_cfg.get(config_key)
            except Exception as e:
                print(f"[ERROR] config load failed for {guild_id}: {e}")

        if selected_model_slug:
            filtered = [m for m in models if m["slug"] == selected_model_slug]
            if filtered:
                models = filtered
            else:
                print(f"[WARNING] Selected model {selected_model_slug} not found. Falling back to defaults.")

        session = await self.get_session()
        last_error = None

        for model in models:
            model_slug = model["slug"]
            defaults = model.get("info", {}).get("defaults", {})
            try:
                # Build payload specific to this model
                payload = payload_builder(model_slug, defaults)
                
                headers = self._get_headers(None if is_multipart else "application/json")

                if is_multipart:
                    async with session.post(f"{self.base_url}{endpoint}", headers=headers, data=payload) as resp:
                         if resp.status == 429:
                              retry_after = int(resp.headers.get("Retry-After", 5))
                              await asyncio.sleep(retry_after)
                              continue
                         if resp.status >= 400:
                              err_text = await resp.text()
                              print(f"[ERROR {resp.status}] {endpoint} Model {model_slug}: {err_text}")
                              raise DeapiError(f"Server rejected request: {err_text}")
                         result = await resp.json()
                         return result.get("data", {}).get("request_id")
                else:
                    async with session.post(f"{self.base_url}{endpoint}", headers=headers, json=payload) as resp:
                         if resp.status == 429:
                              retry_after = int(resp.headers.get("Retry-After", 5))
                              await asyncio.sleep(retry_after)
                              continue
                         if resp.status >= 400:
                              err_text = await resp.text()
                              print(f"[ERROR {resp.status}] {endpoint} Model {model_slug}: {err_text}")
                              raise DeapiError(f"Server rejected request: {err_text}")
                         result = await resp.json()
                         return result.get("data", {}).get("request_id")

            except aiohttp.ClientResponseError as e:
                print(f"[DEBUG] Model {model_slug} failed with status {e.status}")
                last_error = e
                continue
            except Exception as e:
                print(f"[DEBUG] Model {model_slug} failed: {e}")
                last_error = e
                continue

        if last_error:
            raise last_error
        raise DeapiError("All models failed")

    async def wait_for_job(self, request_id, interval=2, timeout=300):
        url = f"{self.base_url}/api/v1/client/request-status/{request_id}"
        session = await self.get_session()
        start = time.time()
        
        while time.time() - start < timeout:
            async with session.get(url, headers=self._get_headers()) as resp:
                resp.raise_for_status()
                json_resp = await resp.json()
                data = json_resp.get("data", {})
                
                status = data.get("status")
                if status == "done":
                    return data
                if status == "error":
                    raise DeapiError(f"Job {request_id} failed: {data}")
                
                progress = data.get("progress", 0)
                if progress > 50:
                    await asyncio.sleep(interval)
                else:
                    await asyncio.sleep(interval * 2)

        raise TimeoutError(f"Job {request_id} timed out after {timeout}s")

    def _get_bytes(self, url_or_bytes):
        return url_or_bytes

    async def enhance_prompt(self, prompt, prompt_type="image"):
        """Enhance a generation prompt using Gemini LLM."""
        print(f"[ENHANCE] Enhancing {prompt_type} prompt via Gemini: '{prompt[:80]}'")
        
        type_guidance = {
            "image": "a text-to-image AI art generator (like Stable Diffusion or Flux)",
            "image2image": "an image-to-image AI transformation model",
            "video": "a text-to-video AI generation model",
            "speech": "a text-to-music AI generation model",
        }
        model_desc = type_guidance.get(prompt_type, "an AI generation model")
        
        system_msg = (
            f"You are an expert prompt engineer for {model_desc}. "
            "The user will give you a basic idea. Rewrite it into a detailed, vivid, professional prompt "
            "that will produce stunning results. Add artistic style, lighting, mood, composition details. "
            "Output ONLY the enhanced prompt text, nothing else — no quotes, no explanation, no markdown. "
            "IMPORTANT: Keep the output under 500 characters."
        )
        
        try:
            import asyncio
            from petey import gemini_api
            enhanced = await asyncio.to_thread(gemini_api.call_gemini, prompt, system_msg)
            
            if enhanced and not enhanced.startswith("Error") and len(enhanced) > len(prompt):
                enhanced = enhanced.strip()[:500]  # hard cap at 500 chars
                print(f"[ENHANCE] Success! Enhanced to: '{enhanced[:120]}'")
                return enhanced
            else:
                print(f"[ENHANCE] Gemini returned unusable result, using original")
                return prompt
        except Exception as e:
            print(f"[ENHANCE ERROR] Gemini enhancement failed: {e}")
            return prompt

    async def generate_image(self, prompt, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            return {
                "prompt": prompt,
                "model": model,
                "width": kwargs.get("width", defaults.get("width", 1024)),
                "height": kwargs.get("height", defaults.get("height", 1024)),
                "steps": kwargs.get("steps", defaults.get("steps", 4)),
                "seed": kwargs.get("seed", random.randint(0, 2**32 - 1)),
                "guidance": kwargs.get("guidance", 3.5)
            }
        
        req_id = await self._execute_with_fallback("/api/v1/client/txt2img", build_payload, "txt2img", guild_id=guild_id)
        return await self.wait_for_job(req_id)

    async def generate_image_to_image(self, prompt, image_bytes, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("prompt", prompt)
            data.add_field("model", model)
            data.add_field("steps", str(kwargs.get("steps", defaults.get("steps", 20))))
            data.add_field("seed", str(random.randint(0, 2**32 - 1)))
            data.add_field("guidance", str(kwargs.get("guidance", defaults.get("guidance", 3.5))))
            data.add_field("width", str(kwargs.get("width", defaults.get("width", 1024))))
            data.add_field("height", str(kwargs.get("height", defaults.get("height", 1024))))
            data.add_field("image", io.BytesIO(image_bytes), filename="source.png", content_type="image/png")
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/img2img", build_payload, "img2img", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id, timeout=600)

    async def generate_video(self, prompt, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            return {
                "prompt": prompt,
                "model": model,
                "width": kwargs.get("width", defaults.get("width", 512)),
                "height": kwargs.get("height", defaults.get("height", 512)),
                "steps": kwargs.get("steps", defaults.get("steps", 20)),
                "frames": kwargs.get("frames", 241),  # ~10 sec @ 24fps (DEAPI max)
                "seed": kwargs.get("seed", random.randint(0, 2**32 - 1)),
                "guidance": kwargs.get("guidance", 3.5),
                "fps": kwargs.get("fps", defaults.get("fps", 24))
            }
        
        req_id = await self._execute_with_fallback("/api/v1/client/txt2video", build_payload, "txt2video", guild_id=guild_id)
        return await self.wait_for_job(req_id, interval=5, timeout=900)  # Longer polling interval for video

    async def generate_image_to_video(self, prompt, image_bytes, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("prompt", prompt)
            data.add_field("model", model)
            data.add_field("steps", str(kwargs.get("steps", defaults.get("steps", 20))))
            data.add_field("frames", str(kwargs.get("frames", 241)))  # ~10 sec @ 24fps (DEAPI max)
            data.add_field("seed", str(random.randint(0, 2**32 - 1)))
            data.add_field("guidance", str(kwargs.get("guidance", defaults.get("guidance", 3.5))))
            data.add_field("fps", str(kwargs.get("fps", defaults.get("fps", 24))))
            data.add_field("width", str(kwargs.get("width", defaults.get("width", 512))))
            data.add_field("height", str(kwargs.get("height", defaults.get("height", 512))))
            data.add_field("first_frame_image", io.BytesIO(image_bytes), filename="start.png", content_type="image/png")
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/img2video", build_payload, "img2video", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id, interval=5, timeout=900)

    async def generate_video_to_video(self, prompt, video_bytes, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("prompt", prompt)
            data.add_field("model", model)
            data.add_field("steps", str(kwargs.get("steps", defaults.get("steps", 20))))
            data.add_field("seed", str(random.randint(0, 2**32 - 1)))
            data.add_field("guidance", str(kwargs.get("guidance", defaults.get("guidance", 3.5))))
            data.add_field("video", io.BytesIO(video_bytes), filename="source.mp4", content_type="video/mp4")
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/vid2video", build_payload, "vid2video", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id, interval=5, timeout=900)

    async def generate_remove_bg(self, image_bytes, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("model", model)
            data.add_field("image", io.BytesIO(image_bytes), filename="source.png", content_type="image/png")
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/img-rmbg", build_payload, "img-rmbg", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id)

    async def generate_upscale(self, image_bytes, scale=2, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("model", model)
            data.add_field("scale", str(scale))
            data.add_field("image", io.BytesIO(image_bytes), filename="source.png", content_type="image/png")
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/img-upscale", build_payload, "img-upscale", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id, timeout=600)

    async def generate_music(self, caption, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("caption", caption)
            data.add_field("model", model)
            # Default to no vocals mode if no lyrics
            data.add_field("lyrics", kwargs.get("lyrics", "[Instrumental]"))
            data.add_field("duration", str(kwargs.get("duration", 30)))
            data.add_field("inference_steps", str(kwargs.get("inference_steps", 8)))
            data.add_field("guidance_scale", str(kwargs.get("guidance_scale", 1)))
            data.add_field("seed", "-1")
            data.add_field("format", "mp3")
            
            ref_audio = kwargs.get("reference_audio")
            if ref_audio:
                data.add_field("reference_audio", io.BytesIO(ref_audio), filename="ref.mp3", content_type="audio/mpeg")
                
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/txt2music", build_payload, "txt2music", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id, interval=4)

    async def generate_speech(self, text, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("text", text)
            data.add_field("model", model)
            data.add_field("lang", kwargs.get("lang", "en-us"))
            data.add_field("speed", str(kwargs.get("speed", 1)))
            data.add_field("format", "mp3")
            data.add_field("sample_rate", "24000")
            data.add_field("mode", kwargs.get("mode", "custom_voice"))
            data.add_field("voice", kwargs.get("voice", "af_sky"))
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/txt2audio", build_payload, "txt2audio", is_multipart=True, guild_id=guild_id)
        return await self.wait_for_job(req_id, interval=2)

    async def image_to_text(self, image_bytes, **kwargs):
        guild_id = kwargs.pop("guild_id", None)
        def build_payload(model, defaults):
            data = aiohttp.FormData()
            data.add_field("model", model)
            data.add_field("image", io.BytesIO(image_bytes), filename="source.png", content_type="image/png")
            return data
            
        req_id = await self._execute_with_fallback("/api/v1/client/img2txt", build_payload, "img2txt", is_multipart=True, guild_id=guild_id)
        job_res = await self.wait_for_job(req_id)
        print(f"[IMAGE-TO-TEXT DEBUG] Raw deAPI response: {job_res}")
        if job_res:
            res_val = job_res.get("result")
            if not res_val and job_res.get("result_url"):
                try:
                    session = await self.get_session()
                    async with session.get(job_res.get("result_url")) as txt_resp:
                        if txt_resp.status == 200:
                            res_val = await txt_resp.text()
                            print(f"[IMAGE-TO-TEXT] Fetched content from result_url, length: {len(res_val)}")
                except Exception as fetch_err:
                    print(f"[IMAGE-TO-TEXT ERROR] Failed to fetch result_url: {fetch_err}")
            return res_val
        return None

# Global singleton instance for the bot
deapi = DeapiClient()
