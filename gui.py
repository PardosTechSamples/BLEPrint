#!/usr/bin/env python3
"""
BLEPrint - batch label printer GUI for Nelko(R) P21, Supvan(R) E10 and compatible BLE printers.

Open a CSV, build a label from its columns with a {Column} template,
preview any row, and print one row or the whole file over Bluetooth LE.

Compatible with the Nelko(R) P21 and the Supvan(R) / Katasymbol E10 (T10/T15 family).
"Nelko", "P21", "Supvan", "Katasymbol" and "E10" are trademarks of their owners, used only to
describe compatibility.

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
from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar,
    QPushButton, QPlainTextEdit, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

import printers


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
QLineEdit:disabled { color: #6b7a90; background: #0d1422; }
QCheckBox { spacing: 6px; }
QComboBox { background: #05080f; color: #e8eef7; border: 1px solid #2a3a52; border-radius: 4px; padding: 3px 8px; min-width: 130px; }
QComboBox QAbstractItemView { background: #121a2b; color: #e8eef7; selection-background-color: #1d4ed8; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #3b4d68; border-radius: 3px; background: #05080f; }
QCheckBox::indicator:checked { background: #3b82f6; border-color: #3b82f6; }
QCheckBox:disabled { color: #6b7a90; }
QCheckBox::indicator:disabled { background: #1a2333; border-color: #2a3a52; }
QTableWidget { background: #05080f; alternate-background-color: #0b1220; gridline-color: #1c2738; border: 1px solid #2a3a52; }
QTableWidget::item:selected { background: #1d4ed8; color: white; }
QHeaderView::section { background: #121a2b; color: #8b9bb4; border: none; padding: 4px 6px; }
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
    """Owns an asyncio loop + the connected printer driver; runs BLE coroutines."""

    sig_log = pyqtSignal(str)
    sig_conn = pyqtSignal(bool, str)      # connected?, detail
    sig_prog = pyqtSignal(int, int)       # value, maximum
    sig_busy = pyqtSignal(bool)
    sig_scan = pyqtSignal(object)         # list[printers.Found]
    sig_pause = pyqtSignal(int, int)      # labels done, total: ask the user to cut, then continue

    def __init__(self):
        super().__init__()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.driver: printers.Driver | None = None
        self._ready = False
        self._resume: asyncio.Future | None = None

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
        return self.driver is not None and self.driver.is_connected

    # ---- high-level ops (call from GUI thread) ----
    def scan(self, timeout=8.0):
        self._submit(self._scan(timeout))

    def connect(self, found):
        self._submit(self._connect(found))

    def disconnect(self):
        self._submit(self._disconnect())

    def print_jobs(self, images, params):
        self._submit(self._print_jobs(images, params))

    async def _scan(self, timeout):
        self.sig_log.emit(f"Searching for Bluetooth devices ({timeout:.0f} s)…")
        found = await printers.scan(timeout)
        known = sum(1 for f in found if f.brand)
        self.sig_log.emit(f"Found {len(found)} devices, {known} supported printer(s).")
        self.sig_scan.emit(found)

    async def _connect(self, found):
        if self.connected:
            await self._disconnect()
        cls = printers.DRIVERS.get(found.brand)
        if cls is None:
            self.sig_log.emit(f"“{found.name or found.address}” is not a supported printer.")
            return
        self.sig_busy.emit(True)
        self.sig_log.emit(f"Connecting to {found.name or found.address}…")
        drv = cls(found)
        detail = await drv.connect(self.sig_log.emit)
        self.driver = drv
        self.sig_conn.emit(True, detail or drv.label)
        self.sig_log.emit(f"Connected to {found.name} ({drv.label}).")
        self.sig_busy.emit(False)

    async def _disconnect(self):
        if self.driver:
            try:
                await self.driver.disconnect()
            finally:
                self.driver = None
        self.sig_conn.emit(False, "")
        self.sig_log.emit("Disconnected.")

    def resume(self, go_on: bool):
        """Answer a sig_pause prompt (call from the GUI thread)."""
        fut = self._resume
        if fut is not None and self.loop:
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(go_on))

    async def _between(self, done, total):
        self._resume = self.loop.create_future()
        self.sig_pause.emit(done, total)
        try:
            return await self._resume
        finally:
            self._resume = None

    async def _print_jobs(self, images, params):
        if not self.connected:
            self.sig_log.emit("Not connected — use Find printer… first.")
            return
        self.sig_busy.emit(True)
        pause = (self.driver.pause_between_labels and not params.get("continuous")
                 and len(images) > 1)
        try:
            await self.driver.print_labels(images, params, self.sig_log.emit, self.sig_prog.emit,
                                           between=self._between if pause else None)
            self.sig_log.emit("Done.")
        finally:
            self.sig_busy.emit(False)

    def shutdown(self):
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


# --------------------------------------------------------------- scan dialog
class ScanDialog(QDialog):
    """Search for Bluetooth devices and pick a printer. Brand comes from MAC prefix / name / service."""

    def __init__(self, parent, ble: BleWorker):
        super().__init__(parent)
        self.setWindowTitle("Find printer")
        self.resize(720, 420)
        self.ble = ble
        self.found: list = []
        self.selected = None

        v = QVBoxLayout(self)
        hint = QLabel("Turn the printer on and close its phone app, then search. "
                      "Supported printers are listed first; the brand is detected from the "
                      "MAC prefix (OUI), the device name or its Bluetooth service.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        v.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Name", "Brand", "Detected by", "MAC", "Signal"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._sel_changed)
        self.table.itemDoubleClicked.connect(lambda _: self._accept())
        v.addWidget(self.table, 1)

        bar = QHBoxLayout()
        self.chk_all = QCheckBox("Show all Bluetooth devices")
        self.chk_all.toggled.connect(self._fill)
        bar.addWidget(self.chk_all)
        bar.addStretch(1)
        self.status = QLabel("")
        self.status.setObjectName("muted")
        bar.addWidget(self.status)
        self.btn_scan = QPushButton("Search again")
        self.btn_scan.setObjectName("ghost")
        self.btn_scan.clicked.connect(self.start_scan)
        bar.addWidget(self.btn_scan)
        self.btn_ok = QPushButton("Connect")
        self.btn_ok.setObjectName("ok")
        self.btn_ok.setEnabled(False)
        self.btn_ok.clicked.connect(self._accept)
        bar.addWidget(self.btn_ok)
        v.addLayout(bar)

        ble.sig_scan.connect(self._on_scan)
        self.start_scan()

    def start_scan(self):
        self.btn_scan.setEnabled(False)
        self.status.setText("Searching…")
        self.ble.scan()

    def _on_scan(self, found):
        self.found = found
        self.btn_scan.setEnabled(True)
        n = sum(1 for f in found if f.brand)
        self.status.setText(f"{n} printer(s), {len(found)} devices")
        self._fill()

    def _rows(self):
        return [f for f in self.found if f.brand or self.chk_all.isChecked()]

    def _fill(self):
        rows = self._rows()
        self.table.setRowCount(len(rows))
        for r, f in enumerate(rows):
            cells = [f.name or "(no name)", printers.BRAND_NAMES.get(f.brand, "—"),
                     f.reason or "—", f.mac or ("hidden" if ":" not in f.address else f.address),
                     f"{f.rssi} dBm"]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if not f.brand:
                    it.setForeground(Qt.GlobalColor.gray)
                self.table.setItem(r, c, it)
        if rows and rows[0].brand:
            self.table.selectRow(0)
        self._sel_changed()

    def _current(self):
        rows = self._rows()
        idx = self.table.currentRow()
        return rows[idx] if 0 <= idx < len(rows) else None

    def _sel_changed(self):
        f = self._current()
        self.btn_ok.setEnabled(bool(f and f.brand))

    def _accept(self):
        f = self._current()
        if f and f.brand:
            self.selected = f
            self.accept()

    def done(self, r):
        try:
            self.ble.sig_scan.disconnect(self._on_scan)
        except TypeError:
            pass
        super().done(r)


# --------------------------------------------------------------- main window
class Main(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BLEPrint")
        self.resize(980, 660)

        self.rows: list[dict] = []
        self.columns: list[str] = []
        self.cur = 0
        self.settings = QSettings("BLEPrint", "BLEPrint")
        self.brand = self.settings.value("brand", "nelko")
        if self.brand not in printers.DRIVERS:
            self.brand = "nelko"
        self.found = None          # printers.Found picked in the scan dialog

        self.ble = BleWorker()
        self.ble.sig_log.connect(self.log)
        self.ble.sig_conn.connect(self.on_conn)
        self.ble.sig_prog.connect(self.on_prog)
        self.ble.sig_busy.connect(self.on_busy)
        self.ble.sig_pause.connect(self.on_pause)
        self.ble.start()

        self._build()
        self._build_menu()
        self.setStyleSheet(QSS_DARK)
        self._apply_brand(self.brand)
        self.log("Ready. Find your printer, open a CSV, edit the template, preview, then print.")
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
            "<h3>BLEPrint 0.2.0</h3>"
            "<p>Open-source batch label printer over Bluetooth LE.</p>"
            "<p><b>Compatible with the Nelko® P21 and the Supvan® / Katasymbol E10</b> "
            "(T10/T15 family) label printers.</p>"
            "<p>“Nelko”, “P21”, “Supvan”, “Katasymbol” and “E10” are trademarks of their "
            "respective owners, used here only to describe compatibility. BLEPrint is not "
            "affiliated with either company.</p>"
            "<p>The Supvan job encoding follows the MIT-licensed "
            "supvan-katasymbol-printer-web-ui project (© Katasymbol Web contributors).</p>"
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
        tagline = QLabel("for Nelko® P21 · Supvan® E10")
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
        self.dev_lbl = QLabel("No printer selected")
        self.dev_lbl.setObjectName("chip")
        top.addWidget(self.dev_lbl)
        self.btn_find = QPushButton("Find printer…")
        self.btn_find.setObjectName("ghost")
        self.btn_find.clicked.connect(self.find_printer)
        top.addWidget(self.btn_find)
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
        self.e_head = QLineEdit("14")
        self.e_feed = QLineEdit("40")
        self.e_gap = QLineEdit("2.0")
        self.e_density = QLineEdit("10")
        self.e_speed = QLineEdit("3")
        self.e_copies = QLineEdit("1")
        self.lbl_density = QLabel("Density (0-15)")
        fields = [("Width across head", self.e_head), ("Length along feed", self.e_feed),
                  ("Gap", self.e_gap), (self.lbl_density, self.e_density),
                  ("Speed", self.e_speed), ("Copies / row", self.e_copies)]
        for i, (name, w) in enumerate(fields):
            w.setFixedWidth(70)
            w.textChanged.connect(self._refresh_preview)
            form.addWidget(name if isinstance(name, QLabel) else QLabel(name), i // 2, (i % 2) * 2)
            form.addWidget(w, i // 2, (i % 2) * 2 + 1)
        lv.addLayout(form)
        self.chk_fit = QCheckBox("Fit length to text (print edge to edge)")
        self.chk_fit.setChecked(self.settings.value("fit_length", "true") == "true")
        self.chk_fit.toggled.connect(self._fit_toggled)
        lv.addWidget(self.chk_fit)
        qr_row = QHBoxLayout()
        qr_row.addWidget(QLabel("Print as"))
        self.cmb_mode = QComboBox()
        self.cmb_mode.addItems(["Text", "QR code + text", "QR code only"])
        self.cmb_mode.setToolTip("QR codes scan best with short text, such as a URL or SKU.")
        try:
            self.cmb_mode.setCurrentIndex(int(self.settings.value("print_mode", 0)))
        except (TypeError, ValueError):
            pass
        self.cmb_mode.currentIndexChanged.connect(self._mode_changed)
        qr_row.addWidget(self.cmb_mode)
        qr_row.addStretch(1)
        lv.addLayout(qr_row)
        self.chk_cont = QCheckBox("Continuous print (all labels in one job)")
        lv.addWidget(self.chk_cont)
        self.cont_note = QLabel("")
        self.cont_note.setObjectName("muted")
        self.cont_note.setWordWrap(True)
        lv.addWidget(self.cont_note)

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
        elif self.found is not None:
            self.ble.connect(self.found)
        else:
            self.find_printer()

    def find_printer(self):
        dlg = ScanDialog(self, self.ble)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected is not None:
            f = dlg.selected
            self.found = f
            self._apply_brand(f.brand)
            self.dev_lbl.setText(f"{printers.BRAND_NAMES[f.brand]} · {f.name or f.address}")
            self.log(f"Selected {f.name} — {printers.BRAND_NAMES[f.brand]} (detected by {f.reason})")
            self.ble.connect(f)

    def _apply_brand(self, brand):
        """Adjust the settings panel to what the selected printer understands."""
        self.brand = brand
        self.settings.setValue("brand", brand)
        cls = printers.DRIVERS[brand]
        lo, hi = cls.density_range
        self.lbl_density.setText(f"Density ({lo}-{hi})")
        try:
            d = int(float(self.e_density.text()))
        except ValueError:
            d = cls.default_density
        if not lo <= d <= hi:
            self.e_density.setText(str(cls.default_density))
        self.e_gap.setEnabled(False)            # set by the printer from the loaded label roll
        self.e_gap.setToolTip("Set by the printer from the loaded label roll.")
        self.e_speed.setEnabled(cls.uses_gap_speed)
        if cls.fixed_head_mm:
            if self.e_head.isEnabled():
                self._free_head = self.e_head.text()
            self.e_head.setText(f"{cls.fixed_head_mm:g}")
        elif not self.e_head.isEnabled():
            self.e_head.setText(getattr(self, "_free_head", "14"))
        self.e_head.setEnabled(cls.fixed_head_mm is None)
        self.chk_fit.setEnabled(cls.supports_fit_length)
        self.chk_fit.setToolTip("" if cls.supports_fit_length else "Only on continuous-tape printers (Supvan).")
        self.e_feed.setEnabled(not self._fit_on())
        self.chk_cont.setEnabled(cls.supports_continuous)
        if not cls.supports_continuous:
            self.chk_cont.setChecked(False)
        self.chk_cont.setToolTip(cls.continuous_note)
        note = cls.continuous_note
        if cls.pause_between_labels:
            note += " Without it, BLEPrint pauses after each label so you can cut it."
        self.cont_note.setText(note)
        if self.found is None:
            self.dev_lbl.setText(f"{printers.BRAND_NAMES[brand]} (not connected)")
        self._refresh_preview()

    def _fit_on(self):
        return self.chk_fit.isEnabled() and self.chk_fit.isChecked()

    def _fit_toggled(self, on):
        self.settings.setValue("fit_length", "true" if on else "false")
        self.e_feed.setEnabled(not self._fit_on())
        self._refresh_preview()

    def _mode_changed(self, idx):
        self.settings.setValue("print_mode", idx)
        self._refresh_preview()

    def on_pause(self, done, total):
        box = QMessageBox(self)
        box.setWindowTitle("Label printed")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(f"<b>Label {done} of {total} printed.</b>")
        box.setInformativeText("Cut it on the printer, then press Continue for the next label.")
        go = box.addButton("Continue", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Stop printing", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(go)
        QApplication.beep()
        box.exec()
        go_on = box.clickedButton() is go
        self.log(f"  label {done}/{total} done — {'continuing' if go_on else 'stopped by user'}")
        self.ble.resume(go_on)

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
        for b in (self.btn_one, self.btn_all, self.btn_conn, self.btn_find):
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
                    speed=i(self.e_speed, 3), copies=max(1, i(self.e_copies, 1)),
                    continuous=self.chk_cont.isEnabled() and self.chk_cont.isChecked(),
                    fit_length=self._fit_on(), qr=self.cmb_mode.currentIndex() > 0,
                    qr_text=self.cmb_mode.currentIndex() == 1)

    def _text_for(self, idx):
        template = self.tmpl.toPlainText().rstrip("\n")
        if self.rows and 0 <= idx < len(self.rows):
            row = self.rows[idx]
            for col in self.columns:
                template = template.replace("{" + col + "}", str(row.get(col, "")))
        return template or "Hello World!"

    def _render(self, idx):
        p = self._params()
        return printers.DRIVERS[self.brand].render(self._text_for(idx), p)

    def _refresh_preview(self):
        try:
            img = self._render(self.cur)
        except Exception as e:
            self.log("Preview error: " + str(e))
            return
        # Show the label reading left to right (the Nelko raster is stored sideways).
        disp = printers.DRIVERS[self.brand].preview(img)
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
                reader = csv.DictReader(fh, skipinitialspace=True)   # "Names, Model"
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
        if not self._check_printer():
            return
        img = self._render(self.cur)
        self.log(f"Printing row {self.cur + 1} ×{p['copies']}"
                 f"{' (continuous)' if p['continuous'] else ''}…")
        self.ble.print_jobs([img] * p["copies"], p)

    def print_all(self):
        if not self.rows:
            QMessageBox.information(self, "No data", "Open a CSV first.")
            return
        if not self._check_printer():
            return
        p = self._params()
        if QMessageBox.question(self, "Print all",
                                f"Print {len(self.rows)} rows × {p['copies']} copy each?") \
                != QMessageBox.StandardButton.Yes:
            return
        images = []
        for i in range(len(self.rows)):
            images += [self._render(i)] * p["copies"]
        self.log(f"Batch printing {len(self.rows)} rows ({len(images)} labels)"
                 f"{' as one continuous job' if p['continuous'] else ''}…")
        self.ble.print_jobs(images, p)

    def _check_printer(self):
        if self.ble.connected:
            return True
        QMessageBox.information(self, "No printer", "Connect to a printer first (Find printer…).")
        return False

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
