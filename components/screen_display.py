import asyncio
import base64
import io

import bot
import numpy as np
import spidev
import gpiod
from gpiod.line import Direction, Value
from PIL import Image, ImageDraw, ImageFont

PANEL_W = 240
PANEL_H = 320
WIDTH = 320
HEIGHT = 240

RST_PIN = 6
DC_PIN = 26
BL_PIN = 13

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED = 40_000_000


def _init_gpio():
    chip = gpiod.Chip("/dev/gpiochip4")
    config = {
        RST_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
        DC_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
        BL_PIN: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE),
    }
    lines = chip.request_lines(consumer="screen_display", config=config)
    return chip, lines


def _init_spi():
    spi = spidev.SpiDev()
    spi.open(SPI_BUS, SPI_DEVICE)
    spi.max_speed_hz = SPI_SPEED
    spi.mode = 0
    return spi


def _write_cmd(spi, lines, cmd):
    lines.set_value(DC_PIN, Value.INACTIVE)
    spi.writebytes([cmd])


def _write_data(spi, lines, data):
    lines.set_value(DC_PIN, Value.ACTIVE)
    spi.writebytes([data])


def _write_data_bulk(spi, lines, data):
    lines.set_value(DC_PIN, Value.ACTIVE)
    for i in range(0, len(data), 4096):
        spi.writebytes2(data[i:i + 4096])


def _reset(lines):
    lines.set_value(RST_PIN, Value.ACTIVE)
    import time
    time.sleep(0.01)
    lines.set_value(RST_PIN, Value.INACTIVE)
    time.sleep(0.01)
    lines.set_value(RST_PIN, Value.ACTIVE)
    time.sleep(0.01)


def _init_display(spi, lines):
    _reset(lines)

    _write_cmd(spi, lines, 0x11)  # sleep out
    import time
    time.sleep(0.12)

    _write_cmd(spi, lines, 0x36)  # memory access control — landscape, flipped 180
    _write_data(spi, lines, 0xA0)

    _write_cmd(spi, lines, 0x3A)  # pixel format: 16-bit RGB565
    _write_data(spi, lines, 0x05)

    _write_cmd(spi, lines, 0x21)  # inversion on (typical for IPS panels)

    _write_cmd(spi, lines, 0x29)  # display on


def _set_window(spi, lines, x0, y0, x1, y1):
    _write_cmd(spi, lines, 0x2A)  # column address set
    _write_data(spi, lines, (x0 >> 8) & 0xFF)
    _write_data(spi, lines, x0 & 0xFF)
    _write_data(spi, lines, (x1 >> 8) & 0xFF)
    _write_data(spi, lines, x1 & 0xFF)

    _write_cmd(spi, lines, 0x2B)  # row address set
    _write_data(spi, lines, (y0 >> 8) & 0xFF)
    _write_data(spi, lines, y0 & 0xFF)
    _write_data(spi, lines, (y1 >> 8) & 0xFF)
    _write_data(spi, lines, y1 & 0xFF)

    _write_cmd(spi, lines, 0x2C)  # memory write


def _show_image(spi, lines, image):
    img = image.resize((WIDTH, HEIGHT)).convert("RGB")
    arr = np.asarray(img, dtype=np.uint16)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
    # Big-endian (high byte first), matching the panel's RGB565 byte order.
    rgb565 = rgb565.astype(">u2").tobytes()

    _set_window(spi, lines, 0, 0, WIDTH - 1, HEIGHT - 1)
    _write_data_bulk(spi, lines, rgb565)


def _text_to_image(text):
    img = Image.new("RGB", (WIDTH, HEIGHT), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    draw.text((10, 10), text, fill=(255, 255, 255), font=font)
    return img


async def main():
    chip, lines = _init_gpio()
    spi = _init_spi()
    _init_display(spi, lines)

    splash = _text_to_image("beemoAI\nscreen ready")
    _show_image(spi, lines, splash)

    # The producer (e.g. beemo_face) can publish faster than the SPI panel can
    # draw. Drain incoming frames in a separate task and only ever render the
    # most recent one, so the display can never fall behind / accumulate lag.
    latest = None
    new_frame = asyncio.Event()

    async def _receive():
        nonlocal latest
        async for msg in bot.subscribe("/s/screen/display"):
            latest = msg
            new_frame.set()

    async def _render():
        nonlocal latest
        while True:
            await new_frame.wait()
            new_frame.clear()
            msg = latest
            if msg is None:
                continue
            if msg.get("type") == "image":
                raw = base64.b64decode(msg["data"])
                img = Image.open(io.BytesIO(raw))
                _show_image(spi, lines, img)
            elif msg.get("type") == "text":
                img = _text_to_image(msg["text"])
                _show_image(spi, lines, img)

    try:
        await asyncio.gather(_receive(), _render())
    finally:
        lines.set_value(BL_PIN, Value.INACTIVE)
        spi.close()
        chip.close()


if __name__ == "__main__":
    bot.run(main())
