"""analysis.py - Các hàm phân tích: chất lượng tín hiệu, nhiễu kênh, gợi ý kênh, hãng sản xuất (OUI)."""
from __future__ import annotations

import math
import os
import re
import urllib.request
import zlib

PALETTE = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080",
           "#9a6324", "#800000", "#808000", "#000075", "#f032e6", "#469990"]

CH5 = [36, 40, 44, 48, 52, 56, 60, 64, 100, 104, 108, 112, 116, 120, 124, 128,
       132, 136, 140, 144, 149, 153, 157, 161, 165]
CH5_NON_DFS = [36, 40, 44, 48, 149, 153, 157, 161]


def color_for(key: str) -> str:
    """Màu cố định theo BSSID (crc32 để không đổi giữa các lần chạy)."""
    return PALETTE[zlib.crc32(key.encode()) % len(PALETTE)]


def quality(dbm: int) -> str:
    if dbm >= -50:
        return "Rất tốt"
    if dbm >= -60:
        return "Tốt"
    if dbm >= -70:
        return "Khá"
    if dbm >= -80:
        return "Yếu"
    return "Rất yếu"


def quality_tag(dbm: int) -> str:
    return "good" if dbm >= -60 else "mid" if dbm >= -75 else "bad"


_STOPS = [(-90, (215, 48, 39)), (-80, (244, 109, 67)), (-70, (254, 224, 139)),
          (-60, (166, 217, 106)), (-50, (26, 152, 80)), (-40, (0, 104, 55))]


def dbm_to_color(dbm: float) -> str:
    """Thang màu heatmap: đỏ (yếu) -> vàng -> xanh lá (mạnh)."""
    if dbm <= _STOPS[0][0]:
        r, g, b = _STOPS[0][1]
    elif dbm >= _STOPS[-1][0]:
        r, g, b = _STOPS[-1][1]
    else:
        for (d0, c0), (d1, c1) in zip(_STOPS, _STOPS[1:]):
            if d0 <= dbm <= d1:
                t = (dbm - d0) / (d1 - d0)
                r, g, b = (int(c0[i] + (c1[i] - c0[i]) * t) for i in range(3))
                break
    return f"#{r:02x}{g:02x}{b:02x}"


def _mw(dbm: float) -> float:
    return 10 ** (dbm / 10)


def interference_24(aps) -> dict[int, float | None]:
    """Mức nhiễu (dBm) lên từng kênh 1..13 của băng 2.4 GHz.

    Kênh 2.4 GHz rộng ~22 MHz nhưng cách nhau 5 MHz => hai kênh cách nhau < 5 số thì chồng lấn.
    Hệ số chồng lấn tuyến tính: cùng kênh = 1.0, cách 4 kênh = 0.2.
    """
    res: dict[int, float | None] = {}
    for c in range(1, 14):
        s = 0.0
        for a in aps:
            if a.band != "2.4 GHz":
                continue
            d = abs(a.channel - c)
            if d < 5:
                s += _mw(a.dbm) * (1 - d / 5)
        res[c] = 10 * math.log10(s) if s > 0 else None
    return res


def interference_5(aps) -> dict[int, float | None]:
    res: dict[int, float | None] = {}
    for c in CH5_NON_DFS:
        s = sum(_mw(a.dbm) for a in aps if a.band == "5 GHz" and a.channel == c)
        res[c] = 10 * math.log10(s) if s > 0 else None
    return res


def _best(scores: dict[int, float | None], candidates) -> int:
    return min(candidates, key=lambda c: (scores[c] is not None, scores[c] if scores[c] is not None else 0))


def _fmt(v) -> str:
    return "trống" if v is None else f"{v:.0f} dBm"


def recommend(aps) -> list[str]:
    lines = []
    if any(a.band == "2.4 GHz" for a in aps):
        sc = interference_24(aps)
        best = _best(sc, (1, 6, 11))
        detail = ", ".join(f"kênh {c}: {_fmt(sc[c])}" for c in (1, 6, 11))
        lines.append(f"2.4 GHz: nên dùng kênh {best} (nhiễu thấp nhất). Mức nhiễu — {detail}.")
    else:
        lines.append("2.4 GHz: không thấy mạng nào.")
    if any(a.band == "5 GHz" for a in aps):
        sc = interference_5(aps)
        best = _best(sc, CH5_NON_DFS)
        detail = ", ".join(f"{c}: {_fmt(sc[c])}" for c in CH5_NON_DFS)
        lines.append(f"5 GHz: nên dùng kênh {best}. Mức nhiễu — {detail}.")
    else:
        lines.append("5 GHz: không thấy mạng nào.")
    return lines


# ---------------------------------------------------------------- Hãng sản xuất (OUI)
OUI_URL = "https://standards-oui.ieee.org/oui/oui.txt"


class Oui:
    """Tra hãng sản xuất từ 3 byte đầu của BSSID. Cần file oui.txt của IEEE (bấm nút cập nhật trong app)."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "oui.txt")
        self.table: dict[str, str] = {}
        self.load()

    def load(self) -> int:
        self.table = {}
        if not os.path.exists(self.path):
            return 0
        rx = re.compile(r"^([0-9A-F]{2}-[0-9A-F]{2}-[0-9A-F]{2})\s+\(hex\)\s+(.+)$")
        with open(self.path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = rx.match(line.strip())
                if m:
                    self.table[m.group(1)] = m.group(2).strip()
        return len(self.table)

    def update(self) -> int:
        req = urllib.request.Request(OUI_URL, headers={"User-Agent": "Mozilla/5.0 wifi-scanner"})
        with urllib.request.urlopen(req, timeout=60) as r, open(self.path, "wb") as f:
            f.write(r.read())
        return self.load()

    def lookup(self, bssid: str) -> str:
        try:
            if int(bssid[:2], 16) & 0x02:  # bit "locally administered": MAC ảo/ngẫu nhiên, không có trong OUI
                return "(MAC cục bộ)"
        except ValueError:
            return "—"
        if not self.table:
            return "—"
        return self.table.get(bssid[:8].upper().replace(":", "-"), "?")
