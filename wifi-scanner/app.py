"""app.py - WiFi Scanner (mini NetSpot) — giao diện tkinter.

Chạy:  python app.py            (quét WiFi thật)
       python app.py --demo      (dữ liệu giả lập)
       python app.py --cli       (in bảng ra terminal 1 lần)
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import queue
import random
import threading
import time

import analysis
import charts
from scanner import backend_name, scan

COLUMNS = [  # (id, tiêu đề, độ rộng, căn lề)
    ("ssid", "SSID", 170, "w"), ("bssid", "BSSID (MAC)", 140, "w"), ("vendor", "Hãng", 130, "w"),
    ("pct", "Tín hiệu %", 90, "center"), ("dbm", "RSSI (dBm)", 95, "center"),
    ("channel", "Kênh", 55, "center"), ("band", "Băng tần", 80, "center"),
    ("freq", "Tần số MHz", 95, "center"), ("security", "Xác thực", 140, "w"),
    ("enc", "Mã hóa", 70, "w"), ("std", "Chuẩn", 85, "center"),
    ("first", "Thấy lần đầu", 105, "center"), ("last", "Thấy lần cuối", 105, "center"),
]
SURVEY_W, SURVEY_H = 760, 460

PARAM_HELP = """\
GIẢI THÍCH CÁC THÔNG SỐ

SSID — Tên mạng WiFi mà người dùng nhìn thấy. Mạng ẩn (hidden) không phát SSID nên hiện "(Hidden)".

BSSID — Địa chỉ MAC của chính điểm phát (AP/radio). Một router phát 2.4 GHz và 5 GHz sẽ có 2 BSSID khác nhau.
         Nếu bit thứ 2 của byte đầu = 1 thì đó là MAC cục bộ (virtual SSID / guest network), không tra được hãng.

Hãng (Vendor) — Tra từ 3 byte đầu của BSSID (OUI, do IEEE cấp) cho biết hãng sản xuất thiết bị.

Tín hiệu % — Chất lượng tín hiệu 0–100% do driver/hệ điều hành báo.
RSSI (dBm) — Công suất thu được. Quy đổi: dBm = % / 2 − 100. Số càng gần 0 càng mạnh.
         ≥ −50 rất tốt | −50…−60 tốt | −60…−70 khá | −70…−80 yếu | < −80 rất yếu / hay rớt kết nối.
         (dBm là thang logarithm: cứ giảm 3 dB thì công suất giảm một nửa, giảm 10 dB thì còn 1/10.)

Kênh (Channel) & Tần số — Mỗi kênh ứng với một tần số trung tâm.
         2.4 GHz: tần số = 2407 + 5×kênh (MHz), kênh 1–13, rộng ~22 MHz nên chỉ 1, 6, 11 là không chồng lấn.
         5 GHz: tần số = 5000 + 5×kênh, kênh 36–165, mỗi kênh 20 MHz không chồng lấn (nhưng có thể gộp thành 40/80/160 MHz).
         Nhiều AP cùng kênh hoặc kênh kề nhau => nhiễu đồng kênh / nhiễu kênh lân cận => chậm, rớt gói.

Băng tần (Band) — 2.4 GHz: xuyên tường tốt, phủ xa, nhưng đông và nhiễu. 5 GHz: nhanh hơn, ít nhiễu, phủ ngắn hơn.

Xác thực (Authentication) — Open (không mật khẩu) | WPA2-Personal (PSK) | WPA3-Personal (SAE) | WPA2-Enterprise (802.1X/RADIUS).
         Open và WEP/WPA đời đầu là không an toàn.
Mã hóa (Encryption) — CCMP (AES, an toàn) | TKIP (cũ, yếu) | None.

Chuẩn (PHY / 802.11) — 802.11b/g (2.4 GHz cũ) | n = WiFi 4 | ac = WiFi 5 | ax = WiFi 6/6E | be = WiFi 7.

Thấy lần đầu / lần cuối — Thời điểm tool bắt gặp AP này trong phiên chạy hiện tại.

BIỂU ĐỒ & GỢI Ý KÊNH
 • Tab "Kênh 2.4/5 GHz": mỗi AP là một hình chuông tại kênh của nó, độ cao = dBm.
 • Gợi ý kênh: cộng công suất (mW) của các AP gây chồng lấn lên từng kênh rồi đổi lại dBm; kênh nhiễu thấp nhất được đề xuất.
 • Tab "Survey": đo tín hiệu tại nhiều vị trí trên bản đồ, nội suy IDW (nghịch đảo khoảng cách bình phương) thành heatmap.
"""


class App:
    def __init__(self, root, demo=False):
        import tkinter as tk
        from tkinter import ttk
        self.tk, self.ttk = tk, ttk
        self.root = root
        root.title("WiFi Scanner — mini NetSpot")
        root.geometry("1200x740")

        self.demo = tk.BooleanVar(value=demo)
        self.auto = tk.BooleanVar(value=True)
        self.interval = tk.IntVar(value=5)
        self.band_filter = tk.StringVar(value="Tất cả")
        self.search = tk.StringVar()
        self.status = tk.StringVar(value="Sẵn sàng")
        self.survey_target = tk.StringVar()

        self.oui = analysis.Oui()
        self.aps = []
        self.by_bssid = {}
        self.first_seen, self.last_seen, self.names = {}, {}, {}
        self.history = collections.defaultdict(lambda: collections.deque(maxlen=600))
        self.sort_col, self.sort_rev = "dbm", True
        self.q = queue.Queue()
        self.scanning = False
        self._auto_job = None
        self._last_err = None
        self.floor_img = None
        self.survey_pts = []

        self._build_ui()
        self._poll()
        self.start_scan()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        bar = ttk.Frame(self.root, padding=6)
        bar.pack(fill="x")
        ttk.Button(bar, text="⟳ Quét ngay", command=self.start_scan).pack(side="left")
        ttk.Checkbutton(bar, text="Tự động quét mỗi", variable=self.auto, command=self._schedule_next).pack(side="left", padx=(10, 0))
        ttk.Spinbox(bar, from_=2, to=60, width=4, textvariable=self.interval).pack(side="left")
        ttk.Label(bar, text="giây").pack(side="left", padx=(2, 12))
        ttk.Label(bar, text="Băng tần:").pack(side="left")
        cb = ttk.Combobox(bar, width=8, state="readonly", textvariable=self.band_filter,
                          values=["Tất cả", "2.4 GHz", "5 GHz", "6 GHz"])
        cb.pack(side="left", padx=(2, 12))
        cb.bind("<<ComboboxSelected>>", lambda e: self._refresh_all())
        ttk.Label(bar, text="Tìm:").pack(side="left")
        ttk.Entry(bar, width=18, textvariable=self.search).pack(side="left", padx=(2, 12))
        self.search.trace_add("write", lambda *a: self._refresh_all())
        ttk.Checkbutton(bar, text="Chế độ Demo", variable=self.demo, command=self._on_demo_toggle).pack(side="left")
        ttk.Button(bar, text="Xuất JSON", command=lambda: self.export("json")).pack(side="right")
        ttk.Button(bar, text="Xuất CSV", command=lambda: self.export("csv")).pack(side="right", padx=4)
        ttk.Button(bar, text="Cập nhật DB hãng", command=self.update_oui).pack(side="right", padx=4)

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=6)

        # --- Tab Discover
        t1 = ttk.Frame(self.nb)
        self.nb.add(t1, text="Discover")
        wrap = ttk.Frame(t1)
        wrap.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(wrap, columns=[c[0] for c in COLUMNS], show="headings", selectmode="extended")
        for cid, title, width, anchor in COLUMNS:
            self.tree.heading(cid, text=title, command=lambda c=cid: self._sort_by(c))
            self.tree.column(cid, width=width, anchor=anchor, stretch=False)
        ys = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        xs = ttk.Scrollbar(wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ys.grid(row=0, column=1, sticky="ns")
        xs.grid(row=1, column=0, sticky="ew")
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        self.tree.tag_configure("good", foreground="#1a7f37")
        self.tree.tag_configure("mid", foreground="#b26a00")
        self.tree.tag_configure("bad", foreground="#c62828")
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._on_select())
        box = ttk.LabelFrame(t1, text="Chi tiết mạng đang chọn", padding=6)
        box.pack(fill="x", pady=(6, 4))
        self.detail = ttk.Label(box, text="Chọn một mạng ở bảng trên.", justify="left", font=("Consolas", 10))
        self.detail.pack(anchor="w")

        # --- Tab kênh 2.4 / 5
        self.cv24 = self._canvas_tab("Kênh 2.4 GHz")
        self.cv5 = self._canvas_tab("Kênh 5 GHz")
        self.rec_label = ttk.Label(self.root, text="", justify="left", foreground="#0b5394", wraplength=1150)
        self.rec_label.pack(fill="x", padx=8, pady=(2, 0))
        # --- Tab thời gian
        self.cvt = self._canvas_tab("Tín hiệu theo thời gian")

        # --- Tab Survey
        t5 = ttk.Frame(self.nb)
        self.nb.add(t5, text="Survey (heatmap)")
        sb = ttk.Frame(t5, padding=4)
        sb.pack(fill="x")
        ttk.Label(sb, text="Mạng cần đo:").pack(side="left")
        self.survey_combo = ttk.Combobox(sb, width=26, state="readonly", textvariable=self.survey_target)
        self.survey_combo.pack(side="left", padx=4)
        self.survey_combo.bind("<<ComboboxSelected>>", lambda e: self._clear_survey())
        ttk.Button(sb, text="Mở bản đồ (PNG/GIF)…", command=self.load_floor).pack(side="left", padx=4)
        ttk.Button(sb, text="Xóa điểm đo", command=self._clear_survey).pack(side="left")
        ttk.Label(sb, text="  Đứng tại vị trí rồi bấm lên bản đồ để đo.", foreground="#666").pack(side="left")
        self.cvs = tk.Canvas(t5, width=SURVEY_W, height=SURVEY_H, bg="white", highlightthickness=1,
                             highlightbackground="#ccc")
        self.cvs.pack(pady=4)
        self.cvs.bind("<Button-1>", self._survey_click)
        charts.draw_heatmap(self.cvs, None, [], SURVEY_W, SURVEY_H)

        # --- Tab giải thích
        t6 = ttk.Frame(self.nb)
        self.nb.add(t6, text="Giải thích thông số")
        txt = tk.Text(t6, wrap="word", font=("Segoe UI", 10), padx=10, pady=8)
        txt.insert("1.0", PARAM_HELP)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True)

        ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w", padding=3).pack(fill="x", side="bottom")

    def _canvas_tab(self, title):
        cv = self.tk.Canvas(self.nb, bg="white", highlightthickness=0)
        self.nb.add(cv, text=title)
        cv.bind("<Configure>", lambda e: self._redraw_charts())
        return cv

    # ------------------------------------------------------------------ quét
    def start_scan(self):
        if self.scanning:
            return
        self.scanning = True
        self.status.set("Đang quét…")
        demo = self.demo.get()

        def work():
            try:
                self.q.put(("ok", scan(demo)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    def _poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "ok":
                    self.scanning = False
                    self._on_scan_ok(data)
                    self._schedule_next()
                elif kind == "err":
                    self.scanning = False
                    self.status.set("Lỗi: " + data.splitlines()[0])
                    if data != self._last_err:
                        self._last_err = data
                        from tkinter import messagebox
                        messagebox.showerror("Không quét được", data)
                    self._schedule_next()
                elif kind == "survey":
                    x, y, dbm, ssid = data
                    if dbm is None:
                        self.status.set(f"Không thấy mạng '{ssid}' tại vị trí này.")
                    else:
                        self.survey_pts.append((x, y, dbm))
                        self.status.set(f"Đã đo {len(self.survey_pts)} điểm — vị trí ({x},{y}): {dbm} dBm")
                        charts.draw_heatmap(self.cvs, self.floor_img, self.survey_pts, SURVEY_W, SURVEY_H)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _schedule_next(self):
        if self._auto_job:
            self.root.after_cancel(self._auto_job)
            self._auto_job = None
        if self.auto.get():
            try:
                sec = max(2, int(self.interval.get()))
            except Exception:  # noqa: BLE001
                sec = 5
            self._auto_job = self.root.after(sec * 1000, self.start_scan)

    def _on_demo_toggle(self):
        self.history.clear()
        self.first_seen.clear()
        self._clear_survey()
        self.start_scan()

    def _on_scan_ok(self, aps):
        now, stamp = time.time(), time.strftime("%H:%M:%S")
        self.by_bssid = {a.bssid: a for a in aps}
        self.aps = list(self.by_bssid.values())
        for a in self.aps:
            self.first_seen.setdefault(a.bssid, stamp)
            self.last_seen[a.bssid] = stamp
            self.history[a.bssid].append((now, a.dbm))
            self.names[a.bssid] = a.ssid
        ssids = sorted({a.ssid for a in self.aps})
        self.survey_combo["values"] = ssids
        if not self.survey_target.get() and self.aps:
            self.survey_target.set(max(self.aps, key=lambda a: a.dbm).ssid)
        self._refresh_all()
        n24 = sum(a.band == "2.4 GHz" for a in self.aps)
        n5 = sum(a.band == "5 GHz" for a in self.aps)
        self.status.set(f"{len(self.aps)} AP | {len(ssids)} SSID | 2.4 GHz: {n24} | 5 GHz: {n5} | "
                        f"Cập nhật {stamp} | Backend: {backend_name(self.demo.get())}")

    # ------------------------------------------------------------------ bảng
    def _filtered(self):
        band, s = self.band_filter.get(), self.search.get().strip().lower()
        return [a for a in self.aps
                if (band == "Tất cả" or a.band == band) and (not s or s in a.ssid.lower() or s in a.bssid)]

    def _sort_key(self, a):
        return {"ssid": a.ssid.lower(), "bssid": a.bssid, "vendor": self.oui.lookup(a.bssid).lower(),
                "pct": a.signal_pct, "dbm": a.dbm, "channel": a.channel, "band": a.freq_mhz,
                "freq": a.freq_mhz, "security": a.security, "enc": a.encryption, "std": a.standard,
                "first": self.first_seen.get(a.bssid, ""), "last": self.last_seen.get(a.bssid, "")}[self.sort_col]

    def _sort_by(self, col):
        self.sort_rev = not self.sort_rev if col == self.sort_col else (col in ("pct", "dbm"))
        self.sort_col = col
        self._refresh_table()

    def _refresh_table(self):
        sel = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for a in sorted(self._filtered(), key=self._sort_key, reverse=self.sort_rev):
            self.tree.insert("", "end", iid=a.bssid, tags=(analysis.quality_tag(a.dbm),), values=(
                ("● " if a.connected else "") + a.ssid, a.bssid, self.oui.lookup(a.bssid), f"{a.signal_pct}%",
                a.dbm, a.channel, a.band, a.freq_mhz, a.security, a.encryption, a.standard,
                self.first_seen.get(a.bssid, ""), self.last_seen.get(a.bssid, "")))
        keep = [i for i in sel if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)

    def _refresh_all(self):
        self._refresh_table()
        self._redraw_charts()
        self._on_select(redraw=False)

    def _on_select(self, redraw=True):
        sel = self.tree.selection()
        if not sel or sel[0] not in self.by_bssid:
            self.detail.configure(text="Chọn một mạng ở bảng trên (giữ Ctrl để chọn nhiều mạng và so sánh ở tab 'Tín hiệu theo thời gian').")
        else:
            a = self.by_bssid[sel[0]]
            if a.band == "2.4 GHz":
                same = [b for b in self.aps if b is not a and b.band == a.band and abs(b.channel - a.channel) < 5]
                note = "(kênh lân cận trong ±4 kênh cũng gây chồng lấn)"
            else:
                same = [b for b in self.aps if b is not a and b.band == a.band and b.channel == a.channel]
                note = "(cùng kênh)"
            self.detail.configure(text=(
                f"SSID      : {a.ssid}{'  [đang kết nối]' if a.connected else ''}\n"
                f"BSSID     : {a.bssid}   Hãng: {self.oui.lookup(a.bssid)}\n"
                f"Tín hiệu  : {a.signal_pct}% ≈ {a.dbm} dBm  →  {analysis.quality(a.dbm)}\n"
                f"Kênh      : {a.channel}  ({a.freq_mhz} MHz, {a.band})   Chuẩn: {a.standard}\n"
                f"Bảo mật   : {a.security} / {a.encryption}\n"
                f"Chồng lấn : {len(same)} mạng khác {note}"))
        if redraw:
            self._redraw_charts()

    # ------------------------------------------------------------------ biểu đồ
    def _redraw_charts(self):
        charts.draw_channel_chart(self.cv24, self.aps, "2.4 GHz")
        charts.draw_channel_chart(self.cv5, self.aps, "5 GHz")
        keys = [k for k in self.tree.selection() if k in self.history]
        if not keys:
            keys = [a.bssid for a in sorted(self.aps, key=lambda a: -a.dbm)[:6]]
        charts.draw_time_chart(self.cvt, self.history, self.names, keys)
        self.rec_label.configure(text="\n".join(analysis.recommend(self.aps)) if self.aps else "")

    # ------------------------------------------------------------------ survey
    def load_floor(self):
        from tkinter import filedialog, messagebox
        path = filedialog.askopenfilename(filetypes=[("Ảnh PNG/GIF", "*.png *.gif"), ("Tất cả", "*.*")])
        if not path:
            return
        try:
            img = self.tk.PhotoImage(file=path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Lỗi", f"Không mở được ảnh (chỉ hỗ trợ PNG/GIF): {e}")
            return
        f = max(math.ceil(img.width() / SURVEY_W), math.ceil(img.height() / SURVEY_H), 1)
        self.floor_img = img.subsample(f) if f > 1 else img
        self._clear_survey()

    def _clear_survey(self):
        self.survey_pts = []
        charts.draw_heatmap(self.cvs, self.floor_img, [], SURVEY_W, SURVEY_H)

    def _survey_click(self, ev):
        ssid = self.survey_target.get()
        if not ssid:
            self.status.set("Chưa có mạng nào để đo — hãy quét trước.")
            return
        demo = self.demo.get()
        self.status.set(f"Đang đo tại ({ev.x},{ev.y})…")

        def work():
            try:
                if demo:  # giả lập: AP đặt ở (30%, 40%) bản đồ, tín hiệu giảm theo khoảng cách
                    dist = math.hypot(ev.x - SURVEY_W * 0.3, ev.y - SURVEY_H * 0.4)
                    dbm = int(-35 - dist * 0.07 + random.uniform(-2, 2))
                else:
                    vals = [a.dbm for a in scan(False) if a.ssid == ssid]
                    dbm = max(vals) if vals else None
                self.q.put(("survey", (ev.x, ev.y, dbm, ssid)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("err", str(e)))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ xuất / OUI
    def export(self, fmt):
        from tkinter import filedialog, messagebox
        if not self.aps:
            messagebox.showinfo("Xuất dữ liệu", "Chưa có dữ liệu để xuất.")
            return
        name = f"wifi_scan_{time.strftime('%Y%m%d_%H%M%S')}.{fmt}"
        path = filedialog.asksaveasfilename(defaultextension="." + fmt, initialfile=name)
        if not path:
            return
        rows = []
        for a in sorted(self.aps, key=lambda a: -a.dbm):
            d = a.to_dict()
            d.update(vendor=self.oui.lookup(a.bssid), quality=analysis.quality(a.dbm),
                     first_seen=self.first_seen.get(a.bssid, ""), last_seen=self.last_seen.get(a.bssid, ""))
            rows.append(d)
        if fmt == "csv":
            with open(path, "w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig để Excel đọc đúng tiếng Việt
                w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        else:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=2)
        self.status.set(f"Đã xuất {len(rows)} mạng → {path}")

    def update_oui(self):
        self.status.set("Đang tải oui.txt từ IEEE…")

        def work():
            try:
                n = self.oui.update()
                self.q.put(("ok", self.aps)) if self.aps else None
                self.root.after(0, lambda: self.status.set(f"Đã nạp {n} hãng sản xuất."))
            except Exception as e:  # noqa: BLE001
                self.root.after(0, lambda: self.status.set(
                    f"Tải thất bại ({e}). Tải tay: {analysis.OUI_URL} rồi đặt cạnh app.py"))

        threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------------- CLI
def cli(demo: bool):
    oui = analysis.Oui()
    aps = sorted(scan(demo), key=lambda a: -a.dbm)
    print(f"{'SSID':<22}{'BSSID':<19}{'dBm':>5}{'%':>5}{'CH':>4}  {'Band':<8}{'Security':<18}{'Std':<9}Vendor")
    for a in aps:
        print(f"{a.ssid[:21]:<22}{a.bssid:<19}{a.dbm:>5}{a.signal_pct:>5}{a.channel:>4}  {a.band:<8}"
              f"{a.security[:17]:<18}{a.standard:<9}{oui.lookup(a.bssid)}")
    print()
    print("\n".join(analysis.recommend(aps)))


def main():
    p = argparse.ArgumentParser(description="WiFi Scanner (mini NetSpot)")
    p.add_argument("--demo", action="store_true", help="dùng dữ liệu giả lập")
    p.add_argument("--cli", action="store_true", help="in kết quả ra terminal, không mở GUI")
    args = p.parse_args()
    if args.cli:
        cli(args.demo)
        return
    import tkinter as tk
    root = tk.Tk()
    App(root, demo=args.demo)
    root.mainloop()


if __name__ == "__main__":
    main()
