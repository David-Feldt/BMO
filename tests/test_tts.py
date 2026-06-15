"""Generate AI voice via Gemini TTS and play it directly through the speaker.

Bypasses the bot framework. Pipes raw PCM straight to aplay on hw:3,0
(HiFiBerry / MAX98357A). Mono is duplicated to stereo because the amp
expects 2 channels.
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ["GEMINI_API_KEY"]
DEVICE = "plughw:3,0"  # plug = let ALSA resample 24kHz → device rate cleanly
RATE = 24000  # Gemini TTS returns 24 kHz, 16-bit, mono PCM
VOICE = "Kore"
TEXT = sys.argv[1] if len(sys.argv) > 1 else "Hello, I am Beemo. Can you hear me clearly?"

client = genai.Client(api_key=API_KEY)
response = client.models.generate_content(
    model="gemini-2.5-flash-preview-tts",
    contents=TEXT,
    config=types.GenerateContentConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=VOICE)
            )
        ),
    ),
)

mono_pcm = response.candidates[0].content.parts[0].inline_data.data
n = len(mono_pcm) // 2
samples = struct.unpack(f"<{n}h", mono_pcm)
stereo_pcm = struct.pack(f"<{n * 2}h", *(s for sample in samples for s in (sample, sample)))

print(f"Got {len(mono_pcm)} bytes mono PCM ({n / RATE:.2f}s). Playing on {DEVICE}...")

subprocess.run(
    ["aplay", "-D", DEVICE, "-f", "S16_LE", "-c", "2", "-r", str(RATE), "-t", "raw"],
    input=stereo_pcm,
    check=True,
)
