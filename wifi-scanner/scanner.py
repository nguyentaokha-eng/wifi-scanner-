"""scanner.py - Lấy danh sách WiFi xung quanh.

Hỗ trợ:
  * Windows : netsh wlan show networks mode=bssid  (+ WlanScan để ép quét lại)
  * Linux   : nmcli dev wifi list --rescan yes
  * Demo    : dữ liệu giả lập, dùng để test / quay video khi máy không có card WiFi
Chỉ yêu cầu card WiFi quét mạng xung quanh như hệ điều hành vẫn làm; không kết nối, không bẻ khóa, không bắt gói dữ liệu.
"""
from __future__ import annotations

import platform
import random
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass


@dataclass
class AccessPoint:
    ssid: str
    bssid: str
    signal_pct: int          # 0-100 %, đây là "Signal quality" mà Windows/NetworkManager báo
    channel: int
    band: str                # "2.4 GHz" | "5 GHz" | "6 GHz"
    freq_mhz: int
    security: str = "—"      # Authentication: Open / WPA2-Personal / WPA3-Personal ...
    encryption: str = "—"    # CCMP / TKIP / None
    standard: str = "—"      # 802.11n / ac / ax ...
    connected: bool = False

    @property
    def dbm(self) -> int:
        """Quy đổi % -> dBm theo công thức Windows/NetworkManager: dBm = %/2 - 100."""
        return int(round(self.signal_pct / 2 - 100))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["dbm"] = self.dbm
        return d


# ----------------------------------------------------------------- tiện ích kênh/tần số
def band_of_channel(ch: int) -> str:
    return "2.4 GHz" if ch <= 14 else "5 GHz"


def channel_to_freq(ch: int, band: str) -> int:
    if band == "2.4 GHz":
        return 2484 if ch == 14 else 2407 + 5 * ch
    if band == "5 GHz":
        return 5000 + 5 * ch
    return 5950 + 5 * ch  # 6 GHz


def freq_to_channel(freq: int) -> tuple[int, str]:
    if freq == 2484:
        return 14, "2.4 GHz"
    if 2400 <= freq < 2500:
        return (freq - 2407) // 5, "2.4 GHz"
    if 5000 <= freq < 5900:
        return (freq - 5000) // 5, "5 GHz"
    return (freq - 5950) // 5, "6 GHz"


# ----------------------------------------------------------------- Windows
_RE_SSID = re.compile(r"^\s*SSID\s+\d+\s*:\s?(.*)$", re.I)
_RE_BSSID = re.compile(r"^\s*BSSID\s+\d+\s*:\s*([0-9a-fA-F:]{17})")
_RE_KV = re.compile(r"^\s*[^:]+?\s*:\s*(.*)$")
_RE_BAND = re.compile(r"(2\.4|5|6)\s*GHz", re.I)


def parse_netsh(text: str, connected_bssid: str | None = None) -> list[AccessPoint]:
    """Parse output của `netsh wlan show networks mode=bssid`.

    Parse theo *vị trí + dạng giá trị* chứ không dựa vào tên nhãn, nên vẫn chạy
    khi Windows để ngôn ngữ khác tiếng Anh.
    """
    aps: list[AccessPoint] = []
    ssid: str | None = None
    net: list[str] = []          # [Network type, Authentication, Encryption]
    cur: dict | None = None

    def flush():
        if cur and "pct" in cur and "ch" in cur:
            ch = cur["ch"]
            band = cur.get("band") or band_of_channel(ch)
            aps.append(AccessPoint(
                ssid=cur["ssid"], bssid=cur["bssid"], signal_pct=cur["pct"], channel=ch,
                band=band, freq_mhz=channel_to_freq(ch, band),
                security=cur["auth"], encryption=cur["enc"], standard=cur.get("std", "—"),
                connected=bool(connected_bssid and cur["bssid"] == connected_bssid)))

    for line in text.splitlines():
        m = _RE_SSID.match(line)
        if m:
            flush()
            cur = None
            ssid = m.group(1).strip() or "(Hidden)"
            net = []
            continue
        m = _RE_BSSID.match(line)
        if m and ssid is not None:
            flush()
            cur = {"ssid": ssid, "bssid": m.group(1).lower(),
                   "auth": net[1] if len(net) > 1 else "—",
                   "enc": net[2] if len(net) > 2 else "—"}
            continue
        m = _RE_KV.match(line)
        if not m or ssid is None:
            continue
        val = m.group(1).strip()
        if cur is None:
            net.append(val)
        elif re.fullmatch(r"\d{1,3}\s*%", val):
            cur["pct"] = int(val.rstrip("% ").strip())
        elif re.fullmatch(r"802\.11\w+", val):
            cur["std"] = val
        elif _RE_BAND.fullmatch(val):
            cur["band"] = _RE_BAND.fullmatch(val).group(1) + " GHz"
        elif re.fullmatch(r"\d{1,3}", val) and "ch" not in cur:
            cur["ch"] = int(val)
    flush()
    return aps


def _run(cmd, **kw) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=25, **kw)
    return (r.stdout or "") + (r.stderr or "")


def _win_trigger_scan() -> None:
    """Gọi WlanScan (wlanapi.dll) để Windows quét lại ngay, nếu không netsh chỉ trả kết quả cache."""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class WLAN_INTERFACE_INFO(ctypes.Structure):
            _fields_ = [("InterfaceGuid", GUID),
                        ("strInterfaceDescription", ctypes.c_wchar * 256),
                        ("isState", wintypes.DWORD)]

        class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
            _fields_ = [("dwNumberOfItems", wintypes.DWORD), ("dwIndex", wintypes.DWORD),
                        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1)]

        api = ctypes.windll.wlanapi
        client = wintypes.HANDLE()
        negotiated = wintypes.DWORD()
        if api.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(client)) != 0:
            return
        p_list = ctypes.c_void_p()
        if api.WlanEnumInterfaces(client, None, ctypes.byref(p_list)) == 0 and p_list.value:
            lst = ctypes.cast(p_list, ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)).contents
            infos = (WLAN_INTERFACE_INFO * lst.dwNumberOfItems).from_address(
                ctypes.addressof(lst.InterfaceInfo))
            for info in infos:
                api.WlanScan(client, ctypes.byref(info.InterfaceGuid), None, None, None)
            api.WlanFreeMemory(p_list)
        api.WlanCloseHandle(client, None)
        time.sleep(2.0)  # chờ card WiFi quét xong
    except Exception:
        pass  # không ép quét được thì vẫn dùng kết quả cache của netsh


def scan_windows() -> list[AccessPoint]:
    _win_trigger_scan()
    out = _run("chcp 65001 >nul & netsh wlan show networks mode=bssid", shell=True)
    if "BSSID" not in out.upper():
        raise RuntimeError(
            "Không đọc được danh sách WiFi.\n"
            "Kiểm tra: (1) máy có card WiFi và WiFi đang bật, "
            "(2) Settings > Privacy > Location đang bật (Windows 11 yêu cầu), "
            "(3) dịch vụ WLAN AutoConfig đang chạy.\n\nnetsh trả về:\n" + out.strip()[:300])
    connected = None
    iface = _run("chcp 65001 >nul & netsh wlan show interfaces", shell=True)
    for line in iface.splitlines():
        m = re.match(r"^\s*BSSID\s*:\s*([0-9a-fA-F:]{17})", line)
        if m:
            connected = m.group(1).lower()
            break
    return parse_netsh(out, connected)


# ----------------------------------------------------------------- Linux
def _split_terse(line: str) -> list[str]:
    """Tách dòng `nmcli -t` (dấu ':' trong giá trị được escape bằng '\\:')."""
    parts, cur, esc = [], "", False
    for ch in line:
        if esc:
            cur += ch
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def parse_nmcli(text: str) -> list[AccessPoint]:
    aps = []
    for line in text.splitlines():
        f = _split_terse(line)
        if len(f) < 8:
            continue
        in_use, ssid, bssid, chan, freq, _rate, sig, sec = f[:8]
        try:
            freq_mhz = int(freq.split()[0])
            ch, band = freq_to_channel(freq_mhz)
            pct = int(sig)
        except (ValueError, IndexError):
            continue
        sec = sec.strip()
        aps.append(AccessPoint(
            ssid=ssid or "(Hidden)", bssid=bssid.lower(), signal_pct=pct,
            channel=int(chan) if chan.isdigit() else ch, band=band, freq_mhz=freq_mhz,
            security=sec if sec and sec != "--" else "Open", encryption="—",
            standard="—", connected=in_use.strip() == "*"))
    return aps


def scan_linux() -> list[AccessPoint]:
    if not shutil.which("nmcli"):
        raise RuntimeError("Không tìm thấy nmcli. Cài NetworkManager: sudo apt install network-manager")
    cmd = ["nmcli", "-t", "-f", "IN-USE,SSID,BSSID,CHAN,FREQ,RATE,SIGNAL,SECURITY",
           "dev", "wifi", "list", "--rescan", "yes"]
    out = _run(cmd)
    aps = parse_nmcli(out)
    if not aps:
        raise RuntimeError("nmcli không trả về mạng nào (máy có WiFi chưa? đã bật WiFi chưa?).\n" + out.strip()[:300])
    return aps


# ----------------------------------------------------------------- Demo
_DEMO: list[dict] | None = None


def scan_demo() -> list[AccessPoint]:
    global _DEMO
    if _DEMO is None:
        raw = [
            ("VNPT_Home_2G", "a4:2b:b0:11:22:01", 1, "2.4 GHz", "WPA2-Personal", "CCMP", "802.11n", -48),
            ("FPT_Telecom", "c0:4a:00:aa:bb:06", 6, "2.4 GHz", "WPA2-Personal", "CCMP", "802.11n", -58),
            ("Viettel_Net", "e8:de:27:12:34:0b", 11, "2.4 GHz", "WPA2-Personal", "CCMP", "802.11n", -66),
            ("Cafe_Free_WiFi", "00:1a:2b:3c:4d:06", 6, "2.4 GHz", "Open", "None", "802.11g", -72),
            ("TP-Link_A1B2", "50:c7:bf:a1:b2:03", 3, "2.4 GHz", "WPA2-Personal", "CCMP", "802.11n", -78),
            ("Phong_203", "dc:fe:18:00:00:0b", 11, "2.4 GHz", "WPA3-Personal", "CCMP", "802.11ax", -83),
            ("VNPT_Home_5G", "a4:2b:b0:11:22:02", 36, "5 GHz", "WPA2-Personal", "CCMP", "802.11ac", -55),
            ("FPT_Telecom_5G", "c0:4a:00:aa:bb:07", 44, "5 GHz", "WPA2-Personal", "CCMP", "802.11ac", -63),
            ("Viettel_5G", "e8:de:27:12:34:0c", 149, "5 GHz", "WPA3-Personal", "CCMP", "802.11ax", -70),
            ("KTX_Student", "00:0c:42:55:66:01", 157, "5 GHz", "WPA2-Enterprise", "CCMP", "802.11ac", -76),
            ("Quan_Tra_Sua", "9c:a2:f4:77:88:36", 36, "5 GHz", "WPA2-Personal", "CCMP", "802.11ac", -81),
            ("(Hidden)", "3a:5b:6c:7d:8e:9f", 9, "2.4 GHz", "WPA2-Personal", "CCMP", "802.11n", -88),
        ]
        _DEMO = [dict(ssid=r[0], bssid=r[1], ch=r[2], band=r[3], auth=r[4], enc=r[5],
                      std=r[6], base=r[7], cur=float(r[7])) for r in raw]
    aps = []
    for i, d in enumerate(_DEMO):
        # Tín hiệu dao động nhẹ quanh giá trị gốc (random walk có kéo về gốc)
        d["cur"] += random.uniform(-2.0, 2.0) + (d["base"] - d["cur"]) * 0.25
        dbm = max(-97.0, min(-30.0, d["cur"]))
        pct = int(max(0, min(100, (dbm + 100) * 2)))
        aps.append(AccessPoint(
            ssid=d["ssid"], bssid=d["bssid"], signal_pct=pct, channel=d["ch"], band=d["band"],
            freq_mhz=channel_to_freq(d["ch"], d["band"]), security=d["auth"], encryption=d["enc"],
            standard=d["std"], connected=(i == 0)))
    return aps


# ----------------------------------------------------------------- API chung
def backend_name(demo: bool = False) -> str:
    if demo:
        return "Demo"
    return {"Windows": "netsh (Windows)", "Linux": "nmcli (Linux)"}.get(platform.system(), platform.system())


def scan(demo: bool = False) -> list[AccessPoint]:
    if demo:
        return scan_demo()
    system = platform.system()
    if system == "Windows":
        return scan_windows()
    if system == "Linux":
        return scan_linux()
    raise RuntimeError(f"Hệ điều hành {system} chưa được hỗ trợ quét thật. Dùng chế độ Demo (--demo).")
