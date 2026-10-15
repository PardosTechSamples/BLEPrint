# Nelko P21 — BLE printing protocol

Reverse-engineered for interoperability and verified live against a P21 (203 DPI).
Independent project; not affiliated with Nelko.

## 1. Connection / handshake

Bluetooth LE only. The printer advertises with its model name (e.g. `P21`).

| Service | Characteristic | Role |
|---------|----------------|------|
| `FF00`  | `FF02` (write-without-response) | commands → printer |
| `FF00`  | `FF01` (notify) | status ← printer |
| `FF00`  | `FF03` (notify) | flow control (unused for label-sized jobs) |

Handshake to print is simply: **GATT connect → subscribe to `FF01` → send commands.**

- **No pairing / crypto handshake is required to print.** The printer answers
  commands immediately once connected. (A separate JieLi "RCSP" channel exists on a
  `AE00` service for firmware updates; it is not involved in printing.)
- The connect LED turns **blue** when connected with a label roll loaded; **red**
  means out-of-paper / not ready, not "unpaired".
- Negotiated MTU observed: 185. Write commands in `MTU-3` byte chunks.

Status queries round-trip cleanly, e.g.:

```
>> CONFIG?\r\n      << "CONFIG " + bytes (byte 0xCB = 203 → 203 DPI)
>> BATTERY?\r\n     << "BATTERY " + level bytes
```

## 2. What is printed (TSPL)

Commands are plain **TSPL** text lines, CRLF-terminated, sent to `FF02`. A label job
is one contiguous buffer:

```
SIZE <w> mm,<h> mm
GAP <g> mm,0 mm
DIRECTION 0,0
REFERENCE 0,0
DENSITY <0..15>
SPEED <n>
CLS
BITMAP 0,0,<widthBytes>,<heightDots>,0,<raw raster bytes>
PRINT 1
```

- Raster is 1 bpp, MSB-first. **Bit = 1 means white (dot not printed)**, bit = 0 =
  black. `widthBytes = ceil(widthDots / 8)`.
- `widthDots = head_mm * dpi / 25.4`, `heightDots = feed_mm * dpi / 25.4` (dpi = 203).
- Other useful commands: `FORMFEED`, `GAPDETECT`, `CALIBRATE`, `SELFTEST`.

## 3. Expected paper size (P21)

The P21 uses a **15 × 40 mm** label by default — that is **15 mm across the print
head × 40 mm along the feed** (not 40 wide). At 203 DPI:

| Axis | mm | dots |
|------|----|------|
| across head (bitmap width) | 15 | 120 |
| along feed (bitmap height) | 40 | 320 |

So `SIZE 15 mm,40 mm`, bitmap 120 wide × 320 tall. To print text reading along the
40 mm length, render it landscape and rotate 90°. (Getting width/length swapped
prints a rotated, clipped label.)

- DPI: 203 · head width: ~15 mm printable · default gap: ~2 mm
