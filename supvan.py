"""
Supvan / Katasymbol E10 (T10/T15 family) label printer over Bluetooth LE.

Protocol taken from the Supvan IPrinter iOS app (SFPrintUtils T15Print / T15Transfer). The job
encoding was cross-checked against, and closely follows, the T15 driver in
khromov/supvan-katasymbol-printer-web-ui (src/lib/printer/families/t15.ts):
    MIT License, Copyright (c) 2026 Katasymbol Web contributors.

  BLE   : service E0FF-3C17-..., write FFE9 (no response), notify FFE1
  Frames: command  7E 5A 0C 00 10 01 AA <cmd> <sum16> 00 01 <a16> <b16>      (sum over [10..16))
          data     7E 5A FC 01 10 02 AA BB <sum16> <idx> <cnt> <500 bytes>   (sum over [10..512))
          replies  7E 5A <len16> ... <cmd at [7]> ...                         (total = len + 4)
  Image : 96-dot head, 8 dots/mm, design in the middle 88 rows. 12 bytes per column, row r is
          byte r//8, bit r%8. Up to 332 columns per 4000-byte buffer behind a 14-byte header,
          each buffer LZMA-compressed (8 KB dictionary, 13-byte .lzma header).
  Job   : INQUIRY_STA -> SET_HEADRATE(0xC9) -> START_PRINT -> wait printing -> PAPER_BACK(0xBA)
          -> per buffer: wait !bufferFull, NEXT_ZIPPEDBULK(0x5C, 512, nframes), frames, BUF_FULL(0x10)
          -> wait until not printing.
"""
import asyncio, lzma, math, struct, time
from bleak import BleakClient, BleakScanner
from PIL import Image, ImageDraw, ImageFont, ImageOps

WRITE_UUIDS = ["ffe9", "fec1", "c302", "ff02", "2af1"]
NOTIFY_UUIDS = ["ffe1", "fec1", "c305", "ff01", "2af0"]
NAME_PREFIXES = ("T0",)

BUF_FULL, INQUIRY_STA, START_PRINT, STOP_PRINT = 0x10, 0x11, 0x13, 0x14
RD_DEV_NAME, STRD_MAT, RETURN_MAT, NEXT_ZIPPEDBULK = 0x16, 0x18, 0x30, 0x5C
PAPER_BACK, SET_HEADRATE = 0xBA, 0xC9

HEAD_DOTS, PRINT_DOTS, DPMM = 96, 88, 8
BYTES_PER_COL, BUF_LEN, MAX_COLS, LEAD_COLS = 12, 4000, 332, 48


# ---------------------------------------------------------------- encoding

def command_frame(cmd, a=0, b=0):
    f = bytearray([0x7E, 0x5A, 0x0C, 0x00, 0x10, 0x01, 0xAA, cmd & 0xFF, 0, 0, 0x00, 0x01]) + struct.pack("<HH", a & 0xFFFF, b & 0xFFFF)
    struct.pack_into("<H", f, 8, sum(f[10:16]) & 0xFFFF)
    return bytes(f)


def data_frames(stream):
    count = math.ceil(len(stream) / 500)
    out = []
    for i in range(count):
        f = bytearray(512)
        f[0:8] = b"\x7e\x5a\xfc\x01\x10\x02\xaa\xbb"
        f[10], f[11] = i & 0xFF, count & 0xFF
        chunk = stream[i * 500:(i + 1) * 500]
        f[12:12 + len(chunk)] = chunk
        struct.pack_into("<H", f, 8, sum(f[10:512]) & 0xFFFF)
        out.append(bytes(f))
    return out


def compress(buf):
    """LZMA-alone stream: props 0x5D, 8 KB dictionary, known size (the app's DefaultLzmaCompressAddHeader)."""
    raw = lzma.compress(bytes(buf), format=lzma.FORMAT_RAW,
                        filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 8192, "lc": 3, "lp": 0, "pb": 2, "nice_len": 128}])
    return bytes([0x5D]) + struct.pack("<I", 8192) + struct.pack("<Q", len(buf)) + raw


def avoid_frame_markers(buf, stream):
    """iOS T15Transfer: if the stream puts AA <BB|06|C7|07> where a 128-byte BLE write's frame marker sits, nudge a dot and recompress."""
    hits = [i for i in (0x7A, 0xFA, 0x17A) if i + 1 < len(stream) and stream[i] == 0xAA and stream[i + 1] in (0xBB, 0x06, 0xC7, 0x07)]
    if not hits:
        return stream
    for i in hits:
        buf[i] = (buf[i] + 1) & 0xFF
    return compress(buf)


def raster(img, left=0, top=0):
    """PIL image (width = label length, any height) -> list of 12-byte columns for the 96-dot head."""
    img = img.convert("L")
    if img.height > PRINT_DOTS:  # keep the middle 88 rows
        y0 = (img.height - PRINT_DOTS) // 2
        img = img.crop((0, y0, img.width, y0 + PRINT_DOTS))
    bw = img.point(lambda p: 1 if p < 128 else 0)
    px = bw.load()
    y_off = (HEAD_DOTS - img.height) // 2 + top
    cols = []
    for c in range(img.width):
        col = bytearray(BYTES_PER_COL)
        x = c - left
        if 0 <= x < img.width:
            for r in range(img.height):
                y = y_off + r
                if 0 <= y < HEAD_DOTS and px[x, r]:
                    col[y >> 3] |= 1 << (y & 7)
        cols.append(bytes(col))
    return cols


def encode_job(labels, density=4, die_cut=False, paper_gap=3, cut_mode=0, avoid_markers=True):
    """labels: list of column lists. Returns (streams, per_label, back_frames). Mirrors T15Print.initEncodeData."""
    buf = bytearray(BUF_LEN)
    streams, per_label, back_frames = [], [], 0
    for n, cols in enumerate(labels):
        last = n == len(labels) - 1
        cut = 2 if cut_mode == 2 else (1 if cut_mode == 0 and last else 0)
        total = len(cols)
        inked = lambda i: 0 <= i < total and any(cols[i])
        current, left, count = 0, total, 0
        while current != total:
            skip, page_start, page_end = 0, False, False
            if current == 0:
                page_start = True
                if n == 0:
                    limit = int((6.5 - paper_gap) * 8) if die_cut else LEAD_COLS
                    i = 0
                    while i < limit and not inked(i):
                        i += 1
                    if i > 0:
                        i -= 1
                    skip = limit - 1 if i >= limit else i
                    back_frames = 0 if skip >= limit - 1 else limit - skip
                    skip = max(0, min(skip, total - 1))
                    current = skip
            left -= skip
            if left > MAX_COLS:
                left -= MAX_COLS
                ncols = MAX_COLS
            else:
                page_end = True
                ncols = left
            for k in range(ncols):
                if current < total:
                    buf[14 + k * BYTES_PER_COL:14 + (k + 1) * BYTES_PER_COL] = cols[current]
                current += 1
            current = min(current, total)
            buf[2] = (0x02 if page_start else 0) | (0x04 if page_end else 0) | (0x08 if page_end and last else 0) | ((cut & 7) << 4)
            buf[3] = ((density << 2) | (1 << 4)) & 0xFF
            buf[4], buf[5], buf[6] = ncols & 0xFF, (ncols >> 8) & 0xFF, BYTES_PER_COL
            buf[8], buf[9], buf[10], buf[11], buf[12], buf[13] = 1, 0, 1, 0, skip & 0xFF, 0
            length = ncols * BYTES_PER_COL + 14
            s = sum(buf[2:14]) + sum(buf[k * 256 - 1] for k in range(1, length // 256 + 1))
            buf[0], buf[1] = s & 0xFF, (s >> 8) & 0xFF
            stream = compress(buf)
            if avoid_markers:
                stream = avoid_frame_markers(buf, stream)
            streams.append(stream)
            count += 1
        per_label.append(count)
    return streams, per_label, back_frames


def head_rate(density):
    """Density 1..7 -> 0xC9 argument (80..140), as the iOS app sends it."""
    f32 = lambda x: struct.unpack("<f", struct.pack("<f", x))[0]  # float32 math like the app
    return int(f32(f32(f32((density - 1) / 10) + f32(0.8)) * 100))


def parse_status(f):
    b = f[14:20]
    st = {
        "buffer_full": bool(b[0] & 0x01), "busy": bool(b[1] & 0x04), "printing": bool(b[2] & 0x40),
        "cover_open": bool(b[2] & 0x08), "charging": bool(b[2] & 0x10), "pages_printed": b[4] | (b[5] << 8),
    }
    if len(f) >= 26:
        st["battery_v"] = round((f[24] | (f[25] << 8)) / 10) / 100
    err = None
    if b[2] & 0x08: err = "Cover is open"
    elif b[3] & 0x01 or b[0] & 0x02: err = "No label detected"
    elif b[0] & 0x04: err = "Labels used up"
    elif b[0] & 0x10: err = "Label tape is not installed"
    st["error"] = err
    return st


# ---------------------------------------------------------------- BLE link

def _short(u):
    u = str(u).lower()
    return u[4:8] if u.endswith("-0000-1000-8000-00805f9b34fb") else u


class SupvanPrinter:
    def __init__(self, device):
        self.device, self.client = device, None
        self.pending, self.frames, self.received = bytearray(), [], 0
        self.event = asyncio.Event()

    @staticmethod
    async def find(timeout=8.0):
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        for d, adv in sorted(found.values(), key=lambda da: -(da[1].rssi or -999)):
            name = adv.local_name or d.name or ""
            if name.startswith(NAME_PREFIXES) and len(name) >= 12:
                return d
        return None

    async def connect(self):
        self.client = BleakClient(self.device, timeout=20.0)
        await self.client.connect()
        chars = {_short(ch.uuid): ch for s in self.client.services for ch in s.characteristics}
        self.wr = next(chars[u] for u in WRITE_UUIDS if u in chars)
        self.nt = next(chars[u] for u in NOTIFY_UUIDS if u in chars)
        await self.client.start_notify(self.nt, self._on_data)
        return self

    async def disconnect(self):
        if self.client:
            try:
                await self.client.disconnect()
            finally:
                self.client = None

    close = disconnect

    @property
    def is_connected(self):
        return self.client is not None and self.client.is_connected

    def _on_data(self, _, data):
        self.received += len(data)
        buf = self.pending + bytes(data)
        i = 0
        while True:
            while i + 1 < len(buf) and not (buf[i] == 0x7E and buf[i + 1] == 0x5A):
                i += 1
            if i + 4 > len(buf):
                break
            total = (buf[i + 2] | (buf[i + 3] << 8)) + 4
            if total < 8 or total > 2048:
                i += 1
                continue
            if i + total > len(buf):
                break
            self.frames.append(bytes(buf[i:i + total]))
            i += total
        self.pending = bytearray(buf[i:])
        self.event.set()

    async def _write(self, data):
        await self.client.write_gatt_char(self.wr, data, response=False)

    async def command(self, cmd, a=0, b=0, timeout=4.0, retry=False):
        for _ in range(2 if retry else 1):
            self.frames.clear(); self.pending.clear()
            await self._write(command_frame(cmd, a, b))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                while self.frames:
                    f = self.frames.pop(0)
                    if f[7] == cmd:
                        return f
                self.event.clear()
                try:
                    await asyncio.wait_for(self.event.wait(), deadline - time.monotonic())
                except asyncio.TimeoutError:
                    pass
        raise TimeoutError(f"no reply to command 0x{cmd:02X}")

    async def status(self):
        return parse_status(await self.command(INQUIRY_STA, retry=True))

    async def name(self):
        f = await self.command(RD_DEV_NAME)
        return f[22:].split(b"\0")[0].decode(errors="replace")

    async def media(self):
        f = await self.command(RETURN_MAT)
        t, gap = (f[39] if len(f) > 39 else 0), (f[42] if len(f) > 42 else 0)
        info = {"die_cut": t in (1, 129), "gap": 3 if gap > 8 else gap,
                "width_mm": min(15, f[40]) if len(f) > 40 else 0, "length_mm": f[41] if len(f) > 41 else 0}
        try:
            await self.command(STRD_MAT, timeout=1.0)
        except TimeoutError:
            pass
        return info

    async def _send_stream(self, stream):
        frames = data_frames(stream)
        await self.command(NEXT_ZIPPEDBULK, 512, len(frames))
        for f in frames:
            self.frames.clear(); self.pending.clear()
            before = self.received
            for o in range(0, 512, 128):  # iOS: four 128-byte writes ~10 ms apart
                await self._write(f[o:o + 128])
                await asyncio.sleep(0.01)
            deadline = time.monotonic() + 2.0
            while self.received == before and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            if self.received == before:
                raise TimeoutError("printer did not acknowledge print data")
        await asyncio.sleep(0.05)
        await self.command(BUF_FULL, 0)

    async def print_images(self, images, density=4, copies=1, die_cut=False, gap=3, log=print, progress=None):
        """Print PIL images (width = label length in dots, 88 rows tall) as ONE continuous job."""
        progress = progress or (lambda done, total: None)
        labels = [raster(im) for im in images] * max(1, copies)
        streams, per_label, back = encode_job(labels, density, die_cut, gap)
        log(f"job: {len(labels)} label(s), {len(streams)} buffer(s), {sum(len(s) for s in streams)} compressed bytes")

        s = None
        for _ in range(30):
            await asyncio.sleep(0.2)
            s = await self.status()
            if not s["busy"]:
                break
        if s and s["error"]:
            raise RuntimeError(s["error"])
        try:
            await self.command(SET_HEADRATE, head_rate(density))
        except TimeoutError:
            pass
        await self.command(START_PRINT, 0)
        try:
            for _ in range(30):
                await asyncio.sleep(0.2)
                s = await self.status()
                if s["printing"]:
                    break
            if not s["printing"]:
                raise RuntimeError("printer did not start printing")
            try:
                await self.command(PAPER_BACK, back)
            except TimeoutError:
                pass
            n = 0
            for li, nbuf in enumerate(per_label):
                log(f"sending label {li + 1}/{len(per_label)}")
                progress(li, len(per_label))
                for _ in range(nbuf):
                    while True:
                        await asyncio.sleep(0.02)
                        s = await self.status()
                        if s["error"]:
                            raise RuntimeError(s["error"])
                        if not s["busy"] and not s["printing"]:
                            raise RuntimeError("printing was stopped on the device")
                        if not s["buffer_full"]:
                            break
                    await self._send_stream(streams[n])
                    n += 1
            log("waiting for printer to finish")
            for _ in range(1200):
                await asyncio.sleep(0.1)
                s = await self.status()
                if not s["printing"] and not s["busy"]:
                    break
            progress(len(per_label), len(per_label))
            log("done")
        except BaseException:
            try:
                await self.command(STOP_PRINT)
            except Exception:
                pass
            raise


E10 = SupvanPrinter  # backwards-compatible name


FONT_PATHS = ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "/Library/Fonts/Arial.ttf", "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]


def render_text_label(text, length_mm=None, margin_mm=1.0):
    """
    Render (multi-line) text for a Supvan label, 88 dots tall.

    length_mm=None/0: fit the label to the text. The font fills the tape height and the label is
    exactly as long as the text plus margin_mm at each end, so it prints edge to edge.
    Otherwise: a fixed length_mm label with the text as large as fits, centered.
    """
    from PIL import Image, ImageDraw, ImageFont
    h = PRINT_DOTS
    fit = not length_mm
    w = 4000 if fit else max(8, int(round(length_mm * DPMM)))
    margin = int(round(margin_mm * DPMM))
    probe = ImageDraw.Draw(Image.new("L", (1, 1)))
    font = None
    for path in FONT_PATHS:
        try:
            for size in range(h * 2, 8, -2):
                f = ImageFont.truetype(path, size)
                l, t, r, b = probe.multiline_textbbox((0, 0), text or " ", font=f, align="center")
                if r - l <= w - 2 * margin and b - t <= h - 2:
                    font = f
                    break
            if font:
                break
        except OSError:
            continue
    font = font or ImageFont.load_default()
    l, t, r, b = probe.multiline_textbbox((0, 0), text or " ", font=font, align="center")
    if fit:
        w = max(8, int(math.ceil(r - l)) + 2 * margin)
    img = Image.new("L", (w, h), 255)
    ImageDraw.Draw(img).multiline_text(((w - (r - l)) // 2 - l, (h - (b - t)) // 2 - t), text,
                                       font=font, fill=0, align="center")
    return img
