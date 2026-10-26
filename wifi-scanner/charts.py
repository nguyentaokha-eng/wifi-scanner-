"""charts.py - Vẽ biểu đồ trực tiếp lên tkinter.Canvas (không cần matplotlib)."""
from __future__ import annotations

import time

from analysis import CH5, color_for, dbm_to_color

YMIN, YMAX = -100, -20
FONT = ("Segoe UI", 8)


def _size(canvas):
    canvas.delete("all")
    return max(canvas.winfo_width(), 400), max(canvas.winfo_height(), 250)


def _grid(canvas, w, h, ml, mr, mt, mb):
    for d in range(YMIN, YMAX + 1, 10):
        y = mt + (YMAX - d) / (YMAX - YMIN) * (h - mt - mb)
        canvas.create_line(ml, y, w - mr, y, fill="#e8e8e8")
        canvas.create_text(ml - 6, y, text=str(d), anchor="e", fill="#666", font=FONT)
    canvas.create_text(ml - 6, mt - 12, text="dBm", anchor="e", fill="#666", font=FONT)


def _y(d, h, mt, mb):
    d = max(YMIN, min(YMAX, d))
    return mt + (YMAX - d) / (YMAX - YMIN) * (h - mt - mb)


def draw_channel_chart(canvas, aps, band: str):
    """Biểu đồ kênh kiểu NetSpot: mỗi AP là một 'cái chuông' đặt tại kênh của nó, cao theo dBm.
    2.4 GHz: chuông rộng ±2 kênh (22 MHz) nên các AP cạnh nhau chồng lên nhau = nhiễu."""
    w, h = _size(canvas)
    ml, mr, mt, mb = 48, 16, 30, 34
    if band == "2.4 GHz":
        chans = list(range(1, 14))
        pos = {c: c for c in chans}
        xlo, xhi, half = -1, 15, 2.0
    else:
        chans = CH5
        pos = {c: i for i, c in enumerate(chans)}
        xlo, xhi, half = -1, len(chans), 0.45

    def X(v):
        return ml + (v - xlo) / (xhi - xlo) * (w - ml - mr)

    canvas.create_text(w / 2, 12, text=f"Biểu đồ kênh {band} — các mạng chồng lên nhau = gây nhiễu cho nhau",
                       fill="#333", font=("Segoe UI", 10, "bold"))
    _grid(canvas, w, h, ml, mr, mt, mb)
    base = _y(YMIN, h, mt, mb)
    canvas.create_line(ml, base, w - mr, base, fill="#888")
    for c in chans:
        canvas.create_text(X(pos[c]), base + 12, text=str(c), fill="#333", font=FONT)
    canvas.create_text(w / 2, h - 6, text="Kênh (channel)", fill="#666", font=FONT)

    sel = sorted((a for a in aps if a.band == band and a.channel in pos), key=lambda a: a.dbm)
    if not sel:
        canvas.create_text(w / 2, h / 2, text="Không có mạng nào ở băng tần này", fill="#999", font=("Segoe UI", 11))
        return
    for a in sel:
        col = color_for(a.bssid)
        c0 = pos[a.channel]
        pts = []
        steps = 24
        for i in range(steps + 1):
            t = -half + 2 * half * i / steps
            if band == "2.4 GHz":
                f = 1 - (t / half) ** 2                    # parabol
            else:
                f = 1.0 if abs(t) < half * 0.8 else max(0.0, 1 - (abs(t) - half * 0.8) / (half * 0.2))
            v = YMIN + (a.dbm - YMIN) * f
            pts += [X(c0 + t), _y(v, h, mt, mb)]
        canvas.create_polygon(pts, fill=col, stipple="gray25", outline=col, width=2)
        canvas.create_text(X(c0), _y(a.dbm, h, mt, mb) - 4, text=a.ssid[:16], anchor="s", fill=col, font=FONT)


def draw_time_chart(canvas, history, names, keys, window=300):
    """Tín hiệu (dBm) của các AP đã chọn theo thời gian, cửa sổ `window` giây gần nhất."""
    w, h = _size(canvas)
    ml, mr, mt, mb = 48, 16, 30, 34
    canvas.create_text(w / 2, 12, text="Cường độ tín hiệu theo thời gian (chọn dòng ở tab Discover để so sánh)",
                       fill="#333", font=("Segoe UI", 10, "bold"))
    _grid(canvas, w, h, ml, mr, mt, mb)
    now = time.time()

    def X(t):
        return ml + (1 - (now - t) / window) * (w - ml - mr)

    for s in range(0, window + 1, 60):
        x = X(now - s)
        canvas.create_line(x, mt, x, h - mb, fill="#f0f0f0")
        canvas.create_text(x, h - mb + 12, text="bây giờ" if s == 0 else f"-{s // 60} phút", fill="#666",
                           font=FONT, anchor="e" if s == 0 else "center")
    if not keys:
        canvas.create_text(w / 2, h / 2, text="Chưa có dữ liệu", fill="#999", font=("Segoe UI", 11))
        return
    ly = mt + 6
    for k in keys:
        pts = [(t, d) for t, d in history.get(k, []) if now - t <= window]
        if not pts:
            continue
        col = color_for(k)
        flat = []
        for t, d in pts:
            flat += [X(t), _y(d, h, mt, mb)]
        if len(pts) > 1:
            canvas.create_line(flat, fill=col, width=2)
        for i in range(0, len(flat), 2):
            canvas.create_oval(flat[i] - 2, flat[i + 1] - 2, flat[i] + 2, flat[i + 1] + 2, fill=col, outline=col)
        canvas.create_rectangle(ml + 8, ly, ml + 18, ly + 10, fill=col, outline=col)
        canvas.create_text(ml + 24, ly + 5, anchor="w", font=FONT, fill="#333",
                           text=f"{names.get(k, '?')} ({k[-5:]})  {pts[-1][1]} dBm")
        ly += 15


def draw_heatmap(canvas, floor_img, pts, W, H, step=16):
    """Heatmap khảo sát (Survey): nội suy IDW (nghịch đảo khoảng cách bình phương) từ các điểm đo."""
    canvas.delete("all")
    if floor_img is not None:
        canvas.create_image(0, 0, anchor="nw", image=floor_img)
    else:
        canvas.create_rectangle(0, 0, W, H, fill="white", outline="")
        for x in range(0, W, 50):
            canvas.create_line(x, 0, x, H, fill="#eee")
        for y in range(0, H, 50):
            canvas.create_line(0, y, W, y, fill="#eee")
    if pts:
        stip = "gray50" if floor_img is not None else ""
        for cy in range(0, H, step):
            for cx in range(0, W, step):
                px, py = cx + step / 2, cy + step / 2
                sw = sv = 0.0
                for x, y, d in pts:
                    wgt = 1.0 / ((px - x) ** 2 + (py - y) ** 2 + 1.0)
                    sw += wgt
                    sv += wgt * d
                canvas.create_rectangle(cx, cy, cx + step, cy + step, fill=dbm_to_color(sv / sw),
                                        outline="", stipple=stip)
        for x, y, d in pts:
            canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="white", outline="black")
            canvas.create_text(x + 8, y - 8, text=f"{d}", anchor="w", font=("Segoe UI", 8, "bold"))
    else:
        canvas.create_text(W / 2, H / 2, text="Bấm vào bản đồ để đo tín hiệu tại vị trí đó",
                           fill="#888", font=("Segoe UI", 12))
    # chú thích thang màu
    lx, ly = W - 190, H - 24
    for i in range(160):
        canvas.create_line(lx + i, ly, lx + i, ly + 10, fill=dbm_to_color(-90 + 50 * i / 159))
    canvas.create_text(lx, ly + 18, text="-90", font=("Segoe UI", 8), anchor="w")
    canvas.create_text(lx + 160, ly + 18, text="-40 dBm", font=("Segoe UI", 8), anchor="e")
