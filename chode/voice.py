import asyncio
import os
import aiohttp

async def generate_speech(text: str, output_file: str = "chode_speech.mp3") -> str:
    """Generates speech from text using DEAPI and saves it to a file."""
    # Ensure the output file is saved in the chode directory, next to this script
    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(script_dir, output_file)
    
    # Strip any weird markdown formatting first so the TTS doesn't try to read asterisks
    clean_text = text.replace("*", "").replace("`", "").replace("_", "").strip()
    
    if not clean_text:
        return ""
        
    try:
        from chode import deapi_client
        result = await deapi_client.deapi.generate_speech(clean_text)
        result_url = result.get("result_url")
        
        if not result_url:
            print(f"[ERROR] TTS generation failed: API returned no URL")
            return ""
            
        async with aiohttp.ClientSession() as session:
            async with session.get(result_url) as resp:
                if resp.status == 200:
                    with open(output_path, "wb") as f:
                        f.write(await resp.read())
                    return output_path
                else:
                    print(f"[ERROR] Failed to download TTS audio: {resp.status}")
                    return ""
    except Exception as e:
        print(f"[ERROR] DEAPI TTS generation failed: {e}")
        import traceback
        traceback.print_exc()
        return ""

import discord
# from discord.ext import voice_recv
import speech_recognition as sr
import time
import io
from pydub import AudioSegment

# class SpeechRecognitionSink(voice_recv.AudioSink):
#     \"\"\"
#     A persistent audio sink that collects PCM data from speaking users,
#     detects silence (1 second), and automatically transcribes their utterance.
#     \"\"\"
#     def __init__(self, bot, callback):
#         super().__init__()
#         self.bot = bot
#         self.callback = callback
#         self.buffers = {}
#         self.last_speech = {}
#         self.recognizer = sr.Recognizer()
#         
#         # Start a background task to process audio buffers continually
#         bot.loop.create_task(self._process_loop())
# 
#     def wants_opus(self) -> bool:
#         # We need raw PCM to easily chunk and transcribe
#         return False
# 
#     def write(self, user, data):
#         if not user: 
#             return
#             
#         now = time.time()
#         
#         if user.id not in self.buffers:
#             self.buffers[user.id] = bytearray()
#             
#         self.buffers[user.id].extend(data.pcm)
#         self.last_speech[user.id] = now
#         
#     def cleanup(self):
#         self.buffers.clear()
#         self.last_speech.clear()
# 
#     async def _process_loop(self):
#         while True:
#             await asyncio.sleep(0.5)
#             now = time.time()
#             to_process = []
#             
#             for uid, last_time in list(self.last_speech.items()):
#                 # Detect 1-second silence
#                 if now - last_time > 1.0:
#                     pcm_data = self.buffers.pop(uid, None)
#                     self.last_speech.pop(uid, None)
#                     
#                     # Only process chunks larger than ~0.5 seconds to ignore random noise/pops
#                     if pcm_data and len(pcm_data) > 48000: 
#                         to_process.append((uid, pcm_data))
#                         
#             for uid, pcm_data in to_process:
#                 user = self.bot.get_user(uid)
#                 if user and not user.bot:
#                     # Run the heavy transcription on a separate thread
#                     await self._transcribe_and_callback(user, pcm_data)
#                     
#     async def _transcribe_and_callback(self, user, pcm_data):
#         def _sync_transcribe():
#             try:
#                 # Convert raw PCM (Discord: 48000Hz, 16-bit, stereo) into a WAV segment
#                 audio_segment = AudioSegment(
#                     data=bytes(pcm_data),
#                     sample_width=2,
#                     frame_rate=48000,
#                     channels=2
#                 )
#                 
#                 # Google Speech Recognition prefers 16kHz mono for accuracy
#                 audio_segment = audio_segment.set_frame_rate(16000).set_channels(1)
#                 
#                 wav_io = io.BytesIO()
#                 audio_segment.export(wav_io, format="wav")
#                 wav_io.seek(0)
#                 
#                 with sr.AudioFile(wav_io) as source:
#                     audio_data = self.recognizer.record(source)
#                     text = self.recognizer.recognize_google(audio_data)
#                     return text
#             except sr.UnknownValueError:
#                 return "" # Didn't understand the audio (expected for coughs/background noise)
#             except Exception as e:
#                 print(f"[ERROR] STT Transcription error: {e}")
#                 return ""
#                 
#         text = await self.bot.loop.run_in_executor(None, _sync_transcribe)
#         
#         if text and text.strip():
#             print(f"🎙️ [STT] {user.name} said: '{text}'")
#             
#             # Simple wake-word check. Since speech recognizers can mishear "chode", we check phonetic neighbors
#             wake_words = ["chode", "showed", "code", "toad", "chad", "should", "joan", "joe"]
#             text_lower = text.lower()
#             
#             if any(word in text_lower for word in wake_words):
#                 # Found a wake-word trigger! Pass it back to our command/LLM callback.
#                 await self.callback(user, text)
