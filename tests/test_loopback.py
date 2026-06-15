"""Live mic-to-speaker loopback to isolate microphone issues.

Bypasses the bot framework. Pipes arecord (USB mic, hw:4,0) directly to
aplay (HiFiBerry / MAX98357A, hw:3,0). plughw lets ALSA handle mono→stereo
and any rate conversion.

Usage: python test_loopback.py [seconds]   (default 15s, Ctrl-C to stop early)
"""

import subprocess
import sys

MIC = "plughw:4,0"
SPK = "plughw:3,0"
RATE = 16000
SECONDS = int(sys.argv[1]) if len(sys.argv) > 1 else 15

print(f"Loopback {MIC} -> {SPK} for {SECONDS}s. Speak into the mic.")

rec = subprocess.Popen(
    ["arecord", "-D", MIC, "-f", "S16_LE", "-c", "1", "-r", str(RATE),
     "-t", "raw", "-d", str(SECONDS)],
    stdout=subprocess.PIPE,
)
play = subprocess.Popen(
    ["aplay", "-D", SPK, "-f", "S16_LE", "-c", "1", "-r", str(RATE), "-t", "raw"],
    stdin=rec.stdout,
)
rec.stdout.close()
play.wait()
rec.wait()
print("Done.")
