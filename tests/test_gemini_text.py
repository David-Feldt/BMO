"""Quick test: send a text message to Gemini Live, get audio back, play it."""

import asyncio
import os
import re
import struct
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = "gemini-3.1-flash-live-preview"

SPEAKER_RATE = 24000
SPEAKER_CHANNELS = 2


async def find_speaker_device():
    proc = await asyncio.create_subprocess_exec(
        "aplay", "-l",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    for line in stdout.decode().splitlines():
        if "hifiberry" in line.lower():
            m = re.match(r"card\s+(\d+).*device\s+(\d+)", line, re.IGNORECASE)
            if m:
                return f"hw:{m.group(1)},{m.group(2)}"
    return "default"


def mono_to_stereo(pcm_mono: bytes) -> bytes:
    samples = struct.unpack(f"<{len(pcm_mono) // 2}h", pcm_mono)
    return struct.pack(f"<{len(samples) * 2}h", *[s for s in samples for _ in range(2)])


async def main():
    spk_dev = await find_speaker_device()
    print(f"Speaker: {spk_dev}")

    client = genai.Client(api_key=API_KEY)

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Kore")
            )
        ),
    )

    print("Connecting to Gemini Live...")
    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Connected!")

        print("Sending text message...")
        await session.send_client_content(
            turns=[types.Content(
                role="user",
                parts=[types.Part(text="Say hello, I am beemo!")]
            )],
            turn_complete=True,
        )
        print("Message sent, waiting for audio response...")

        # Collect audio
        audio_data = bytearray()
        async for response in session.receive():
            sc = response.server_content
            if sc and sc.model_turn:
                for part in sc.model_turn.parts:
                    if part.inline_data and part.inline_data.data:
                        print(f"Got {len(part.inline_data.data)} bytes of audio")
                        audio_data.extend(part.inline_data.data)
            elif response.session_resumption_update:
                continue  # skip these
            else:
                print(f"[other] {response}")
            if sc and sc.turn_complete:
                print(f"Turn complete! Total: {len(audio_data)} bytes")
                break

        if audio_data:
            stereo = mono_to_stereo(bytes(audio_data))
            print(f"Playing {len(stereo)} bytes...")
            proc = await asyncio.create_subprocess_exec(
                "aplay", "-D", spk_dev,
                "-f", "S16_LE", "-c", str(SPEAKER_CHANNELS),
                "-r", str(SPEAKER_RATE), "-t", "raw",
                stdin=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            proc.stdin.write(stereo)
            proc.stdin.close()
            await proc.wait()
            print("Done!")
        else:
            print("No audio received")


if __name__ == "__main__":
    asyncio.run(main())
