# BLEPrint

Open-source desktop tool to print labels on the **Nelko® P21** and the
**Supvan® / Katasymbol E10** (T10/T15 family) from Windows or macOS — no phone app required.

- **Find printer…**: search Bluetooth, see which devices are supported printers, and pick one.
  The brand is detected from the MAC prefix (OUI), then the device name, then its GATT service
- **Continuous print** (Supvan E10, continuous tape): send every label in one job so they print back
  to back. Without it, BLEPrint pauses after each label ("Label 1 of 5 printed") so you can cut it,
  then press **Continue**
- **Fit length to text** (Supvan E10): the label is exactly as long as the text, at full tape
  height, so it prints edge to edge instead of centered on a fixed-length label
- **Print as QR**: *QR code + text* or *QR code only*. The code fills the full printable height
  (11 mm on the E10) and works well for short text such as a URL or SKU; long text still prints,
  but the modules get too small to read
- Print images/text as TSPL labels
- **Batch print from a CSV**: build a label from a `{Column}` template, preview any
  row, and print one row or the whole file
- A small reusable Python client (`bleprint.py`) you can script against

> Compatible with the **Nelko® P21** and the **Supvan® / Katasymbol E10**.
> "Nelko", "P21", "Supvan", "Katasymbol" and "E10" are trademarks of their respective
> owners, used here only to describe compatibility. BLEPrint is not affiliated with either.

| Printer | Detected by | Language | Labels |
|---|---|---|---|
| Nelko P21 | MAC prefix `BE:C0:11`, name `P21…`, service `FF00` | TSPL text over BLE, 96-dot (12 mm) head | individual die-cut labels; size and gap come from the loaded roll |
| Supvan E10 / T10 | OUI `A4:93:40` (Beijing Supvan), name `T0xxx…`, service `E0FF…` | `7E 5A` frames + LZMA bitmaps, 96-dot head | continuous tape: one job, or label by label with a cut prompt |

macOS never shows a device's MAC address, but both printers advertise their own MAC in the
last 6 bytes of their manufacturer data, so OUI detection works on macOS as well as Windows.

## Install

```bash
pip install PyQt6 bleak pillow segno
```

## Run

```bash
python gui.py
```

- **macOS:** launch from a Terminal the first time so the OS can grant Bluetooth
  access (or, for the packaged `.app`, approve the Bluetooth prompt).
- Close the phone app first — the printer accepts only one BLE connection at a time.

### Scripting

```python
import asyncio
from bleprint import BlePrinter

async def main():
    dev = await BlePrinter.discover("P21")
    async with BlePrinter(dev) as p:
        await p.print_text("Hello World!", head_mm=15, feed_mm=40)

asyncio.run(main())
```

## How it works

**Nelko P21.** The printer speaks **TSPL** over a BLE GATT service (`FF00`: write `FF02`, status
`FF01`). No pairing/crypto handshake is required to print. See
[`PROTOCOL.md`](PROTOCOL.md) for the full reverse-engineered protocol notes.

Label geometry matters: a "15×40 mm" roll is **15 mm across the head × 40 mm along
the feed**, so text is rendered landscape and rotated 90°.

**Supvan E10.** Binary `7E 5A` command frames on service `E0FF-3C17-…` (write `FFE9`, notify
`FFE1`), again with no pairing step. The label is rasterised for the 96-dot head (8 dots/mm),
packed into 4000-byte buffers, LZMA-compressed and streamed in 500-byte frames that the printer
acknowledges. The job encoding follows the MIT-licensed
[supvan-katasymbol-printer-web-ui](https://github.com/khromov/supvan-katasymbol-printer-web-ui)
project (© Katasymbol Web contributors). See `supvan.py` and [`PROTOCOL.md`](PROTOCOL.md).

## Scope & legality

This project is a clean reimplementation of a **functional protocol** (GATT UUIDs,
TSPL command names, byte layouts) for interoperability with hardware you own. It
contains **none** of the vendor's code, firmware, artwork, or credentials, and it
does not include or require any app-decryption step. Please don't open issues asking
for the vendor's binaries or firmware — they won't be distributed here.

## License

MIT — see [`LICENSE`](LICENSE).
