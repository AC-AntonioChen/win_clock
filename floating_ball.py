"""Windows floating countdown capsule (v4 — true per-pixel alpha).

A frameless, always-on-top floating capsule showing time left to a target date:

    ┌─────────────────────────────────────────────────────┐
    │  国庆节            67 天            12:30:45        │
    │  剩余                                时 分 秒        │
    └─────────────────────────────────────────────────────┘

Key difference from v3: it uses TRUE PER-PIXEL ALPHA transparency via
UpdateLayeredWindow (WS_EX_LAYERED), NOT a magenta colour key. This eliminates
the pink/purple fringe that appears around anti-aliased edges when a colour-key
scheme is used (colour keys can't represent partial transparency, so the
semi-transparent edge pixels blend toward the key colour and show as coloured
bands — the "purple lines" problem).

How it works:
  * We render the whole capsule (with soft shadow + anti-aliased text) to a
    premultiplied RGBA buffer.
  * A tkinter window provides the message loop + mouse/keyboard events, but we
    override its drawing: we mark it WS_EX_LAYERED and call UpdateLayeredWindow
    with the RGBA buffer. Pixels with alpha=0 are fully transparent AND
    click-through is achieved by hot-tracking via the window's own region.
  * Because the window itself receives mouse events only where we set its
    window region (or we make the transparent pixels click-through with
    WS_EX_TRANSPARENT toggling), dragging still works on the capsule body.

Wheel / Ctrl+wheel to zoom (60–200%); position + scale persisted.

Run:  python floating_ball.py
"""

import ctypes
import datetime as dt
import json
import math
import os
import sys
import threading
import time
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageTk
import numpy as np
import tkinter as tk
from tkinter import simpledialog, messagebox

# --------------------------------------------------------------------------- #
# Win32 plumbing for UpdateLayeredWindow (per-pixel alpha)
# --------------------------------------------------------------------------- #
user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
kernel32 = ctypes.windll.kernel32

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20
ULW_ALPHA = 0x00000002
AC_SRC_ALPHA = 0x01
BI_RGB = 0
CBM_INIT = 0x04


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
        ("SourceConstantAlpha", wintypes.BYTE),
        ("AlphaFormat", wintypes.BYTE),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


user32.SetWindowLongPtrW.restype = ctypes.c_long
user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
user32.GetWindowLongPtrW.restype = ctypes.c_long
user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF,
                                              wintypes.BYTE, wintypes.DWORD]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                   ctypes.POINTER(ctypes.c_void_p),
                                   wintypes.HANDLE, wintypes.DWORD]
# GDI handles are 64-bit on 64-bit Windows; force these to c_void_p so they
# don't overflow when passed as arguments.
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]


def make_layered(hwnd):
    """Add WS_EX_LAYERED to the window so UpdateLayeredWindow can draw it."""
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW)


def update_window_bitmap(hwnd, rgba_image):
    """Paint `rgba_image` (PIL RGBA) into `hwnd` with per-pixel alpha."""
    img = rgba_image.convert("RGBA")
    w, h = img.size
    arr = np.array(img).astype(np.float32)
    # premultiply alpha (required for AC_SRC_ALPHA blending)
    arr[:, :, :3] *= (arr[:, :, 3:4] / 255.0)
    arr = arr.astype(np.uint8)
    # BMP is bottom-up
    arr = arr[::-1]
    buf = arr.tobytes()

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(bi)
    bi.biWidth = w
    bi.biHeight = h
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = BI_RGB
    bi.biSizeImage = len(buf)

    hdc_screen = user32.GetDC(0)
    ptr = ctypes.c_void_p()
    hbm = gdi32.CreateDIBSection(hdc_screen, ctypes.byref(bi), 0,
                                 ctypes.byref(ptr), None, 0)
    if not hbm:
        user32.ReleaseDC(0, hdc_screen)
        return False
    ctypes.memmove(ptr, buf, len(buf))
    hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
    gdi32.SelectObject(hdc_mem, hbm)

    blend = BLENDFUNCTION()
    blend.BlendOp = 0
    blend.BlendFlags = 0
    blend.SourceConstantAlpha = 255
    blend.AlphaFormat = AC_SRC_ALPHA

    pt_zero = POINT(0, 0)
    size = SIZE(w, h)
    ok = user32.UpdateLayeredWindow(hwnd, hdc_screen, None,
                                    ctypes.byref(size), hdc_mem,
                                    ctypes.byref(pt_zero), 0,
                                    ctypes.byref(blend), ULW_ALPHA)
    gdi32.DeleteObject(hbm)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(0, hdc_screen)
    return bool(ok)


def set_click_through(hwnd, enabled):
    """Toggle WS_EX_TRANSPARENT to make the whole window click-through.

    We keep click-through OFF normally so the capsule is draggable; we don't
    actually need it on because fully-transparent (alpha=0) pixels already pass
    clicks through under UpdateLayeredWindow only when combined with a region.
    Kept here for completeness / future per-pixel hit-testing.
    """
    ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
    if enabled:
        ex |= WS_EX_TRANSPARENT
    else:
        ex &= ~WS_EX_TRANSPARENT
    user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ex)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "countdown_config.json")

DEFAULT_TARGETS = [
    {"name": "国庆节", "date": "2026-10-01"},
    {"name": "元旦",   "date": "2027-01-01"},
]
DEFAULT_THEME = "night"   # defined early so DEFAULT_SETTINGS can reference it
DEFAULT_SETTINGS = {"scale": 1.0, "pos_x": None, "pos_y": None, "theme": DEFAULT_THEME}

# Today's key milestones (hour, minute). The user-defined moments that mark
# the passing of the day — "time is running out" reminders. 24:00 (next midnight)
# is appended automatically as the "today ends" sentinel.
DEFAULT_MILESTONES = [
    ("12:30", 12, 30),
    ("17:30", 17, 30),
    ("23:00", 23, 0),
]

# Carousel timing (seconds each frame stays before scrolling to the next).
DWELL_MAIN = 45.0      # main target
DWELL_MILESTONE = 30.0  # each daily milestone
SCROLL_DURATION = 1.2  # animation length of one slide transition (slow & gentle)

# --------------------------------------------------------------------------- #
# Capsule geometry
# --------------------------------------------------------------------------- #
BASE_W = 390   # content needs ~376px; small slack keeps it from auto-growing
BASE_H = 120
MARGIN = 24  # transparent padding (also room for drop shadow)


# --------------------------------------------------------------------------- #
# Themes — a Theme bundles every colour the capsule uses. Add a new preset by
# appending to THEMES; users may also supply a custom hex string (parsed into
# an ad-hoc Theme at runtime).
# --------------------------------------------------------------------------- #
class Theme:
    """All colours used to paint the capsule.

    Fields are RGBA tuples. accent is the focal number's "normal" colour (the
    urgency colours still override it when a milestone gets close).
    """
    __slots__ = ("bg", "bg_hi", "outline", "shadow",
                 "name_col", "label", "accent", "unit",
                 "hms", "sep", "hms_label")

    def __init__(self, bg, bg_hi, outline, shadow,
                 name_col, label, accent, unit, hms, sep, hms_label):
        self.bg = bg
        self.bg_hi = bg_hi
        self.outline = outline
        self.shadow = shadow
        self.name_col = name_col
        self.label = label
        self.accent = accent
        self.unit = unit
        self.hms = hms
        self.sep = sep
        self.hms_label = hms_label


def _rgba(hex6, alpha=255):
    """#RRGGBB or RRGGBB -> (r,g,b,alpha)."""
    h = hex6.lstrip("#")
    if len(h) != 6:
        raise ValueError(f"bad hex colour: {hex6!r}")
    r = int(h[0:2], 16); g = int(h[2:4], 16); b = int(h[4:6], 16)
    return (r, g, b, alpha)


# Build a Theme from a single hex background colour, deriving harmonious text
# colours automatically (used for user-supplied custom colours).
def _theme_from_bg(hex6, alpha=247):
    r, g, b = _rgba(hex6)[:3]
    bright = (r + g + b) / 3
    is_dark = bright < 128
    if is_dark:
        # dark bg -> light text
        name_c = (min(255, r + 210), min(255, g + 215), min(255, b + 220), 255)
        accent = (255, 255, 255, 255)
        hms_c = (min(255, r + 205), min(255, g + 208), min(255, b + 213), 255)
        sep_c = (min(255, r + 95), min(255, g + 98), min(255, b + 105), 255)
    else:
        # light bg -> dark text
        name_c = (max(0, r - 90), max(0, g - 90), max(0, b - 95), 255)
        accent = (max(0, r - 130), max(0, g - 130), max(0, b - 135), 255)
        hms_c = (max(0, r - 70), max(0, g - 70), max(0, b - 75), 255)
        sep_c = (max(0, r - 40), max(0, g - 40), max(0, b - 40), 255)
    return Theme(
        bg=(r, g, b, alpha),
        bg_hi=(255, 255, 255, 16 if is_dark else 40),
        outline=(255, 255, 255, 24 if is_dark else 60),
        shadow=(0, 0, 0, 120),
        name_col=name_c,
        label=sep_c,
        accent=accent,
        unit=hms_c,
        hms=hms_c,
        sep=sep_c,
        hms_label=sep_c,
    )


THEMES = {
    # Night-ink: the original dark charcoal.
    "night": Theme(
        bg=(26, 27, 32, 247), bg_hi=(255, 255, 255, 16),
        outline=(255, 255, 255, 24), shadow=(0, 0, 0, 120),
        name_col=(235, 238, 245, 255), label=(140, 144, 156, 255),
        accent=(255, 255, 255, 255), unit=(200, 204, 214, 255),
        hms=(228, 230, 238, 255), sep=(110, 114, 126, 255),
        hms_label=(110, 114, 126, 255),
    ),
    # Rose: warm dusty-pink.
    "rose": Theme(
        bg=(58, 32, 42, 247), bg_hi=(255, 255, 255, 18),
        outline=(255, 200, 215, 40), shadow=(0, 0, 0, 120),
        name_col=(255, 226, 236, 255), label=(205, 168, 182, 255),
        accent=(255, 240, 245, 255), unit=(238, 200, 212, 255),
        hms=(245, 214, 224, 255), sep=(180, 140, 158, 255),
        hms_label=(180, 140, 158, 255),
    ),
    # Jade: deep teal-green.
    "jade": Theme(
        bg=(18, 44, 40, 247), bg_hi=(255, 255, 255, 18),
        outline=(180, 240, 220, 36), shadow=(0, 0, 0, 120),
        name_col=(224, 246, 236, 255), label=(150, 196, 178, 255),
        accent=(236, 255, 246, 255), unit=(200, 232, 218, 255),
        hms=(220, 244, 232, 255), sep=(120, 168, 150, 255),
        hms_label=(120, 168, 150, 255),
    ),
    # Clear: fully transparent background (text floats on the desktop).
    "clear": Theme(
        bg=(0, 0, 0, 0), bg_hi=(255, 255, 255, 0),
        outline=(255, 255, 255, 0), shadow=(0, 0, 0, 0),
        name_col=(235, 238, 245, 255), label=(180, 184, 196, 255),
        accent=(255, 255, 255, 255), unit=(220, 224, 234, 255),
        hms=(235, 238, 248, 255), sep=(160, 164, 176, 255),
        hms_label=(160, 164, 176, 255),
    ),
}



def load_config():
    data = {"targets": list(DEFAULT_TARGETS), "settings": dict(DEFAULT_SETTINGS)}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw.get("targets"), list):
                data["targets"] = raw["targets"]
            if isinstance(raw.get("settings"), dict):
                for k in ("scale", "pos_x", "pos_y", "theme"):
                    if k in raw["settings"]:
                        data["settings"][k] = raw["settings"][k]
        except Exception:
            pass
    return data


def save_config(config):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


def resolve_theme(spec):
    """Turn a theme spec (preset name OR '#hex' / 'hex') into a Theme object.

    Returns (Theme, display_label). Falls back to night theme on bad input.
    """
    if isinstance(spec, str) and spec.startswith("#"):
        spec = spec[1:]
    if isinstance(spec, str) and spec in THEMES:
        return THEMES[spec], {"night": "夜墨", "rose": "玫瑰", "jade": "青玉", "clear": "透明"}[spec]
    # try custom hex
    if isinstance(spec, str) and len(spec.lstrip("#")) == 6:
        try:
            return _theme_from_bg(spec), f"自定义 #{spec.lstrip('#').upper()}"
        except Exception:
            pass
    return THEMES[DEFAULT_THEME], "夜墨"


# --------------------------------------------------------------------------- #
# Windows autostart — writes/removes a Run-key entry in the current user's
# registry (HKCU), no admin rights required.
# --------------------------------------------------------------------------- #
AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "WinCountdown"


def _exe_or_script():
    """The command to launch the app at startup: the frozen exe if running as
    PyInstaller bundle, else `pythonw <script>`."""
    if getattr(sys, "frozen", False):
        # bundled exe — quote the path in case it contains spaces
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(__file__)}"'


def is_autostart_enabled():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                            winreg.KEY_READ) as k:
            winreg.QueryValueEx(k, AUTOSTART_VALUE)
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def set_autostart(enabled):
    try:
        import winreg
        if enabled:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, AUTOSTART_VALUE, 0, winreg.REG_SZ,
                                  _exe_or_script())
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                    winreg.DeleteValue(k, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass
        return True
    except OSError:
        return False


def parse_date(s):
    if not isinstance(s, str):
        return None
    for sep in ("-", "/"):
        parts = s.split(sep)
        if len(parts) == 3:
            try:
                return dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
            except ValueError:
                return None
    return None


def split_remaining(target_date, now=None):
    now = now or dt.datetime.now()
    target_dt = dt.datetime(target_date.year, target_date.month, target_date.day)
    total = int((target_dt - now).total_seconds())
    is_past = total < 0
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    return days, hours, minutes, seconds, is_past


# --------------------------------------------------------------------------- #
# Fonts
# --------------------------------------------------------------------------- #
def _has_cjk(text):
    return any('\u3000' <= ch <= '\u9fff' or '\uff00' <= ch <= '\uffef'
               for ch in (text or ""))


def _font(size, *, bold=False, mono=False, cjk=False):
    size = max(6, int(round(size)))
    if cjk:
        paths = ["C:/Windows/Fonts/msyhbd.ttc" if bold else "C:/Windows/Fonts/msyh.ttc",
                 "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/arial.ttf"]
    elif mono:
        paths = ["C:/Windows/Fonts/consola.ttf", "C:/Windows/Fonts/cour.ttf",
                 "C:/Windows/Fonts/arial.ttf"]
    elif bold:
        paths = ["C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
                 "C:/Windows/Fonts/arial.ttf"]
    else:
        paths = ["C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf"]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _tw(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b[2] - b[0], b[3] - b[1], b[0]


# --------------------------------------------------------------------------- #
# Frame model — one "frame" describes everything needed to paint the capsule.
# Both the main target and the daily milestones produce a frame, so the
# rendering code is shared and consistent.
# --------------------------------------------------------------------------- #
class Frame:
    """A single carousel frame.

    left_name   : top-left title text   (e.g. "国庆节" / "今日节点")
    left_label  : small label below it  (e.g. "剩余" / "下一节点")
    main_text   : the big focal string  (e.g. "67" / "2")
    main_unit   : unit after the big number (e.g. "天" / "时")
    hms         : [hh, mm, ss] right-side secondary readout
    urgency     : "normal" | "yellow" | "orange" | "red"  -> main_text colour
    """
    __slots__ = ("left_name", "left_label", "main_text", "main_unit",
                 "hms", "urgency")

    def __init__(self, left_name, left_label, main_text, main_unit,
                 hms, urgency="normal"):
        self.left_name = left_name
        self.left_label = left_label
        self.main_text = main_text
        self.main_unit = main_unit
        self.hms = hms
        self.urgency = urgency


URGENCY_COLOR = {
    "normal": (255, 255, 255, 255),   # white
    "yellow": (255, 214, 70, 255),    # warm yellow
    "orange": (255, 152, 50, 255),    # orange
    "red":    (255, 76, 76, 255),     # red — strongest
}


def urgency_for_seconds(total_seconds):
    """Map a remaining-time (seconds) to an urgency level.

      > 1h         -> normal (white)
      30m..1h      -> yellow
      5m..30m      -> orange
      < 5m         -> red
    """
    if total_seconds is None:
        return "normal"
    if total_seconds < 0:
        return "normal"
    if total_seconds < 5 * 60:
        return "red"
    if total_seconds < 30 * 60:
        return "orange"
    if total_seconds < 3600:
        return "yellow"
    return "normal"


# --------------------------------------------------------------------------- #
# Capsule rendering — fully transparent background, no colour key.
# `frame` is a Frame describing the content. `scroll_offset` is in [-1..1]:
#   0  = frame centred/visible
#  <0  = frame sliding up & out (negative => upward)
#  >0  = frame sliding in from below
# --------------------------------------------------------------------------- #
def render_capsule(frame, scale=1.0, scroll_offset=0.0, theme=None):
    """Render a capsule frame to RGBA. Returns (image, capsule_width)."""
    if theme is None:
        theme = THEMES[DEFAULT_THEME]
    s = scale
    W = int(round(BASE_W * s))
    H = int(round(BASE_H * s))
    win_w = W + MARGIN * 2
    win_h = H + MARGIN * 2

    probe = Image.new("RGBA", (1, 1))
    pd = ImageDraw.Draw(probe)

    name_txt = frame.left_name or " "
    label_txt = frame.left_label or " "
    main_txt = str(frame.main_text)
    unit_txt = frame.main_unit or ""
    hms = list(frame.hms) if frame.hms else ["00", "00", "00"]
    # normal urgency uses the theme accent; coloured urgency overrides it
    main_color = URGENCY_COLOR[frame.urgency] if frame.urgency != "normal" else theme.accent

    f_name = _font(18 * s, bold=True, cjk=_has_cjk(name_txt))
    f_label = _font(14 * s, bold=False, cjk=True)
    f_main = _font(58 * s, bold=True)
    f_unit = _font(20 * s, bold=False, cjk=_has_cjk(unit_txt))
    f_hms = _font(28 * s, bold=True, mono=True)   # was 24 -> bigger per request
    f_tiny = _font(11 * s, bold=False, cjk=True)

    name_w, name_h, name_ox = _tw(pd, name_txt, f_name)
    label_w, label_h, _ = _tw(pd, label_txt, f_label)
    d_w, d_h, d_ox = _tw(pd, main_txt, f_main)
    unit_w, unit_h, unit_ox = _tw(pd, unit_txt, f_unit)
    hms_pieces_w = [_tw(pd, p, f_hms)[0] for p in hms]
    sep = ":"
    sep_w = _tw(pd, sep, f_hms)[0]
    hms_h = _tw(pd, "00", f_hms)[1]

    pad = int(22 * s)
    gap_unit = int(8 * s)
    gap_hms_sep = int(8 * s)
    group_gap = int(24 * s)

    days_group_w = d_w + (gap_unit + unit_w if unit_txt else 0)
    hms_group_w = sum(hms_pieces_w) + 2 * sep_w + 2 * gap_hms_sep
    content_w = pad + max(name_w, label_w) + group_gap + days_group_w + group_gap + hms_group_w + pad
    if content_w > W:
        W = content_w
        win_w = W + MARGIN * 2

    img = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x0, y0 = MARGIN, MARGIN
    x1, y1 = MARGIN + W, MARGIN + H
    radius = H // 2

    # soft drop shadow (skip for fully-transparent themes)
    if theme.shadow[3] > 0:
        shadow = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        sd.rounded_rectangle((x0 + 1, y0 + 6, x1 + 3, y1 + 8),
                             radius=radius, fill=theme.shadow)
        shadow = shadow.filter(ImageFilter.GaussianBlur(8 * s))
        img = Image.alpha_composite(img, shadow)
        draw = ImageDraw.Draw(img)

    # body fill (skip drawing if fully transparent — text floats on desktop)
    has_body = theme.bg[3] > 0
    body_mask = None
    if has_body:
        draw.rounded_rectangle((x0, y0, x1, y1), radius=radius, fill=theme.bg)

        # body mask (rounded) reused for sheen clip + final text clip
        body_mask = Image.new("L", (win_w, win_h), 0)
        ImageDraw.Draw(body_mask).rounded_rectangle((x0, y0, x1 - 1, y1 - 1),
                                                    radius=radius, fill=255)

        # top sheen (alpha-composited, clipped to body — no fringe)
        if theme.bg_hi[3] > 0:
            sheen = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
            shd = ImageDraw.Draw(sheen)
            sheen_h = max(1, int(H * 0.42))
            for i in range(sheen_h):
                t = i / sheen_h
                a = int(theme.bg_hi[3] * (1 - t) ** 1.5)
                if a <= 0:
                    break
                shd.line((x0, y0 + i, x1 - 1, y0 + i), fill=(255, 255, 255, a))
            sheen.putalpha(Image.composite(sheen.split()[3],
                                           Image.new("L", (win_w, win_h), 0), body_mask))
            img = Image.alpha_composite(img, sheen)
            draw = ImageDraw.Draw(img)

        # crisp outline
        if theme.outline[3] > 0:
            draw.rounded_rectangle((x0, y0, x1 - 1, y1 - 1),
                                   radius=radius, outline=theme.outline, width=1)

    # ---- text, with vertical scroll offset applied ---- #
    # scroll_offset in [-1,1]: -1 = fully above (slid up out), +1 = fully below.
    # Map to a pixel shift over the capsule height.
    dy = int(scroll_offset * (H + 8))
    mid_y = (y0 + y1) // 2 + dy
    # Left column: name above label, vertically centred as a block.
    # We space the two lines by a value derived from the font SIZE (not the
    # textbbox height), because CJK font bounding boxes under-report vertical
    # extent and the two lines visibly overlap if we trust bbox height alone.
    line_gap = int(10 * s)
    line_step_name = int(20 * s)    # visual row height of the 18px name font
    line_step_label = int(16 * s)   # visual row height of the 14px label font
    block_h = line_step_name + line_gap + line_step_label
    block_top = mid_y - block_h // 2
    left_x = x0 + pad
    draw.text((left_x - name_ox, block_top), name_txt, fill=theme.name_col, font=f_name)
    draw.text((left_x, block_top + line_step_name + line_gap),
              label_txt, fill=theme.label, font=f_label)

    after_left = left_x + max(name_w, label_w) + group_gap
    days_x = after_left
    draw.text((days_x - d_ox, mid_y - d_h // 2 - 2), main_txt, fill=main_color, font=f_main)
    if unit_txt:
        unit_x = days_x + d_w + gap_unit
        draw.text((unit_x - unit_ox, mid_y - unit_h // 2 + int(14 * s)),
                  unit_txt, fill=theme.unit, font=f_unit)

    hms_right = x1 - pad
    cx = hms_right
    draw.text((cx - hms_pieces_w[2], mid_y - hms_h // 2 - 4), hms[2], fill=theme.hms, font=f_hms)
    ss_cx = cx - hms_pieces_w[2] // 2
    cx -= hms_pieces_w[2] + gap_hms_sep + sep_w
    draw.text((cx - sep_w, mid_y - hms_h // 2 - 4), sep, fill=theme.sep, font=f_hms)
    cx -= sep_w + gap_hms_sep
    draw.text((cx - hms_pieces_w[1], mid_y - hms_h // 2 - 4), hms[1], fill=theme.hms, font=f_hms)
    mm_cx = cx - hms_pieces_w[1] // 2
    cx -= hms_pieces_w[1] + gap_hms_sep + sep_w
    draw.text((cx - sep_w, mid_y - hms_h // 2 - 4), sep, fill=theme.sep, font=f_hms)
    cx -= sep_w + gap_hms_sep
    draw.text((cx - hms_pieces_w[0], mid_y - hms_h // 2 - 4), hms[0], fill=theme.hms, font=f_hms)
    hh_cx = cx - hms_pieces_w[0] // 2

    label_y = mid_y - hms_h // 2 - 4 + hms_h + int(4 * s)
    for txt, ccx in (("时", hh_cx), ("分", mm_cx), ("秒", ss_cx)):
        tw, th, tox = _tw(draw, txt, f_tiny)
        draw.text((ccx - tw // 2 - tox, label_y), txt, fill=theme.hms_label, font=f_tiny)

    # clip text to the rounded body so scrolling frames don't leak past edges.
    # For bodyless (clear) themes there's no shape to clip to, so return as-is.
    if body_mask is None:
        return img, W
    out = Image.new("RGBA", (win_w, win_h), (0, 0, 0, 0))
    out.paste(img, (0, 0), body_mask)
    return out, W


# --------------------------------------------------------------------------- #
# Target selection
# --------------------------------------------------------------------------- #
def nearest_target(config, now=None):
    now = now or dt.datetime.now()
    today = now.date()
    futures, pasts = [], []
    for t in config["targets"]:
        d = parse_date(t.get("date"))
        if d is None:
            continue
        (futures if d >= today else pasts).append((d, t))
    if futures:
        futures.sort(key=lambda x: x[0])
        return futures[0][1], futures[0][0]
    if pasts:
        pasts.sort(key=lambda x: x[0], reverse=True)
        return pasts[0][1], pasts[0][0]
    return None, None


def tooltip_lines(config, now=None):
    now = now or dt.datetime.now()
    lines = []
    for t in config["targets"]:
        d = parse_date(t.get("date"))
        if d is None:
            lines.append(f"{t.get('name','?')}: 日期无效")
            continue
        days, hh, mm, ss, is_past = split_remaining(d, now)
        tag = "已过" if is_past else "剩余"
        lines.append(f"{t['name']}  {tag} {days}天 {hh:02d}:{mm:02d}:{ss:02d}")
    return lines


# --------------------------------------------------------------------------- #
# Frame construction for the carousel
# --------------------------------------------------------------------------- #
def _next_milestone_dt(now, h, m):
    """datetime of today's h:m if still in the future, else None (passed)."""
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return cand if cand > now else None


def build_main_frame(config, now=None):
    """The focal main-target frame, or None if no targets set."""
    now = now or dt.datetime.now()
    target, target_date = nearest_target(config, now)
    if target is None or target_date is None:
        return Frame("未设置", "—", "0", "天", ["00", "00", "00"], "normal")
    days, hh, mm, ss, is_past = split_remaining(target_date, now)
    label = "已过" if is_past else "剩余"
    return Frame(target.get("name", ""), label, str(days), "天",
                 [f"{hh:02d}", f"{mm:02d}", f"{ss:02d}"], "normal")


def build_milestone_frames(now=None):
    """Frames for each upcoming milestone today, plus the 'day ends' frame.

    Only milestones still in the future are returned (per spec: skip passed
    ones). Each frame's main number is the hours remaining to that milestone,
    with the finer H:M:S in the secondary readout. Urgency colour is derived
    from the seconds remaining.
    """
    now = now or dt.datetime.now()
    frames = []
    for label, h, m in DEFAULT_MILESTONES:
        mdt = _next_milestone_dt(now, h, m)
        if mdt is None:
            continue
        total = int((mdt - now).total_seconds())
        hh, rem = divmod(total, 3600)
        mm, ss = divmod(rem, 60)
        urg = urgency_for_seconds(total)
        # main focal = whole hours left (or "0" when under 1h); unit "时"
        # but when under an hour we show minutes as the big number instead,
        # so the urgency reads more naturally.
        if total >= 3600:
            main_txt, unit = str(hh), "时"
        else:
            main_txt, unit = str(mm), "分"
        frames.append(Frame(
            label, "还剩", main_txt, unit,
            [f"{hh:02d}", f"{mm:02d}", f"{ss:02d}"], urg,
        ))
    # "today ends" sentinel — next midnight.
    midnight = (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    total = int((midnight - now).total_seconds())
    hh, rem = divmod(total, 3600)
    mm, ss = divmod(rem, 60)
    urg = urgency_for_seconds(total)
    frames.append(Frame("今天结束", "还剩",
                        str(hh), "时",
                        [f"{hh:02d}", f"{mm:02d}", f"{ss:02d}"], urg))
    return frames


def build_carousel(config, now=None):
    """Full ordered carousel list for this moment: [main] + [milestones...].

    Main target leads; milestones follow in chronological order. If there are
    no milestones left today (e.g. just before midnight) only main is returned.
    """
    now = now or dt.datetime.now()
    frames = [build_main_frame(config, now)]
    frames.extend(build_milestone_frames(now))
    return frames


def dwell_for(frame):
    """How long (seconds) a frame should stay before scrolling to the next."""
    if frame.left_label in ("剩余", "已过"):
        return DWELL_MAIN
    return DWELL_MILESTONE


# --------------------------------------------------------------------------- #
# The application — tk window painted via UpdateLayeredWindow
# --------------------------------------------------------------------------- #
class FloatingCapsule:
    SNAP_DISTANCE = 4   # only snap when almost touching the screen edge
    HIDDEN_TAB = 14
    ANIM_STEPS = 12
    ANIM_DELAY = 12
    SCALE_MIN = 0.6
    SCALE_MAX = 2.0
    SCALE_STEP = 0.08

    def __init__(self):
        self.config = load_config()
        self.scale = float(self.config["settings"].get("scale", 1.0) or 1.0)
        self.scale = min(self.SCALE_MAX, max(self.SCALE_MIN, self.scale))
        # theme: preset name or '#hex'. Resolved into a Theme object on each
        # render so a custom hex change takes effect immediately.
        self.theme_spec = self.config["settings"].get("theme", DEFAULT_THEME)

        self.root = tk.Tk()
        self.root.title("countdown-capsule")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        # NOTE: deliberately NO -transparentcolor. We use per-pixel alpha via
        # UpdateLayeredWindow, which avoids the colour-key fringe problem.

        self.root.update_idletasks()
        self.screen_w = self.root.winfo_screenwidth()
        self.screen_h = self.root.winfo_screenheight()

        # Mark the real HWND as layered so we can paint it ourselves.
        self.hwnd = self.root.winfo_id()
        # Top-level HWND (winfo_id gives a child on some builds); walk to root.
        root_hwnd = self.hwnd
        while True:
            parent = user32.GetParent(root_hwnd)
            if not parent:
                break
            root_hwnd = parent
        self.top_hwnd = root_hwnd
        make_layered(self.top_hwnd)

        # We still need an event surface. Tk's canvas gives us mouse events.
        self.canvas = tk.Canvas(self.root, width=1, height=1,
                                highlightthickness=0, bd=0)
        self.canvas.pack()

        # state
        self._drag_start = None
        self._edge = None
        self._hidden = False
        self._hover_revealed = False
        self._anim_after_id = None

        # carousel state
        self._car_index = 0          # which frame is currently shown
        self._car_frames = []        # current carousel list
        self._dwell_left = 0.0       # seconds remaining on current frame
        self._scrolling = False      # True while a slide animation is playing
        self._scroll_t0 = 0.0        # perf_counter at scroll start
        self._scroll_from_idx = 0
        self._scroll_to_idx = 0

        # initial position
        sx = self.config["settings"].get("pos_x")
        sy = self.config["settings"].get("pos_y")
        if not (isinstance(sx, int) and isinstance(sy, int)):
            sx = self.screen_w - (BASE_W + MARGIN * 2) - 40
            sy = 60
        self.root.geometry(f"+{int(sx)}+{int(sy)}")

        # bindings
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._on_right_click)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.root.bind("<Enter>", self._on_enter)
        self.root.bind("<Leave>", self._on_leave)

        self._init_carousel()
        self._tick_anim()
        self._daily_loop_start()
        self.root.mainloop()

    # ---- carousel ---- #
    def _init_carousel(self):
        now = dt.datetime.now()
        self._car_frames = build_carousel(self.config, now)
        # clamp index
        if self._car_index >= len(self._car_frames):
            self._car_index = 0
        if not self._car_frames:
            self._car_frames = [Frame("未设置", "—", "0", "天", ["00", "00", "00"])]
            self._car_index = 0
        self._dwell_left = dwell_for(self._car_frames[self._car_index])

    def _current_frame(self):
        """Return the (possibly mid-scroll) frame to render right now.

        During a scroll we render the OUTGOING frame sliding up and the INCOMING
        frame sliding in from below; the caller composites both. To keep the
        HWND paint simple we instead render the incoming frame at the current
        scroll progress as its vertical offset, which gives the slide effect.
        """
        if not self._car_frames:
            return Frame("未设置", "—", "0", "天", ["00", "00", "00"])
        idx = min(self._car_index, len(self._car_frames) - 1)
        return self._car_frames[idx]

    def _tick_anim(self):
        """High-frequency driver: advances slide animation + dwell countdown,
        and refreshes the on-screen numbers every tick."""
        now_dt = dt.datetime.now()

        # If a slide animation is in progress, advance it.
        if self._scrolling:
            elapsed = time.perf_counter() - self._scroll_t0
            if elapsed >= SCROLL_DURATION:
                # finish: snap to the destination frame
                self._scrolling = False
                self._car_index = self._scroll_to_idx
                self._dwell_left = dwell_for(self._current_frame())
            self._render()
            self.root.after(50, self._tick_anim)
            return

        # refresh carousel list (milestones may have passed since last tick)
        new_frames = build_carousel(self.config, now_dt)
        self._car_frames = new_frames
        if self._car_index >= len(self._car_frames):
            self._car_index = 0

        # count down dwell; when it hits zero, start a slide to the next frame.
        self._dwell_left -= 0.05
        if self._dwell_left <= 0 and len(self._car_frames) > 1:
            self._start_scroll_to((self._car_index + 1) % len(self._car_frames))

        self._render()
        self.root.after(50, self._tick_anim)

    def _start_scroll_to(self, next_idx):
        """Begin a slide animation from the current frame to `next_idx`.
        No-op if a slide is already in progress or there's nothing to scroll."""
        if self._scrolling:
            return
        if len(self._car_frames) <= 1:
            return
        if next_idx % len(self._car_frames) == self._car_index:
            return
        self._scrolling = True
        self._scroll_t0 = time.perf_counter()
        self._scroll_from_idx = self._car_index
        self._scroll_to_idx = next_idx % len(self._car_frames)

    def _scroll_progress(self):
        """0..1 progress of the current slide animation (0 at start)."""
        if not self._scrolling:
            return 1.0
        e = time.perf_counter() - self._scroll_t0
        return min(1.0, max(0.0, e / SCROLL_DURATION))

    def _render(self):
        """Paint current frame; during a scroll, paint outgoing+incoming with
        a vertical slide offset each, then composite."""
        now_dt = dt.datetime.now()
        theme, _ = resolve_theme(self.theme_spec)
        # Always rebuild frame DATA with fresh numbers (seconds tick live), but
        # keep the carousel index stable so we don't jump items mid-view.
        if self._scrolling:
            out_idx = self._scroll_from_idx
            in_idx = self._scroll_to_idx
            frames = build_carousel(self.config, now_dt)
            out_idx = min(out_idx, len(frames) - 1)
            in_idx = min(in_idx, len(frames) - 1)
            p = self._scroll_progress()
            # ease-in-out (smooth start AND end) for a gentle slide
            eased = 0.5 - 0.5 * math.cos(p * math.pi)
            # outgoing slides UP (offset 0 -> -1), incoming slides in from BELOW (offset +1 -> 0)
            img_out, _ = render_capsule(frames[out_idx], scale=self.scale,
                                        scroll_offset=-eased, theme=theme)
            img_in, W = render_capsule(frames[in_idx], scale=self.scale,
                                       scroll_offset=1 - eased, theme=theme)
            img = Image.alpha_composite(img_out, img_in)
        else:
            frame = self._current_frame()
            # refresh the live numbers on this same item
            live = build_carousel(self.config, now_dt)
            if self._car_index < len(live):
                frame = live[self._car_index]
            img, W = render_capsule(frame, scale=self.scale, theme=theme)

        new_w, new_h = img.size
        cur_w = self.root.winfo_width()
        cur_h = self.root.winfo_height()
        if cur_w != new_w or cur_h != new_h:
            x, y = self._geometry_xy()
            self.canvas.configure(width=new_w, height=new_h)
            self.root.geometry(f"{new_w}x{new_h}+{x}+{y}")
            self.root.update_idletasks()
            make_layered(self.top_hwnd)
        update_window_bitmap(self.top_hwnd, img)

    # ---- zoom ---- #
    def _on_wheel(self, event):
        if event.delta > 0:
            self.scale = min(self.SCALE_MAX, self.scale + self.SCALE_STEP)
        else:
            self.scale = max(self.SCALE_MIN, self.scale - self.SCALE_STEP)
        self.config["settings"]["scale"] = round(self.scale, 3)
        save_config(self.config)
        self._render()

    def _zoom_to(self, value):
        self.scale = min(self.SCALE_MAX, max(self.SCALE_MIN, value))
        self.config["settings"]["scale"] = round(self.scale, 3)
        save_config(self.config)
        self._render()

    # ---- geometry helpers ---- #
    def _geometry_xy(self):
        s = self.root.geometry()
        try:
            return int(s.split("+")[-2]), int(s.split("+")[-1])
        except Exception:
            return self.root.winfo_x(), self.root.winfo_y()

    def _win_size(self):
        s = self.root.geometry()
        try:
            w, h = s.split("+")[0].split("x")
            return int(w), int(h)
        except Exception:
            return self.root.winfo_width(), self.root.winfo_height()

    # ---- dragging ---- #
    def _on_press(self, event):
        self._drag_start = (event.x_root, event.y_root, *self._geometry_xy())
        if self._hidden:
            self._set_hidden(False, animate=False)

    def _on_drag(self, event):
        if not self._drag_start:
            return
        mx, my, ox, oy = self._drag_start
        nx = ox + (event.x_root - mx)
        ny = oy + (event.y_root - my)
        ww, wh = self._win_size()
        nx = max(-MARGIN, min(self.screen_w - ww + MARGIN, nx))
        ny = max(-MARGIN, min(self.screen_h - wh + MARGIN, ny))
        self.root.geometry(f"+{nx}+{ny}")

    def _on_release(self, event):
        if not self._drag_start:
            return
        mx, my, ox, oy = self._drag_start
        self._drag_start = None
        # Click vs drag: if the mouse barely moved between press and release,
        # treat it as a click and advance the carousel manually. Threshold of
        # 5px tolerates tiny hand jitter without eating real drags.
        moved = abs(event.x_root - mx) + abs(event.y_root - my)
        if moved < 5:
            self._start_scroll_to(self._car_index + 1)
            return
        x, y = self._geometry_xy()
        self.config["settings"]["pos_x"] = x
        self.config["settings"]["pos_y"] = y
        save_config(self.config)
        self._maybe_snap(x, y)

    def _maybe_snap(self, x, y):
        ww, wh = self._win_size()
        cands = []
        if x < self.SNAP_DISTANCE:
            cands.append(("left", x))
        if self.screen_w - ww - x < self.SNAP_DISTANCE:
            cands.append(("right", self.screen_w - ww - x))
        if y < self.SNAP_DISTANCE:
            cands.append(("top", y))
        if self.screen_h - wh - y < self.SNAP_DISTANCE and y < self.screen_h - 80:
            cands.append(("bottom", self.screen_h - wh - y))
        if cands:
            edge = min(cands, key=lambda c: c[1])[0]
            self._edge = edge
            self._animate_to(self._snapped_xy(edge), then_hide=True)
        else:
            self._edge = None
            self._set_hidden(False, animate=False)

    def _snapped_xy(self, edge):
        x, y = self._geometry_xy()
        ww, wh = self._win_size()
        if edge == "left":
            return -MARGIN, y
        if edge == "right":
            return self.screen_w - ww + MARGIN, y
        if edge == "top":
            return x, -MARGIN
        if edge == "bottom":
            return x, self.screen_h - wh + MARGIN
        return x, y

    def _hidden_xy(self, edge):
        x, y = self._geometry_xy()
        ww, wh = self._win_size()
        keep = self.HIDDEN_TAB + MARGIN
        if edge == "left":
            return -(ww - keep), y
        if edge == "right":
            return self.screen_w - keep, y
        if edge == "top":
            return x, -(wh - keep)
        if edge == "bottom":
            return x, self.screen_h - keep
        return x, y

    def _animate_to(self, target_xy, then_hide=False):
        sx, sy = self._geometry_xy()
        tx, ty = target_xy
        if self._anim_after_id:
            self.root.after_cancel(self._anim_after_id)

        def step(i):
            t = i / self.ANIM_STEPS
            e = 1 - (1 - t) ** 3
            nx = int(sx + (tx - sx) * e)
            ny = int(sy + (ty - sy) * e)
            self.root.geometry(f"+{nx}+{ny}")
            if i < self.ANIM_STEPS:
                self._anim_after_id = self.root.after(self.ANIM_DELAY, step, i + 1)
            else:
                self._anim_after_id = None
                if then_hide:
                    self._set_hidden(True, animate=False)
        step(1)

    def _set_hidden(self, hidden, animate=True):
        self._hidden = hidden
        if hidden and self._edge:
            target = self._hidden_xy(self._edge)
            (self._animate_to(target) if animate
             else self.root.geometry(f"+{target[0]}+{target[1]}"))
        elif not hidden and self._edge:
            target = self._snapped_xy(self._edge)
            (self._animate_to(target) if animate
             else self.root.geometry(f"+{target[0]}+{target[1]}"))

    def _on_enter(self, _e):
        if self._hidden and self._edge:
            self._hover_revealed = True
            self._set_hidden(False, animate=True)

    def _on_leave(self, _e):
        if self._hover_revealed and self._edge:
            self._hover_revealed = False
            self._set_hidden(True, animate=True)

    # ---- menu ---- #
    def _on_right_click(self, event):
        menu = tk.Menu(self.root, tearoff=0)
        now = dt.datetime.now()
        for idx, t in enumerate(self.config["targets"]):
            d = parse_date(t.get("date"))
            if d is None:
                label = f"{t.get('name','?')}: 日期无效"
            else:
                days, hh, mm, ss, is_past = split_remaining(d, now)
                tag = "已过" if is_past else "剩余"
                label = f"{t['name']}  ·  {tag} {days}天 {hh:02d}:{mm:02d}:{ss:02d}"
            menu.add_command(label=label, state="disabled")
            menu.add_command(label=f"    删除 {t['name']}",
                             command=lambda i=idx: self.delete_target(i))
        if self.config["targets"]:
            menu.add_separator()
        menu.add_command(label="添加目标...", command=self.add_target)
        menu.add_command(label=f"缩放 {int(self.scale*100)}%  (滚轮调节)",
                         state="disabled")
        menu.add_command(label="  放大", command=lambda: self._zoom_to(self.scale + self.SCALE_STEP))
        menu.add_command(label="  缩小", command=lambda: self._zoom_to(self.scale - self.SCALE_STEP))
        menu.add_command(label="  重置 100%", command=lambda: self._zoom_to(1.0))
        menu.add_command(label="立即刷新", command=self._render)
        menu.add_command(label="取消贴边", command=self._unstick)
        menu.add_separator()
        # theme submenu
        _, cur_label = resolve_theme(self.theme_spec)
        tmenu = tk.Menu(menu, tearoff=0)
        preset_labels = {"night": "夜墨", "rose": "玫瑰", "jade": "青玉", "clear": "透明"}
        for key, lbl in preset_labels.items():
            mark = "✓ " if self.theme_spec == key else "   "
            tmenu.add_command(label=f"{mark}{lbl}",
                              command=lambda k=key: self._set_theme(k))
        tmenu.add_separator()
        tmenu.add_command(label="   自定义颜色…",
                          command=self._set_custom_theme)
        menu.add_cascade(label=f"主题：{cur_label}", menu=tmenu)
        # autostart toggle
        autostart_on = is_autostart_enabled()
        menu.add_command(
            label=("✓ 开机自启：已开启" if autostart_on else "   开机自启：已关闭"),
            command=self._toggle_autostart,
        )
        menu.add_separator()
        menu.add_command(label="退出", command=self._quit)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_theme(self, spec):
        """Apply a theme spec (preset key or '#hex') and persist it."""
        self.theme_spec = spec
        self.config["settings"]["theme"] = spec
        save_config(self.config)
        self._render()

    def _set_custom_theme(self):
        """Prompt for a hex colour and apply it as a custom theme."""
        val = simpledialog.askstring(
            "自定义主题颜色",
            "输入背景颜色十六进制（如 #2D1B4E 或 2D1B4E）：",
            parent=self.root,
            initialvalue="#1A1B20",
        )
        if val is None:
            return
        val = val.strip()
        # validate
        h = val.lstrip("#")
        if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
            messagebox.showerror("格式错误",
                                 "请输入 6 位十六进制颜色，如 #2D1B4E",
                                 parent=self.root)
            return
        self._set_theme("#" + h.upper())

    def _toggle_autostart(self):
        """Flip the boot-autostart Run-key entry and report the result."""
        now_on = not is_autostart_enabled()
        ok = set_autostart(now_on)
        if ok:
            state = "已开启" if now_on else "已关闭"
            messagebox.showinfo("开机自启", f"开机自启：{state}",
                                parent=self.root)
        else:
            messagebox.showerror("开机自启", "设置失败（无法写入注册表）",
                                 parent=self.root)

    def _unstick(self):
        self._edge = None
        self._hidden = False
        self._hover_revealed = False

    def _quit(self):
        x, y = self._geometry_xy()
        self.config["settings"]["pos_x"] = x
        self.config["settings"]["pos_y"] = y
        save_config(self.config)
        self.root.destroy()

    def add_target(self):
        name = simpledialog.askstring("添加倒计时目标", "名称（如：考试）：",
                                      parent=self.root)
        if name is None:
            return
        name = name.strip() or "目标"
        date_str = simpledialog.askstring(
            "添加倒计时目标", "日期 (YYYY-MM-DD，如 2026-10-01)：",
            parent=self.root)
        if date_str is None:
            return
        d = parse_date(date_str.strip())
        if d is None:
            messagebox.showerror("格式错误", "日期格式应为 YYYY-MM-DD",
                                 parent=self.root)
            return
        self.config["targets"].append({"name": name, "date": d.isoformat()})
        save_config(self.config)
        self._render()

    def delete_target(self, idx):
        if 0 <= idx < len(self.config["targets"]):
            self.config["targets"].pop(idx)
            save_config(self.config)
            self._render()

    def _daily_loop_start(self):
        def loop():
            last_day = dt.date.today()
            while True:
                time.sleep(60)
                today = dt.date.today()
                if today != last_day:
                    last_day = today
                    self.root.after(0, self._render)
        threading.Thread(target=loop, daemon=True).start()


def main():
    FloatingCapsule()


if __name__ == "__main__":
    main()
