"""Record 5 seconds and transcribe locally with Vosk."""

import asyncio
import json
import re
import wave

from vosk import Model, KaldiRecognizer

RATE = 16000
CHANNELS = 1
DURATION = 5


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
    print(f"Recorded {len(audio_data)} bytes")

    print("Transcribing with Vosk...")
    model = Model(lang="en-us")
    rec = KaldiRecognizer(model, RATE)

    # Feed in chunks
    chunk_size = 4000
    for i in range(0, len(audio_data), chunk_size):
        rec.AcceptWaveform(audio_data[i:i + chunk_size])

    result = json.loads(rec.FinalResult())
    print(f"Vosk heard: {result.get('text', '(nothing)')}")


if __name__ == "__main__":
    asyncio.run(main())
