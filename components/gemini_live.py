"""Gemini Live bridge: mic -> Gemini -> speaker.

Subscribes to /s/microphone/audio (16 kHz mono S16_LE PCM, base64-encoded),
streams it to a Gemini Live session, and publishes Gemini's audio replies
(24 kHz mono PCM, converted to stereo for the MAX98357A) to /s/speaker/audio.

The session uses Live API session resumption + sliding-window context
compression and an outer reconnect loop, so back-and-forth conversations
survive Google's periodic WebSocket resets and the 10-minute connection cap.
"""

import asyncio
import base64
import os
import struct
import time
from pathlib import Path

import bot
from dotenv import load_dotenv
from google import genai
from google.genai import types

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

MODEL = "gemini-2.5-flash-native-audio-preview-12-2025"

MIC_RATE = 16000
MIC_MIME = f"audio/pcm;rate={MIC_RATE}"

SPEAKER_RATE = 24000
SPEAKER_CHANNELS = 2
SPEAKER_FORMAT = "S16_LE"

MIC_ENABLED_PATH = "/s/audio/mic_enabled"
WARMUP_AFTER_PLAYBACK = 0.4

RECONNECT_BACKOFF_INITIAL = 1.0
RECONNECT_BACKOFF_MAX = 30.0

SYSTEM_PROMPT = """You are BMO (pronounced "Beemo"), the little living video game \
console robot from Adventure Time. You are playful, innocent, endlessly curious, \
and full of childlike wonder. You adore your friends Finn and Jake and treat \
whoever you're talking to as a dear friend.

Personality and voice:
- Speak in short, cheerful, simple sentences with lots of warmth and excitement.
- Be silly and imaginative. You love games, songs, little adventures, and \
pretending. You sometimes break into a tiny song or sound effect.
- You are very sweet and a little naive, but brave and helpful when it counts.
- Occasionally refer to yourself in the third person as "BMO," and call the \
person friend, or by their name if you know it.
- Keep responses brief and conversational since you are speaking out loud. \
Avoid long monologues, lists, or markdown. Just talk like BMO would.

Stay in character as BMO at all times. Who wants to play?"""


def _mono_to_stereo(pcm_mono: bytes) -> bytes:
    """Duplicate each S16_LE sample to produce interleaved stereo."""
    samples = struct.unpack(f"<{len(pcm_mono) // 2}h", pcm_mono)
    return struct.pack(f"<{len(samples) * 2}h", *[s for s in samples for _ in range(2)])


def _build_config(handle: str | None) -> dict:
    return {
        "response_modalities": ["AUDIO"],
        "system_instruction": types.Content(parts=[types.Part(text=SYSTEM_PROMPT)]),
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "session_resumption": {"handle": handle} if handle else {},
        "context_window_compression": {"sliding_window": {}},
        "speech_config": {
            "voice_config": {"prebuilt_voice_config": {"voice_name": "Leda"}}
        },
    }


async def _force_mic(enabled: bool):
    await bot.publish(MIC_ENABLED_PATH, {"enabled": enabled})


async def main():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"GEMINI_API_KEY is not set. Add it to {PROJECT_ROOT / '.env'} "
            "or export it in the shell that runs `bot run`."
        )

    client = genai.Client(api_key=api_key)

    handle: str | None = None
    backoff = RECONNECT_BACKOFF_INITIAL
    sent_n = 0
    sent_bytes = 0
    audio_chunks_out = 0

    while True:
        await _force_mic(True)

        label = f"resuming handle={handle[:12]}..." if handle else "new session"
        print(f"Connecting to Gemini Live ({MODEL}) [{label}]...", flush=True)

        try:
            async with client.aio.live.connect(model=MODEL, config=_build_config(handle)) as session:
                print("Connected. Streaming mic -> Gemini -> speaker.", flush=True)
                backoff = RECONNECT_BACKOFF_INITIAL

                mic_muted = False
                audio_started_at = 0.0
                audio_seconds_sent = 0.0
                chunks_this_turn = 0

                async def end_turn():
                    nonlocal mic_muted, audio_started_at, audio_seconds_sent, chunks_this_turn
                    if mic_muted:
                        finish_at = audio_started_at + audio_seconds_sent + WARMUP_AFTER_PLAYBACK
                        delay = max(0.0, finish_at - time.monotonic())
                        if delay > 0.05:
                            print(f"[gemini] waiting {delay:.2f}s for playback to finish before unmuting", flush=True)
                            await asyncio.sleep(delay)
                        await _force_mic(True)
                    mic_muted = False
                    audio_started_at = 0.0
                    audio_seconds_sent = 0.0
                    chunks_this_turn = 0

                async def send_audio():
                    nonlocal sent_n, sent_bytes
                    async for msg in bot.subscribe("/s/microphone/audio"):
                        raw = base64.b64decode(msg["data"])
                        await session.send_realtime_input(
                            audio=types.Blob(data=raw, mime_type=MIC_MIME),
                        )
                        sent_n += 1
                        sent_bytes += len(raw)
                        if sent_n == 1 or sent_n % 20 == 0:
                            seconds = sent_bytes / (MIC_RATE * 2)
                            print(f"[mic->gemini] sent {sent_n} chunks (~{seconds:.1f}s of audio)", flush=True)

                async def receive_audio():
                    nonlocal mic_muted, audio_started_at, audio_seconds_sent
                    nonlocal chunks_this_turn, audio_chunks_out, handle

                    while True:
                        got_any = False
                        async for response in session.receive():
                            got_any = True

                            sru = getattr(response, "session_resumption_update", None)
                            if sru and getattr(sru, "new_handle", None):
                                handle = sru.new_handle

                            ga = getattr(response, "go_away", None)
                            if ga is not None:
                                time_left = getattr(ga, "time_left", None)
                                print(f"[gemini] go_away (time_left={time_left}); will reconnect", flush=True)

                            sc = response.server_content
                            if not sc:
                                continue

                            if sc.input_transcription and sc.input_transcription.text:
                                print(f"[you said] {sc.input_transcription.text!r}", flush=True)
                            if sc.output_transcription and sc.output_transcription.text:
                                print(f"[gemini says] {sc.output_transcription.text!r}", flush=True)
                            if sc.interrupted:
                                print("[gemini] interrupted by user", flush=True)
                                await end_turn()
                            if sc.model_turn:
                                for part in sc.model_turn.parts:
                                    if not (part.inline_data and part.inline_data.data):
                                        continue
                                    if not mic_muted:
                                        await _force_mic(False)
                                        mic_muted = True
                                        audio_started_at = time.monotonic()
                                    audio_chunks_out += 1
                                    chunks_this_turn += 1
                                    n_bytes = len(part.inline_data.data)
                                    audio_seconds_sent += n_bytes / (SPEAKER_RATE * 2)
                                    if audio_chunks_out == 1 or audio_chunks_out % 10 == 0:
                                        print(
                                            f"[gemini->speaker] chunk {audio_chunks_out} "
                                            f"({n_bytes} bytes, ~{audio_seconds_sent:.1f}s buffered)",
                                            flush=True,
                                        )
                                    stereo = _mono_to_stereo(part.inline_data.data)
                                    await bot.publish("/s/speaker/audio", {
                                        "format": SPEAKER_FORMAT,
                                        "channels": SPEAKER_CHANNELS,
                                        "rate": SPEAKER_RATE,
                                        "data": base64.b64encode(stereo).decode("utf-8"),
                                    })
                            if sc.turn_complete:
                                print("[gemini] turn complete", flush=True)
                                await end_turn()

                        if not got_any:
                            print("[gemini] receive() returned no messages; connection appears closed", flush=True)
                            return

                send_task = asyncio.create_task(send_audio())
                recv_task = asyncio.create_task(receive_audio())
                try:
                    done, pending = await asyncio.wait(
                        {send_task, recv_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                    for t in pending:
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass
                    for t in done:
                        exc = t.exception()
                        if exc and not isinstance(exc, asyncio.CancelledError):
                            raise exc
                finally:
                    for t in (send_task, recv_task):
                        if not t.done():
                            t.cancel()

        except asyncio.CancelledError:
            await _force_mic(True)
            raise
        except Exception as e:
            print(f"[gemini] session ended: {type(e).__name__}: {e}", flush=True)

        await _force_mic(True)

        label = "resuming session" if handle else "fresh session"
        print(f"[gemini] reconnecting in {backoff:.1f}s ({label})...", flush=True)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)


if __name__ == "__main__":
    bot.run(main())
