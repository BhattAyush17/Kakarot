"""Cached border matting and bundle-safe asset lookup."""
from collections import deque
from functools import lru_cache
from pathlib import Path
import sys

from kakarot_overlay.qt import QtCore as C, QtGui as G


def resolve(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "assets" / name


@lru_cache(maxsize=8)
def load_transparent_png(path):
    image = G.QImage(str(path)).convertToFormat(G.QImage.Format.Format_ARGB32)
    if image.isNull():
        raise ValueError("Avatar image is missing or unreadable")
    w, h = image.width(), image.height()
    if w * h > 4_000_000:
        image = image.scaled(1500, 1500, C.Qt.AspectRatioMode.KeepAspectRatio,
                             C.Qt.TransformationMode.FastTransformation)
        w, h = image.width(), image.height()
    seen = bytearray(w * h)
    pending = deque([(x, 0) for x in range(w)] + [(x, h-1) for x in range(w)] +
                    [(0, y) for y in range(h)] + [(w-1, y) for y in range(h)])
    while pending:
        x, y = pending.popleft()
        i = y*w+x
        if seen[i]:
            continue
        seen[i] = 1
        pixel = image.pixelColor(x, y)
        rgb = pixel.red(), pixel.green(), pixel.blue()
        if pixel.alpha() and not (min(rgb) >= 225 and max(rgb)-min(rgb) <= 15):
            continue
        image.setPixel(x, y, 0)
        for nx, ny in ((x-1,y), (x+1,y), (x,y-1), (x,y+1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny*w+nx]:
                pending.append((nx, ny))
    return image


def avatar_pixmap(width=190):
    path = resolve("avatar.png")
    image = G.QImage(str(path)) if path.exists() else load_transparent_png(
        str(resolve("watermarked_img_15057877357388561413.png")))
    pixmap = G.QPixmap.fromImage(image)
    return pixmap.scaled(width, width, C.Qt.AspectRatioMode.KeepAspectRatio,
                         C.Qt.TransformationMode.FastTransformation)


def tray_icon():
    icon = G.QIcon()
    for size in (22, 24, 44, 48, 128):
        icon.addPixmap(avatar_pixmap(size))
    return icon
