"""Record 3 seconds of mic audio, save to file, then send to Gemini for transcription."""

import asyncio
import base64
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ["GEMINI_API_KEY"]
RATE = 16000
CHANNELS = 1
DURATION = 3


async def find_mic_device():
    proc = await asyncio.create_subprocess_exec(
        "arecord", "-l",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    for line in stdout.decode().splitlines():
        m = re.match(r"card\s+(\d+).*device\s+(\d+)", line, re.IGNORECASE)
        if m:
            return f"hw:{m.group(1)},{m.group(2)}"
    return "default"


async def main():
    mic_dev = await find_mic_device()
    print(f"Mic: {mic_dev}")
    print(f"Recording {DURATION} seconds... speak now!")

    proc = await asyncio.create_subprocess_exec(
        "arecord",
        "-D", mic_dev,
        "-f", "S16_LE",
        "-c", str(CHANNELS),
        "-r", str(RATE),
        "-t", "raw",
        "-d", str(DURATION),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    audio_data, _ = await proc.communicate()
    print(f"Recorded {len(audio_data)} bytes ({len(audio_data) / (RATE * 2):.1f}s)")

    # Save raw file for inspection
    with open("/tmp/mic_test.raw", "wb") as f:
        f.write(audio_data)
    print("Saved to /tmp/mic_test.raw")

    # Send to Gemini for transcription
    print("Sending to Gemini for transcription...")
    client = genai.Client(api_key=API_KEY)

    audio_b64 = base64.b64encode(audio_data).decode()
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            mime_type="audio/pcm;rate=16000",
                            data=audio_data,
                        )
                    ),
                    types.Part(text="Transcribe exactly what you hear in this audio."),
                ],
            )
        ],
    )
    print(f"Gemini heard: {response.text}")


if __name__ == "__main__":
    asyncio.run(main())
