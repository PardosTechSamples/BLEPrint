"""
Printer brands BLEPrint knows, how to recognise them from a BLE scan, and one driver per brand.

Brand detection, strongest signal first:
  1. MAC prefix (OUI). macOS hides MAC addresses, but both supported printers put their own MAC
     in the last 6 bytes of the advertised manufacturer data (that is also how the Nelko SDK's
     getMacWithAdvertisementData: and the Supvan app read it). On Windows/Linux the BLE address
     itself is used too.
  2. Advertised name prefix (Nelko "P21…", Supvan "T0xxx…" serials).
  3. Advertised GATT service UUIDs.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field

from bleak import BleakScanner

import qrlabel
import supvan
from bleprint import FEED_MARGIN_DOTS, BlePrinter, head_dots_for

# Known MAC prefixes of label printers.
#   A4:93:40  Beijing Supvan Information Technology Co.,Ltd. (IEEE MA-L registry)
#   BE:C0:11  Nelko P21 (a locally administered prefix, so it is NOT in the IEEE registry; seen on
#             a real P21, BE:C0:11:xx:xx:xx)
OUI_TABLE = {
    "A49340": ("supvan", "Beijing Supvan Information Technology (IEEE OUI)"),
    "BEC011": ("nelko", "Nelko P21 vendor prefix (locally administered, not IEEE-registered)"),
}

NAME_RULES = [
    (re.compile(r"^P21", re.I), "nelko"),
    (re.compile(r"^T0\d{3}[A-Z0-9]{6,}$", re.I), "supvan"),
]

SERVICE_RULES = {
    "0000ff00-0000-1000-8000-00805f9b34fb": "nelko",
    "0000e0ff-3c17-d293-8e48-14fe2e4da212": "supvan",
}

_MAC_RE = re.compile(r"^([0-9A-F]{2}[:-]){5}[0-9A-F]{2}$", re.I)


def fmt_mac(b: bytes) -> str:
    return ":".join(f"{x:02X}" for x in b)


def macs_from_advert(address: str, manufacturer_data: dict[int, bytes]) -> list[str]:
    """Candidate MAC addresses: the BLE address (not on macOS) and the tail of manufacturer data."""
    out = []
    if _MAC_RE.match(address or ""):
        out.append(address.upper().replace("-", ":"))
    for company, payload in (manufacturer_data or {}).items():
        raw = company.to_bytes(2, "little") + bytes(payload)
        if len(raw) >= 6:
            out.append(fmt_mac(raw[-6:]))
    return out


def oui_lookup(mac: str):
    return OUI_TABLE.get(mac.replace(":", "").upper()[:6])


@dataclass
class Found:
    device: object
    name: str
    address: str
    rssi: int
    brand: str | None = None          # "nelko" / "supvan" / None
    reason: str = ""                  # how the brand was detected
    mac: str | None = None
    services: list[str] = field(default_factory=list)


def identify(name: str, address: str, services, manufacturer_data) -> tuple[str | None, str, str | None]:
    """Return (brand, reason, mac)."""
    macs = macs_from_advert(address, manufacturer_data)
    for mac in macs:
        hit = oui_lookup(mac)
        if hit:
            return hit[0], f"MAC {mac} — {hit[1]}", mac
    # Only report a MAC we can trust: a real BLE address (Windows/Linux). Manufacturer-data bytes of
    # other devices are not necessarily a MAC.
    mac = macs[0] if macs and _MAC_RE.match(address or "") else None
    for rx, brand in NAME_RULES:
        if name and rx.search(name):
            return brand, f"name “{name}”", mac
    for s in services or []:
        if s.lower() in SERVICE_RULES:
            return SERVICE_RULES[s.lower()], f"service {s[4:8].upper()}", mac
    return None, "", mac


async def scan(timeout: float = 8.0) -> list[Found]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for d, adv in found.values():
        name = adv.local_name or d.name or ""
        brand, reason, mac = identify(name, d.address, adv.service_uuids, adv.manufacturer_data)
        out.append(Found(d, name, d.address, adv.rssi or -999, brand, reason, mac, list(adv.service_uuids)))
    out.sort(key=lambda f: (f.brand is None, -f.rssi))
    return out


# --------------------------------------------------------------- drivers
class Driver:
    """Common interface the GUI talks to."""
    brand = ""
    label = ""
    supports_continuous = False
    continuous_note = ""
    density_range = (0, 15)
    default_density = 10
    uses_gap_speed = True
    fixed_head_mm: float | None = None
    supports_fit_length = False   # label length can follow the text (continuous tape)
    pause_between_labels = False  # prompt the user between labels when not printing continuously

    def __init__(self, found: Found):
        self.found = found

    @property
    def is_connected(self):
        raise NotImplementedError

    async def connect(self, log):
        raise NotImplementedError

    async def disconnect(self):
        raise NotImplementedError

    @staticmethod
    def render(text, p):
        raise NotImplementedError

    @staticmethod
    def preview(img):
        """Image as it should be shown on screen (reading left to right)."""
        return img

    async def print_labels(self, images, p, log, progress, between=None):
        """between(done, total): awaited after each label but the last; return False to stop."""
        raise NotImplementedError


class NelkoDriver(Driver):
    brand = "nelko"
    label = "Nelko P21"
    supports_continuous = False
    continuous_note = "Continuous printing is disabled on the Nelko P21 until it has been tested."

    def __init__(self, found):
        super().__init__(found)
        self.p = BlePrinter(found.device)

    @property
    def is_connected(self):
        return self.p.is_connected

    async def connect(self, log):
        await self.p.connect()
        return f"MTU {self.p.mtu}"

    async def disconnect(self):
        await self.p.disconnect()

    @staticmethod
    def render(text, p):
        if p.get("qr"):
            dpmm = 203 / 25.4
            img = qrlabel.render_qr_label(text, head_dots_for(p["head_mm"]),
                                          int(round(p["feed_mm"] * dpmm)), margin=FEED_MARGIN_DOTS,
                                          show_text=p.get("qr_text", True))
            return img.rotate(90, expand=True)   # -> head_dots wide x feed_dots tall, like text
        return BlePrinter.render_text_label(text, p["head_mm"], p["feed_mm"])

    @staticmethod
    def preview(img):
        return img.rotate(-90, expand=True)

    async def print_labels(self, images, p, log, progress, between=None):
        total = len(images)
        progress(0, total)
        for i, img in enumerate(images, 1):
            await self.p.print_image(img, p["head_mm"], p["feed_mm"], gap_mm=p["gap_mm"],
                                     density=p["density"], speed=p["speed"])
            progress(i, total)
            log(f"  printed {i}/{total}")
            await asyncio.sleep(max(1.2, p["feed_mm"] / 25.0 + 0.9))


class SupvanDriver(Driver):
    brand = "supvan"
    label = "Supvan E10"
    supports_continuous = True
    continuous_note = "Send every label in one job so they print back to back on the tape."
    density_range = (1, 7)
    default_density = 4
    uses_gap_speed = False
    fixed_head_mm = 12.0
    supports_fit_length = True
    pause_between_labels = True

    def __init__(self, found):
        super().__init__(found)
        self.p = supvan.SupvanPrinter(found.device)
        self.media = {"die_cut": False, "gap": 3}

    @property
    def is_connected(self):
        return self.p.is_connected

    async def connect(self, log):
        await self.p.connect()
        try:
            model = await self.p.name()
            self.label = f"Supvan {model}"
            s = await self.p.status()
            self.media = await self.p.media()
            log(f"{model}: battery {s.get('battery_v', '?')} V, tape {self.media.get('width_mm')} mm"
                f"{' die-cut' if self.media['die_cut'] else ' continuous'}"
                + (f" — {s['error']}" if s["error"] else ""))
        except Exception as e:  # status is informative only
            log(f"(status read failed: {e})")
        return self.label

    async def disconnect(self):
        await self.p.disconnect()

    @staticmethod
    def render(text, p):
        if p.get("qr"):
            width = None if p.get("fit_length") else int(round(p["feed_mm"] * supvan.DPMM))
            return qrlabel.render_qr_label(text, supvan.PRINT_DOTS, width, show_text=p.get("qr_text", True))
        return supvan.render_text_label(text, None if p.get("fit_length") else p["feed_mm"])

    async def print_labels(self, images, p, log, progress, between=None):
        density = max(1, min(7, p["density"]))
        gap = self.media.get("gap") or 3
        die = self.media.get("die_cut", False)
        if p.get("continuous"):
            await self.p.print_images(images, density, 1, die, gap, log=log, progress=progress)
            return
        total = len(images)
        progress(0, total)
        for i, img in enumerate(images, 1):
            await self.p.print_images([img], density, 1, die, gap, log=lambda m: None)
            progress(i, total)
            log(f"  printed {i}/{total}")
            if between and i < total and not await between(i, total):
                log(f"Stopped after {i} of {total} labels.")
                return


DRIVERS = {"nelko": NelkoDriver, "supvan": SupvanDriver}
BRAND_NAMES = {"nelko": "Nelko", "supvan": "Supvan"}
