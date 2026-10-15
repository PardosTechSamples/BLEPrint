#!/usr/bin/env python3
"""
BLEPrint - batch label printer GUI for Nelko(R) P21 and compatible BLE printers.

Open a CSV, build a label from its columns with a {Column} template,
preview any row, and print one row or the whole file over Bluetooth LE.

Compatible with the Nelko(R) P21 and similar JieLi-based BLE label printers.
"Nelko" and "P21" are trademarks of their owner, used to describe compatibility.

Run:  python gui.py        (launch from a Terminal so macOS can grant Bluetooth access)
Deps: pip install PyQt6 bleak pillow
"""

from __future__ import annotations

import asyncio
import csv
import io
import traceback
from pathlib import Path

from PIL import Image
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QPlainTextEdit, QSplitter, QVBoxLayout, QWidget,
)

from bleprint import BlePrinter


QSS_DARK = """
QMainWindow, QWidget { background: #0b1220; color: #e8eef7; font-size: 12px; }
QPushButton { background: #3b82f6; color: white; border: none; border-radius: 5px; padding: 6px 11px; font-weight: 600; }
QPushButton:hover { background: #1d4ed8; }
QPushButton:disabled { background: #2a3a52; color: #6b7a90; }
QPushButton#ghost { background: #243044; }
QPushButton#ghost:hover { background: #2e3f58; }
QPushButton#ok { background: #047857; }
QPushButton#ok:hover { background: #065f46; }
QPushButton#warn { background: #b45309; }
QPushButton#warn:hover { background: #92400e; }
QLineEdit, QPlainTextEdit { background: #05080f; color: #e8eef7; border: 1px solid #2a3a52; border-radius: 4px; padding: 4px 6px; }
QLabel#brand { font-size: 15px; font-weight: 800; }
QLabel#muted { color: #8b9bb4; font-size: 11px; }
QLabel#chip { background:#182234; border:1px solid #2a3a52; border-radius:4px; padding:3px 6px; }
QFrame#card { background: #121a2b; border: 1px solid #1c2738; border-radius: 8px; }
QPlainTextEdit#term { font-family: Menlo, Consolas, monospace; color: #86efac; background: #05100c; font-size: 11px; }
QLabel#preview { background: #05080f; border: 1px solid #2a3a52; border-radius: 6px; }
QProgressBar { background:#05080f; border:1px solid #2a3a52; border-radius:4px; text-align:center; color:#e8eef7; }
QProgressBar::chunk { background:#3b82f6; border-radius:3px; }
"""


# --------------------------------------------------------------- BLE worker
class BleWorker(QThread):
    """Owns an asyncio loop + a persistent BlePrinter; runs BLE coroutines."""

    sig_log = pyqtSignal(str)
    sig_conn = pyqtSignal(bool, str)      # connected?, detail
    sig_prog = pyqtSignal(int, int)       # value, maximum
    sig_busy = pyqtSignal(bool)

    def __init__(self):
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.printer: BlePrinter | None = None
        self._ready = False

    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self._ready = True
        self.loop.run_forever()

    def _submit(self, coro):
        while not self._ready:
            self.msleep(10)
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)

        def _cb(f):
            exc = f.exception()
            if exc:
                self.sig_log.emit("✖ " + "".join(
                    traceback.format_exception_only(type(exc), exc)).strip())
                self.sig_busy.emit(False)
        fut.add_done_callback(_cb)

    @property
    def connected(self):
        return self.printer is not None and self.printer.is_connected

    # ---- high-level ops (call from GUI thread) ----
    def connect(self):
        self._submit(self._connect())

    def disconnect(self):
        self._submit(self._disconnect())

    def print_jobs(self, jobs):
        """jobs: list of (pil_image, params-dict). Prints each, paced."""
        self._submit(self._print_jobs(jobs))

    async def _connect(self):
        if self.connected:
            return
        self.sig_busy.emit(True)
        self.sig_log.emit("Scanning for P21…")
        dev = await BlePrinter.discover("P21", timeout=12.0)
        if not dev:
            self.sig_log.emit("No P21 found (is it on, phone app closed?)")
            self.sig_busy.emit(False)
            return
        pr = BlePrinter(dev)
        await pr.connect()
        self.printer = pr
        self.sig_conn.emit(True, f"MTU {pr.mtu}")
        self.sig_log.emit(f"Connected to {dev.name} (MTU {pr.mtu}). LED should be blue.")
        self.sig_busy.emit(False)

    async def _disconnect(self):
        if self.printer:
            await self.printer.disconnect()
            self.printer = None
        self.sig_conn.emit(False, "")
        self.sig_log.emit("Disconnected.")

    async def _ensure(self):
        if not self.connected:
            await self._connect()
        return self.connected

    async def _print_jobs(self, jobs):
        self.sig_busy.emit(True)
        if not await self._ensure():
            self.sig_busy.emit(False)
            return
        total = len(jobs)
        self.sig_prog.emit(0, total)
        for i, (img, p) in enumerate(jobs, 1):
            await self.printer.print_image(
                img, p["head_mm"], p["feed_mm"], gap_mm=p["gap_mm"],
                density=p["density"], speed=p["speed"])
            self.sig_prog.emit(i, total)
            self.sig_log.emit(f"  printed {i}/{total}")
            await asyncio.sleep(_label_seconds(p))
        self.sig_log.emit("Done.")
        self.sig_busy.emit(False)

    def shutdown(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


def _label_seconds(p):
    return max(1.2, p["feed_mm"] / 25.0 + 0.9)


# --------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BLEPrint")
        self.resize(980, 660)

        self.rows: list[dict] = []
        self.columns: list[str] = []
        self.cur = 0

        self.ble = BleWorker()
        self.ble.sig_log.connect(self.log)
        self.ble.sig_conn.connect(self.on_conn)
        self.ble.sig_prog.connect(self.on_prog)
        self.ble.sig_busy.connect(self.on_busy)
        self.ble.start()

        self._build()
        self._build_menu()
        self.setStyleSheet(QSS_DARK)
        self.log("Ready. Open a CSV, edit the template, preview, then print.")
        self._refresh_preview()

    # ---- menu / about ----
    def _build_menu(self):
        # On macOS the "About BLEPrint" item is moved into the application menu
        # (Apple menu → BLEPrint → About BLEPrint) automatically via AboutRole.
        menubar = self.menuBar()
        app_menu = menubar.addMenu("BLEPrint")
        about = QAction("About BLEPrint", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self._show_about)
        app_menu.addAction(about)

    def _show_about(self):
        QMessageBox.about(
            self, "About BLEPrint",
            "<h3>BLEPrint 0.1.0</h3>"
            "<p>Open-source batch label printer over Bluetooth LE.</p>"
            "<p><b>Compatible with the Nelko® P21 label printer</b> "
            "(and similar JieLi-based BLE label printers).</p>"
            "<p>“Nelko” and “P21” are trademarks of their respective owner, "
            "used here only to describe compatibility.</p>"
            "<p>MIT License.</p>"
        )

    # ---- layout ----
    def _build(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)

        # top bar
        top = QHBoxLayout()
        brand = QLabel("BLEPrint")
        brand.setObjectName("brand")
        top.addWidget(brand)
        tagline = QLabel("for Nelko® P21")
        tagline.setObjectName("muted")
        top.addWidget(tagline)
        top.addStretch(1)
        self.file_lbl = QLabel("No CSV loaded")
        self.file_lbl.setObjectName("muted")
        top.addWidget(self.file_lbl)
        self.btn_open = QPushButton("Open CSV…")
        self.btn_open.setObjectName("ghost")
        self.btn_open.clicked.connect(self.open_csv)
        top.addWidget(self.btn_open)
        self.conn_dot = QLabel("● disconnected")
        self.conn_dot.setStyleSheet("color:#f87171; font-weight:700;")
        top.addWidget(self.conn_dot)
        self.btn_conn = QPushButton("Connect")
        self.btn_conn.setObjectName("ok")
        self.btn_conn.clicked.connect(self.toggle_conn)
        top.addWidget(self.btn_conn)
        outer.addLayout(top)

        split = QSplitter()
        outer.addWidget(split, 1)

        # left card
        left = QFrame()
        left.setObjectName("card")
        lv = QVBoxLayout(left)

        lv.addWidget(self._h("Label size (mm) & print settings"))
        form = QGridLayout()
        self.e_head = QLineEdit("15")
        self.e_feed = QLineEdit("40")
        self.e_gap = QLineEdit("2.0")
        self.e_density = QLineEdit("10")
        self.e_speed = QLineEdit("3")
        self.e_copies = QLineEdit("1")
        fields = [("Width across head", self.e_head), ("Length along feed", self.e_feed),
                  ("Gap", self.e_gap), ("Density (0-15)", self.e_density),
                  ("Speed", self.e_speed), ("Copies / row", self.e_copies)]
        for i, (name, w) in enumerate(fields):
            w.setFixedWidth(70)
            w.textChanged.connect(self._refresh_preview)
            form.addWidget(QLabel(name), i // 2, (i % 2) * 2)
            form.addWidget(w, i // 2, (i % 2) * 2 + 1)
        lv.addLayout(form)

        lv.addWidget(self._h("Template — click a column to insert {Column}"))
        self.cols_box = QHBoxLayout()
        self.cols_box.setSpacing(4)
        cols_wrap = QWidget()
        cols_wrap.setLayout(self.cols_box)
        lv.addWidget(cols_wrap)
        self.tmpl = QPlainTextEdit("{Name}\n{SKU}")
        self.tmpl.setFixedHeight(120)
        self.tmpl.textChanged.connect(self._refresh_preview)
        lv.addWidget(self.tmpl)

        nav = QHBoxLayout()
        b_prev = QPushButton("◀")
        b_prev.setObjectName("ghost")
        b_prev.setFixedWidth(40)
        b_prev.clicked.connect(lambda: self.goto(self.cur - 1))
        b_next = QPushButton("▶")
        b_next.setObjectName("ghost")
        b_next.setFixedWidth(40)
        b_next.clicked.connect(lambda: self.goto(self.cur + 1))
        self.row_lbl = QLabel("row 0 / 0")
        nav.addWidget(b_prev)
        nav.addWidget(self.row_lbl)
        nav.addWidget(b_next)
        nav.addStretch(1)
        lv.addLayout(nav)

        acts = QHBoxLayout()
        self.btn_one = QPushButton("Print this row")
        self.btn_one.clicked.connect(self.print_current)
        self.btn_all = QPushButton("Print ALL rows")
        self.btn_all.setObjectName("warn")
        self.btn_all.clicked.connect(self.print_all)
        acts.addWidget(self.btn_one)
        acts.addWidget(self.btn_all)
        lv.addLayout(acts)
        lv.addStretch(1)
        split.addWidget(left)

        # right card
        right = QFrame()
        right.setObjectName("card")
        rv = QVBoxLayout(right)
        rv.addWidget(self._h("Preview (as printed)"))
        self.preview = QLabel()
        self.preview.setObjectName("preview")
        self.preview.setFixedSize(360, 200)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        rv.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignHCenter)
        rv.addWidget(self._h("Log"))
        self.logw = QPlainTextEdit()
        self.logw.setObjectName("term")
        self.logw.setReadOnly(True)
        rv.addWidget(self.logw, 1)
        self.prog = QProgressBar()
        rv.addWidget(self.prog)
        split.addWidget(right)
        split.setSizes([560, 420])

    def _h(self, text):
        lab = QLabel(text)
        lab.setObjectName("muted")
        return lab

    # ---- logging / signals ----
    def log(self, msg):
        self.logw.appendPlainText(msg)

    def toggle_conn(self):
        if self.ble.connected:
            self.ble.disconnect()
        else:
            self.ble.connect()

    def on_conn(self, connected, detail):
        if connected:
            self.conn_dot.setText("● connected")
            self.conn_dot.setStyleSheet("color:#34d399; font-weight:700;")
            self.btn_conn.setText("Disconnect")
        else:
            self.conn_dot.setText("● disconnected")
            self.conn_dot.setStyleSheet("color:#f87171; font-weight:700;")
            self.btn_conn.setText("Connect")

    def on_prog(self, value, maximum):
        self.prog.setMaximum(max(1, maximum))
        self.prog.setValue(value)

    def on_busy(self, busy):
        for b in (self.btn_one, self.btn_all, self.btn_conn):
            b.setEnabled(not busy)

    # ---- params / rendering ----
    def _params(self):
        def f(w, d):
            try:
                return float(w.text())
            except Exception:
                return d
        def i(w, d):
            try:
                return int(float(w.text()))
            except Exception:
                return d
        return dict(head_mm=f(self.e_head, 15), feed_mm=f(self.e_feed, 40),
                    gap_mm=f(self.e_gap, 2.0), density=i(self.e_density, 10),
                    speed=i(self.e_speed, 3), copies=max(1, i(self.e_copies, 1)))

    def _text_for(self, idx):
        template = self.tmpl.toPlainText().rstrip("\n")
        if self.rows and 0 <= idx < len(self.rows):
            row = self.rows[idx]
            for col in self.columns:
                template = template.replace("{" + col + "}", str(row.get(col, "")))
        return template or "Hello World!"

    def _render(self, idx):
        p = self._params()
        return BlePrinter.render_text_label(self._text_for(idx), p["head_mm"], p["feed_mm"])

    def _refresh_preview(self):
        try:
            img = self._render(self.cur)
        except Exception as e:
            self.log("Preview error: " + str(e))
            return
        # Printed orientation reads along the label length (sideways on screen);
        # rotate the DISPLAY copy back to landscape so it's readable. Printing is
        # unchanged and still uses the properly-rotated raster from _render().
        disp = img.rotate(-90, expand=True)
        buf = io.BytesIO()
        disp.convert("L").save(buf, format="PNG")
        pix = QPixmap()
        pix.loadFromData(buf.getvalue(), "PNG")
        pix = pix.scaled(self.preview.width() - 8, self.preview.height() - 8,
                         Qt.AspectRatioMode.KeepAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pix)

    # ---- CSV ----
    def open_csv(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open CSV", "", "CSV files (*.csv);;All files (*)")
        if not path:
            return
        try:
            with open(path, newline="", encoding="utf-8-sig") as fh:
                reader = csv.DictReader(fh)
                self.columns = list(reader.fieldnames or [])
                self.rows = [dict(r) for r in reader]
        except Exception as e:
            QMessageBox.critical(self, "CSV error", str(e))
            return
        self.file_lbl.setText(f"{Path(path).name}  ({len(self.rows)} rows)")
        self.log(f"Loaded {len(self.rows)} rows · columns: {', '.join(self.columns)}")
        # clear + rebuild column chips
        while self.cols_box.count():
            item = self.cols_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for col in self.columns:
            b = QPushButton(col)
            b.setObjectName("ghost")
            b.clicked.connect(lambda _=False, c=col: self._insert(c))
            self.cols_box.addWidget(b)
        self.cols_box.addStretch(1)
        self.goto(0)

    def _insert(self, col):
        self.tmpl.insertPlainText("{" + col + "}")

    def goto(self, idx):
        n = len(self.rows)
        self.cur = max(0, min(idx, n - 1)) if n else 0
        self.row_lbl.setText(f"row {self.cur + 1 if n else 0} / {n}")
        self._refresh_preview()

    # ---- print ----
    def print_current(self):
        p = self._params()
        img = self._render(self.cur)
        jobs = [(img, p)] * p["copies"]
        self.log(f"Printing row {self.cur + 1} ×{p['copies']}…")
        self.ble.print_jobs(jobs)

    def print_all(self):
        if not self.rows:
            QMessageBox.information(self, "No data", "Open a CSV first.")
            return
        p = self._params()
        if QMessageBox.question(self, "Print all",
                                f"Print {len(self.rows)} rows × {p['copies']} copy each?") \
                != QMessageBox.StandardButton.Yes:
            return
        jobs = []
        for i in range(len(self.rows)):
            img = self._render(i)
            jobs += [(img, p)] * p["copies"]
        self.log(f"Batch printing {len(self.rows)} rows ({len(jobs)} labels)…")
        self.ble.print_jobs(jobs)

    def closeEvent(self, ev):
        try:
            self.ble.disconnect()
            self.ble.shutdown()
            self.ble.wait(1500)
        except Exception:
            pass
        super().closeEvent(ev)


def main():
    app = QApplication([])
    w = Main()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
