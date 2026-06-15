"""Standalone test: talk to Gemini Live via mic and speaker.
No botos needed — just validates the API works end-to-end.

Usage:
    # Either set the env var:
    export GEMINI_API_KEY=your_key
    # Or put it in beemoAI/.env (auto-loaded)
    python3 test_gemini_live.py
"""

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

# Mic: 16kHz mono S16_LE
MIC_RATE = 16000
MIC_CHANNELS = 1
MIC_CHUNK = 1600  # 100ms of audio

# Speaker: MAX98357A wants stereo
SPEAKER_RATE = 24000  # Gemini outputs 24kHz
SPEAKER_CHANNELS = 2


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
    """Duplicate each sample to convert mono to stereo."""
    samples = struct.unpack(f"<{len(pcm_mono) // 2}h", pcm_mono)
    return struct.pack(f"<{len(samples) * 2}h", *[s for s in samples for _ in range(2)])


async def main():
    mic_dev = await find_mic_device()
    spk_dev = await find_speaker_device()
    print(f"Mic: {mic_dev}, Speaker: {spk_dev}")

    client = genai.Client(api_key=API_KEY)

    config = {
        "response_modalities": ["AUDIO"],
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {"voice_name": "Kore"}
            }
        },
    }

    # Start mic recording
    mic_proc = await asyncio.create_subprocess_exec(
        "arecord",
        "-D", mic_dev,
        "-f", "S16_LE",
        "-c", str(MIC_CHANNELS),
        "-r", str(MIC_RATE),
        "-t", "raw",
        "--buffer-size", str(MIC_CHUNK * 4),
        "--period-size", str(MIC_CHUNK),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    # Start speaker playback (persistent aplay process)
    spk_proc = await asyncio.create_subprocess_exec(
        "aplay",
        "-D", spk_dev,
        "-f", "S16_LE",
        "-c", str(SPEAKER_CHANNELS),
        "-r", str(SPEAKER_RATE),
        "-t", "raw",
        "--buffer-size", "9600",
        stdin=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    print("Connecting to Gemini Live...")

    async with client.aio.live.connect(model=MODEL, config=config) as session:
        print("Connected! Start talking...")

        _send_count = 0

        async def send_audio():
            """Stream mic audio to Gemini."""
            nonlocal _send_count
            chunk_bytes = MIC_CHUNK * MIC_CHANNELS * 2  # 2 bytes per sample
            try:
                while True:
                    data = await mic_proc.stdout.readexactly(chunk_bytes)
                    await session.send_realtime_input(
                        audio=types.Blob(
                            data=data,
                            mime_type="audio/pcm;rate=16000",
                        )
                    )
                    _send_count += 1
                    if _send_count % 50 == 0:  # every 5 seconds
                        print(f"[sent {_send_count} chunks]")
            except (asyncio.IncompleteReadError, asyncio.CancelledError):
                pass

        async def receive_audio():
            """Receive audio from Gemini and play it."""
            try:
                async for response in session.receive():
                    print(f"[recv] {type(response).__name__}: {response}")
                    sc = response.server_content
                    if sc and sc.model_turn:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                print(f"[audio] got {len(part.inline_data.data)} bytes")
                                stereo = mono_to_stereo(part.inline_data.data)
                                spk_proc.stdin.write(stereo)
                                await spk_proc.stdin.drain()
                    if sc and sc.turn_complete:
                        print("[turn complete]")
            except asyncio.CancelledError:
                pass

        try:
            await asyncio.gather(send_audio(), receive_audio())
        except KeyboardInterrupt:
            pass
        finally:
            mic_proc.kill()
            spk_proc.stdin.close()
            await mic_proc.wait()
            await spk_proc.wait()


if __name__ == "__main__":
    asyncio.run(main())
