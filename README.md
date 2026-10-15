# BLEPrint

Open-source desktop tool to print labels on the **Nelko® P21** (and compatible
JieLi-based BLE label printers) from Windows or macOS — no phone app required.

- Discover and connect to the printer over Bluetooth LE
- Print images/text as TSPL labels
- **Batch print from a CSV**: build a label from a `{Column}` template, preview any
  row, and print one row or the whole file
- A small reusable Python client (`bleprint.py`) you can script against

> Compatible with the **Nelko® P21** and similar JieLi-based BLE label printers.
> "Nelko" and "P21" are trademarks of their respective owner, used here only to
> describe compatibility.

## Install

```bash
pip install PyQt6 bleak pillow
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

The printer speaks **TSPL** over a BLE GATT service (`FF00`: write `FF02`, status
`FF01`). No pairing/crypto handshake is required to print. See
[`PROTOCOL.md`](PROTOCOL.md) for the full reverse-engineered protocol notes.

Label geometry matters: a "15×40 mm" roll is **15 mm across the head × 40 mm along
the feed**, so text is rendered landscape and rotated 90°.

## Scope & legality

This project is a clean reimplementation of a **functional protocol** (GATT UUIDs,
TSPL command names, byte layouts) for interoperability with hardware you own. It
contains **none** of the vendor's code, firmware, artwork, or credentials, and it
does not include or require any app-decryption step. Please don't open issues asking
for the vendor's binaries or firmware — they won't be distributed here.

## License

MIT — see [`LICENSE`](LICENSE).
