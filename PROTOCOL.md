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

---

## Supvan E10 (T10/T15) BLE protocol (verified live 2026-10-15)

Source: Supvan IPrinter 3.9.18 (`SFBLEManager`, `SFPrintUtils`). The E10 shows up as `T0132F25…`, handled by `SFDeviceT10Model` (type 15, T15 print path).

### GATT
- Service `0000E0FF-3C17-D293-8E48-14FE2E4DA212`
- Write `FFE9` (write-without-response); notify `FFE1`. `FFEA` notify is unused.
- No pairing or auth handshake is needed. The printer answers commands as soon as notify is enabled.

### Command frame (16 bytes)
`7E 5A 0C 00 | 10 01 AA <cmd> | <sum16 LE> | 00 01 <val16 LE> 00 00`, where sum16 = sum of bytes 10..15.

### Response frame
`7E 5A <len16 LE> 00 03 55 <cmd> <chk> ...payload`. len counts the bytes after the first 4.
- 0x12 CHECK_DEVICE -> ok
- 0x11 INQUERY_STA -> status registers (22-byte body)
- 0xB8 RD_DEV_SN -> ASCII serial from offset 22 ("T0132F25…")
- 0x16 RD_DEV_NAME -> ASCII from offset 22 ("E10pro")
- 0x22 READ_DPI -> no reply on the E10

### Opcodes (initCommonds)
INQUERY_STA 0x11, CHECK_DEVICE 0x12, START_PRINT 0x13, STOP_PRINT 0x14, RD_DEV_NAME 0x16,
READ_REV 0x17, STRD_MAT 0x18, PAPER_SKIP 0x2E, RETURN_MAT 0x30, NEXTFRM_BULK 0x5A,
NEXT_ZIPPEDBULK 0x5C, SET_MAT_DATA 0x5F, RD_DEV_SN 0xB8, PAPER_BACK 0xBA, READ_FIRMWARE_REV 0xC5.

### Printing (implemented in supvan_e10.py; job ran without errors 2026-10-15)
Job encoding was cross-checked against khromov/supvan-katasymbol-printer-web-ui (MIT), `families/t15.ts`, and matches the iOS binary (buffer 0xFA0, `0x5C` announce with 512, frame-marker nudges at 0x7A/0xFA/0x17A).
1. INQUIRY_STA until not busy. Status bytes are at [14..19] and battery mV at [24..25].
2. SET_HEADRATE 0xC9 = 80 + 10*(density-1), then START_PRINT 0x13 (arg 0), then poll until the printing bit (b[2]&0x40) is set.
3. PAPER_BACK 0xBA(back columns).
4. For each 4000-byte buffer: wait until !bufferFull, send `0x5C(512, nframes)`, then 512-byte data frames
   `7E 5A FC 01 10 02 AA BB <sum16> <idx> <cnt> <500 B>`. Each frame goes as four 128-byte writes and the printer acks each one. Then send BUF_FULL 0x10(0).
5. Poll until not printing and not busy.
Buffer layout: [0..1] checksum, [2] page flags (start 0x02, end 0x04, job end 0x08, cut<<4), [3] density<<2|0x10, [4..5] columns, [6]=12, [8]=1, [10]=1, [12] skip, then 12 bytes per column (row r -> byte r/8, bit r%8; design in the middle 88 of 96 dots).
Each buffer is compressed with LZMA (lc3 lp0 pb2, 8 KB dict) behind a 13-byte .lzma header.
