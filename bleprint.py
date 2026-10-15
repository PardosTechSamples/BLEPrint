#!/usr/bin/env python3
"""
Nelko P21 (and similar) label-printer client over Bluetooth LE.

Reverse-engineered from the Nelko iOS app's HLSDK.framework (v4.12.3).

Transport / protocol (verified against a live P21):
  - BLE GATT service 0xFF00
        write char   0xFF02  (write-without-response)  <- commands go here
        notify char  0xFF01  (notify)                  <- status comes back here
  - Command language: plain TSPL text lines, CR/LF terminated.
  - NO handshake / pairing is required to print or query status.
    (The "get rand / get pass" handshake in the SDK is Jieli RCSP on the
     separate 0xAE00 service, used only for firmware OTA — not for printing.)

Dependencies:  pip install bleak pillow
"""

import asyncio
from bleak import BleakClient, BleakScanner

SERVICE = "0000ff00-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000ff02-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000ff01-0000-1000-8000-00805f9b34fb"


class BlePrinter:
    def __init__(self, device):
        self._device = device
        self._client = None
        self._rx = bytearray()
        self._rx_event = asyncio.Event()

    # ---- discovery ----
    @staticmethod
    async def discover(name_prefix="P21", timeout=10.0):
        """Return the first BLE device whose advertised name starts with name_prefix."""
        devices = await BleakScanner.discover(timeout=timeout)
        for d in devices:
            if d.name and d.name.startswith(name_prefix):
                return d
        return None

    @staticmethod
    async def find(address, timeout=15.0):
        return await BleakScanner.find_device_by_address(address, timeout=timeout)

    # ---- connection ----
    async def connect(self):
        """Open the BLE connection and subscribe to status notifications."""
        self._client = BleakClient(self._device)
        await self._client.connect()

        def _on_notify(_, data):
            self._rx += bytes(data)
            self._rx_event.set()

        await self._client.start_notify(CHAR_NOTIFY, _on_notify)
        return self

    async def disconnect(self):
        """Close the BLE connection."""
        if self._client is None:
            return
        try:
            await self._client.stop_notify(CHAR_NOTIFY)
        except Exception:
            pass
        try:
            await self._client.disconnect()
        finally:
            self._client = None

    @property
    def is_connected(self):
        return self._client is not None and self._client.is_connected

    async def __aenter__(self):
        return await self.connect()

    async def __aexit__(self, *exc):
        await self.disconnect()

    @property
    def mtu(self):
        return self._client.mtu_size

    # ---- raw I/O ----
    async def send(self, data: bytes):
        """Write raw bytes to the printer, chunked to the negotiated MTU."""
        mtu = max(20, self.mtu - 3)
        for i in range(0, len(data), mtu):
            await self._client.write_gatt_char(CHAR_WRITE, data[i:i + mtu], response=False)
            await asyncio.sleep(0.01)  # gentle pacing; the SDK does flow control here

    async def command(self, line: str):
        """Send one TSPL command line (CRLF appended)."""
        await self.send(line.encode("latin1") + b"\r\n")

    async def query(self, line: str, wait=1.5) -> bytes:
        """Send a query and return whatever the printer notifies back."""
        self._rx.clear()
        self._rx_event.clear()
        await self.command(line)
        try:
            await asyncio.wait_for(self._rx_event.wait(), timeout=wait)
        except asyncio.TimeoutError:
            pass
        await asyncio.sleep(0.2)
        return bytes(self._rx)

    # ---- convenience status ----
    async def config(self):
        return await self.query("CONFIG?")

    async def battery(self):
        return await self.query("BATTERY?")

    # ---- printing ----
    #
    # Label geometry note (verified on a P21):
    #   head_mm  = label dimension ACROSS the print head  (e.g. 15 for a 15x40 label)
    #   feed_mm  = label dimension ALONG the feed/paper path (e.g. 40)
    # TSPL `SIZE head_mm mm,feed_mm mm`. The bitmap is head_dots wide x feed_dots tall.
    # A "15x40mm" roll is 15 mm across the head and 40 mm long, so to print text
    # reading along the length you render landscape then rotate 90 degrees.

    @staticmethod
    def render_text_label(text, head_mm, feed_mm, dpi=203, along_length=True,
                          margin_dots=16, max_font=200):
        """Render `text` to a 1-bit PIL image sized for the label (head_dots x feed_dots)."""
        from PIL import Image, ImageDraw, ImageFont

        head_dots = int(round(head_mm * dpi / 25.4))
        feed_dots = int(round(feed_mm * dpi / 25.4))
        # Canvas we lay text on: if reading along the length, draw landscape then rotate.
        cw, ch = (feed_dots, head_dots) if along_length else (head_dots, feed_dots)

        img = Image.new("L", (cw, ch), 255)
        d = ImageDraw.Draw(img)
        font = None
        for path in ["/System/Library/Fonts/Supplemental/Arial Bold.ttf",
                     "/System/Library/Fonts/Helvetica.ttc",
                     "/Library/Fonts/Arial.ttf"]:
            try:
                for sz in range(max_font, 8, -2):
                    f = ImageFont.truetype(path, sz)
                    l, t, r, b = d.textbbox((0, 0), text, font=f)
                    if (r - l) <= cw - margin_dots and (b - t) <= ch - margin_dots:
                        font = f
                        break
                if font:
                    break
            except Exception:
                continue
        if font is None:
            font = ImageFont.load_default()
        l, t, r, b = d.textbbox((0, 0), text, font=font)
        d.text(((cw - (r - l)) // 2 - l, (ch - (b - t)) // 2 - t), text, fill=0, font=font)
        if along_length:
            img = img.rotate(90, expand=True)   # -> head_dots wide x feed_dots tall
        return img

    async def print_image(self, image, head_mm, feed_mm, gap_mm=2.0,
                          density=10, speed=3, dpi=203, threshold=128):
        """
        Print a PIL image as a monochrome TSPL label.

        head_mm = size across the print head, feed_mm = size along the feed.
        The image is resized to (head_dots x feed_dots). density: darkness 0-15.
        """
        head_dots = int(round(head_mm * dpi / 25.4))
        feed_dots = int(round(feed_mm * dpi / 25.4))

        img = image.convert("L").resize((head_dots, feed_dots))
        bw = img.point(lambda p: 0 if p < threshold else 255, mode="1")  # 1 = white

        width_bytes = (head_dots + 7) // 8
        raster = bytearray()
        px = bw.load()
        for y in range(feed_dots):
            for xb in range(width_bytes):
                byte = 0
                for bit in range(8):
                    x = xb * 8 + bit
                    # TSPL: bit=1 means the dot is NOT printed (white).
                    dot_white = 1
                    if x < head_dots:
                        dot_white = 1 if px[x, y] else 0
                    byte = (byte << 1) | dot_white
                raster.append(byte)

        # Build ONE contiguous TSPL job buffer, exactly like the SDK does:
        # SIZE / GAP / DIRECTION / REFERENCE / DENSITY / SPEED / CLS / BITMAP+raster / PRINT
        job = bytearray()
        job += f"SIZE {head_mm} mm,{feed_mm} mm\r\n".encode("latin1")
        job += f"GAP {gap_mm:.1f} mm,0 mm\r\n".encode("latin1")
        job += b"DIRECTION 0,0\r\n"
        job += b"REFERENCE 0,0\r\n"
        job += f"DENSITY {density}\r\n".encode("latin1")
        job += f"SPEED {speed}\r\n".encode("latin1")
        job += b"CLS\r\n"
        job += f"BITMAP 0,0,{width_bytes},{feed_dots},0,".encode("latin1")
        job += bytes(raster)
        job += b"\r\n"
        job += b"PRINT 1\r\n"
        await self.send(bytes(job))

    async def print_text(self, text, head_mm, feed_mm, **kw):
        """Convenience: render `text` to fit the label and print it."""
        img = self.render_text_label(text, head_mm, feed_mm, dpi=kw.get("dpi", 203))
        await self.print_image(img, head_mm, feed_mm, **kw)


# ---- demo ----
async def _demo():
    print("Scanning for P21...")
    dev = await BlePrinter.discover("P21", timeout=10.0)
    if not dev:
        print("No P21 found. Close the phone app and make sure it is on.")
        return
    print("Found:", dev.name, dev.address)
    async with BlePrinter(dev) as p:
        print("Connected, MTU", p.mtu)
        print("CONFIG? ->", (await p.config()).hex(" "))
        print("BATTERY? ->", (await p.battery()).hex(" "))
        # Print "Hello World!" on a 15 x 40 mm label (15 across head, 40 along feed).
        await p.print_text("Hello World!", head_mm=15, feed_mm=40)
        import asyncio as _a
        await _a.sleep(6)


if __name__ == "__main__":
    asyncio.run(_demo())
