import asyncio
import base64
import io
import math
import random
import struct
import time

import bot
from PIL import Image, ImageDraw

WIDTH = 320
HEIGHT = 240

BG_COLOR = (0, 200, 180)
EYE_COLOR = (20, 20, 20)
MOUTH_COLOR = (20, 20, 20)

# Lip-sync tuning
RMS_FULL_SCALE = 6000.0   # int16 RMS that maps to a fully open mouth
MOUTH_GAIN = 1.8          # extra scaling so normal speech reaches a wide mouth
MAX_MOUTH_HALF_H = 22     # pixels: half-height of a fully open mouth
FLAP_HZ = 4.5             # how many open/close cycles per second while talking
MIN_FLAP_OPEN = 0.55      # minimum peak opening so even quiet speech visibly flaps

# Sync tuning. Gemini streams its reply faster than real time, so we keep our own
# copy of the audio and "play" through it at the real sample rate, reading the
# amplitude at the current playback position. OUTPUT_LATENCY shifts the mouth
# later to line up with the speaker's aplay output buffer.
SPEAKER_RATE = 24000      # default Hz if a chunk omits "rate"
SPEAKER_CHANNELS = 2      # default channel count if a chunk omits "channels"
OUTPUT_LATENCY = 0.20     # seconds: speaker buffer delay between arrival and sound
WINDOW_S = 0.04           # seconds of audio used to measure "current" loudness
RENDER_DT = 0.06          # seconds between rendered frames (~16 fps)

# Shared playback buffer: the watcher appends raw PCM, the render loop drains it
# in real time. Both coroutines run on the same event loop, so plain dict access
# is safe (no await between read and mutate of these fields).
_audio = {
    "pcm": bytearray(),        # queued-but-not-yet-played speaker PCM
    "play_clock": 0.0,         # monotonic time the front of pcm should sound
    "bytes_per_sec": SPEAKER_RATE * SPEAKER_CHANNELS * 2,
    "bytes_per_frame": SPEAKER_CHANNELS * 2,
}


def _draw_face(blink=False, talking=False, mouth_open=0.0):
    img = Image.new("RGB", (WIDTH, HEIGHT), BG_COLOR)
    draw = ImageDraw.Draw(img)

    cx = WIDTH // 2
    cy = HEIGHT // 2 - 15

    eye_dx = 60   # distance of each eye from center (further apart)
    eye_r = 9     # eye dot radius (smaller)
    if blink:
        draw.line([(cx - eye_dx - eye_r, cy), (cx - eye_dx + eye_r, cy)], fill=EYE_COLOR, width=3)
        draw.line([(cx + eye_dx - eye_r, cy), (cx + eye_dx + eye_r, cy)], fill=EYE_COLOR, width=3)
    else:
        draw.ellipse([(cx - eye_dx - eye_r, cy - eye_r), (cx - eye_dx + eye_r, cy + eye_r)], fill=EYE_COLOR)
        draw.ellipse([(cx + eye_dx - eye_r, cy - eye_r), (cx + eye_dx + eye_r, cy + eye_r)], fill=EYE_COLOR)

    mouth_y = cy + 45
    if talking and mouth_open > 0.05:
        # Open part of the flap: an oval whose height tracks how far open we are.
        half_h = max(2, int(mouth_open * MAX_MOUTH_HALF_H))
        draw.ellipse([(cx - 24, mouth_y - half_h), (cx + 24, mouth_y + half_h)], fill=MOUTH_COLOR)
    elif talking:
        # Closed part of the flap.
        draw.line([(cx - 22, mouth_y), (cx + 22, mouth_y)], fill=MOUTH_COLOR, width=3)
    else:
        # Idle: a friendly smile.
        draw.arc([(cx - 32, mouth_y - 20), (cx + 32, mouth_y + 14)], 0, 180, fill=MOUTH_COLOR, width=4)

    return img


def _image_to_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _chunk_level(raw: bytes) -> float:
    """Cheap RMS over a subsampled S16_LE buffer, mapped to mouth openness (0..1)."""
    n = len(raw) // 2
    if n == 0:
        return 0.0
    step = max(1, n // 256)  # cap the work regardless of chunk size
    acc = 0.0
    count = 0
    for off in range(0, n * 2, step * 2):
        s = struct.unpack_from("<h", raw, off)[0]
        acc += s * s
        count += 1
    rms = math.sqrt(acc / count) if count else 0.0
    return min(1.0, (rms / RMS_FULL_SCALE) * MOUTH_GAIN)


async def _watch_speaker():
    """Buffer the audio Gemini sends to the speaker for real-time playback."""
    async for msg in bot.subscribe("/s/speaker/audio"):
        raw = base64.b64decode(msg["data"])
        rate = int(msg.get("rate", SPEAKER_RATE))
        channels = int(msg.get("channels", SPEAKER_CHANNELS))
        now = time.monotonic()

        # If nothing is queued, this chunk starts a new utterance: anchor the
        # playback clock OUTPUT_LATENCY ahead so the mouth lines up with sound.
        if not _audio["pcm"] and now >= _audio["play_clock"]:
            _audio["play_clock"] = now + OUTPUT_LATENCY
        _audio["bytes_per_sec"] = rate * channels * 2
        _audio["bytes_per_frame"] = channels * 2
        _audio["pcm"].extend(raw)


def _voice_level(now: float) -> float:
    """Advance the playback cursor to `now` and return the current loudness (0..1)."""
    pcm = _audio["pcm"]
    if not pcm:
        return 0.0

    bps = _audio["bytes_per_sec"]
    bpf = _audio["bytes_per_frame"]
    elapsed = now - _audio["play_clock"]
    if elapsed <= 0:
        return 0.0  # still within the output-latency pre-roll; no sound yet

    # Drop the audio that has already played, frame-aligned.
    consume = int(elapsed * bps)
    consume -= consume % bpf
    if consume >= len(pcm):
        pcm.clear()
        _audio["play_clock"] = now
        return 0.0
    if consume > 0:
        del pcm[:consume]
        _audio["play_clock"] += consume / bps

    # Measure loudness of the slice playing right now.
    win = int(WINDOW_S * bps)
    win -= win % bpf
    return _chunk_level(bytes(pcm[:win])) if win else 0.0


async def _render():
    flap_phase = 0.0

    while True:
        now = time.monotonic()
        level = _voice_level(now)          # current voice loudness (0..1)
        talking = bool(_audio["pcm"])

        if talking:
            # Open/close flap, paced by FLAP_HZ; peak height scales with loudness.
            flap_phase += 2 * math.pi * FLAP_HZ * RENDER_DT
            peak = max(MIN_FLAP_OPEN, level)
            mouth_open = peak * (1 - math.cos(flap_phase)) / 2  # 0..peak, starts closed
        else:
            flap_phase = 0.0
            mouth_open = 0.0

        blink = random.random() < 0.08

        frame = _draw_face(blink=blink, talking=talking, mouth_open=mouth_open)
        data = _image_to_b64(frame)

        await bot.publish("/s/screen/display", {
            "type": "image",
            "data": data,
        })

        await asyncio.sleep(RENDER_DT)


async def main():
    await asyncio.gather(_render(), _watch_speaker())


if __name__ == "__main__":
    bot.run(main())
