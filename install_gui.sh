#!/bin/bash
# Install Kakarot as a native GNOME application with the floating mascot as
# its launcher, plus the daemon service, icon, and AppStream metadata.
set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ID="io.github.kakarot"
DESKTOP_ID="kakarot-mascot.desktop"
DAEMON_PYTHON="$BASE_DIR/venv/bin/python3"

if [ ! -x "$DAEMON_PYTHON" ]; then
  echo "error: project venv missing; install requirements.txt before the daemon." >&2
  exit 1
fi

# The daemon owns the project venv and the memory budget that goes with it. The
# mascot is a separate GUI process, so it must run under an interpreter that
# actually has PyQt6 -- per AGENTS.md the system interpreter owns GUI bindings,
# so it is preferred over the venv rather than assumed.
GUI_PYTHON=""
for candidate in /usr/bin/python3 "$BASE_DIR/venv/bin/python3"; do
  if [ -x "$candidate" ] && \
     "$candidate" -c "import PyQt6" >/dev/null 2>&1; then
    GUI_PYTHON="$candidate"
    break
  fi
done
if [ -z "$GUI_PYTHON" ]; then
  echo "error: no interpreter with PyQt6 and QtDBus found; the floating mascot cannot start." >&2
  echo "       run: $BASE_DIR/venv/bin/pip install -r $BASE_DIR/requirements-mascot.txt" >&2
  echo "   or install PyQt6 for /usr/bin/python3." >&2
  exit 1
fi

APPS_DIR="$HOME/.local/share/applications"
ICON_THEME_DIR="$HOME/.local/share/icons/hicolor"
SCALABLE_DIR="$ICON_THEME_DIR/scalable/apps"
SIZED_DIR="$ICON_THEME_DIR/256x256/apps"
METAINFO_DIR="$HOME/.local/share/metainfo"
DBUS_SERVICE_DIR="$HOME/.local/share/dbus-1/services"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$APPS_DIR" "$SCALABLE_DIR" "$SIZED_DIR" \
         "$METAINFO_DIR" "$DBUS_SERVICE_DIR" "$UNIT_DIR"

# ---------------------------------------------------------------- launcher
echo "Installing floating mascot launcher..."
# Exec is the non-D-Bus fallback, so it must name the real interpreter and the
# absolute frontend path (the same pair kakarot-mascot.service runs). The bus
# name remains available for D-Bus callers; the desktop entry below launches
# the same overlay script directly.
sed -e "s|@GUI_PYTHON@|$GUI_PYTHON|g" \
    -e "s|@PREFIX@|$BASE_DIR|g" \
    "$BASE_DIR/$DESKTOP_ID" > "$APPS_DIR/$DESKTOP_ID"
chmod +x "$APPS_DIR/$DESKTOP_ID"
rm -f "$APPS_DIR/io.github.kakarot.desktop"

echo "Installing mascot activation route..."
cp "$BASE_DIR/$APP_ID.service" "$DBUS_SERVICE_DIR/$APP_ID.service"

# The daemon is a systemd unit; this makes the bus start it on demand so a
# GNOME shortcut can reach it even when it is not running yet.
cat > "$DBUS_SERVICE_DIR/$APP_ID.Daemon.service" <<EOF
[D-BUS Service]
Name=$APP_ID.Daemon
SystemdService=kakarot-core.service
Exec=/usr/bin/systemctl --user start kakarot-core.service
EOF

# ------------------------------------------------------------------- icon
# One identity, one icon. The launcher, the app grid and the running window
# all resolve Icon=$APP_ID.Daemon through the theme, so avatar.png is the
# single source of truth and nothing is generated at runtime.
# The basename must stay the RDNN name: a second, differently named icon would
# give GNOME two identities to match against StartupWMClass and pin a
# duplicate dock icon.
echo "Installing application icon..."
ICON_SRC=""
for candidate in "$BASE_DIR/avatar.png" "$BASE_DIR/assets/avatar.png"; do
    if [ -f "$candidate" ]; then
        ICON_SRC="$candidate"
        break
    fi
done

if [ -n "$ICON_SRC" ]; then
    # The XDG icon theme spec keys a fixed-size directory by its size, so a
    # 610px master must be rasterised into each standard size rather than
    # copied once into 256x256. One size alone renders soft everywhere else.
    ICON_PY=""
    for candidate in "$BASE_DIR/venv/bin/python3" "$GUI_PYTHON" /usr/bin/python3; do
        if [ -x "$candidate" ] && \
           "$candidate" -c "import PIL" >/dev/null 2>&1; then
            ICON_PY="$candidate"
            break
        fi
    done

    if [ -n "$ICON_PY" ]; then
        "$ICON_PY" - "$ICON_SRC" "$ICON_THEME_DIR" "$APP_ID.Daemon" <<'PY'
import sys
from pathlib import Path
from PIL import Image

src, theme_root, name = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
image = Image.open(src).convert("RGBA")
w, h = image.size
side = min(w, h)
if w != h:  # a non-square source would be distorted by the theme
    image = image.crop(((w - side) // 2, (h - side) // 2,
                        (w - side) // 2 + side, (h - side) // 2 + side))
    w = h = side

# 512 is the ceiling: it covers HiDPI panel scaling, and the master is never
# enlarged, so nothing is upscaled from a smaller bitmap.
for size in (48, 64, 128, 256, 512):
    target = theme_root / f"{size}x{size}" / "apps"
    target.mkdir(parents=True, exist_ok=True)
    if size <= w:
        resized = image if size == w else image.resize((size, size), Image.LANCZOS)
    else:
        resized = image  # keep the master; never upscale
    resized.save(target / f"{name}.png", format="PNG", optimize=True)
    print(f"  {size:>3}x{size:<3} -> {target / (name + '.png')}")
PY
    else
        echo "  (Pillow unavailable; installing the master as-is)"
        cp "$ICON_SRC" "$SIZED_DIR/$APP_ID.Daemon.png"
    fi
    echo "  source -> $ICON_SRC"
    # Only one asset may own the identity; drop the old scalable icon so the
    # theme cannot resolve the name to a stale graphic.
    rm -f "$SCALABLE_DIR/$APP_ID.Daemon.svg"
else
    echo "  no avatar.png found; falling back to the bundled scalable icon"
    cp "$BASE_DIR/assets/$APP_ID.Daemon.svg" "$SCALABLE_DIR/$APP_ID.Daemon.svg"
    rm -f "$SIZED_DIR/$APP_ID.Daemon.png"
    echo "  svg    -> $SCALABLE_DIR/$APP_ID.Daemon.svg"
fi

echo "Installing AppStream metadata..."
cp "$BASE_DIR/assets/$APP_ID.Daemon.metainfo.xml" "$METAINFO_DIR/$APP_ID.Daemon.metainfo.xml"

echo "Refreshing caches..."
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f -t "$ICON_THEME_DIR" || true
command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$APPS_DIR" || true
command -v glib-compile-schemas >/dev/null 2>&1 && \
  glib-compile-schemas "$HOME/.local/share/dbus-1/services" 2>/dev/null || true

# ------------------------------------------------------------------ daemon
echo "Installing systemd user units..."
sed -e "s|@PYTHON@|$DAEMON_PYTHON|g" \
    -e "s|@PREFIX@|$BASE_DIR|g" \
    "$BASE_DIR/kakarot-core.service" > "$UNIT_DIR/kakarot-core.service"
cat > "$UNIT_DIR/kakarot-mascot.service" <<EOF
[Unit]
Description=Kakarot floating desktop mascot
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
WorkingDirectory=$BASE_DIR
Environment=PYTHONPATH=$BASE_DIR
ExecStart=$GUI_PYTHON $BASE_DIR/kakarot_overlay/mascot.py
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
EOF
systemctl --user daemon-reload
systemctl --user disable --now kakarot.service kakarot-mascot.service 2>/dev/null || true
rm -f "$UNIT_DIR/kakarot.service"
systemctl --user daemon-reload
systemctl --user enable kakarot-core.service
systemctl --user restart kakarot-core.service

echo
echo "Installed. The core starts on desktop login; launch the floating mascot from Applications."
echo "  Launcher : $APPS_DIR/$DESKTOP_ID"
if [ -n "$ICON_SRC" ]; then
  echo "  Icon     : $SIZED_DIR/$APP_ID.Daemon.png (+ 48/64/128/512) from $ICON_SRC"
else
  echo "  Icon     : $SCALABLE_DIR/$APP_ID.Daemon.svg"
fi
echo "  Metainfo : $METAINFO_DIR/$APP_ID.Daemon.metainfo.xml"
echo "  Units    : $UNIT_DIR/kakarot-core.service (compatibility alias: kakarot.service)"
echo
echo "Drive the daemon from any app or GNOME shortcut with:"
echo "  gdbus call --session --dest io.github.kakarot.Daemon \\"
echo "    --object-path /io/github/kakarot/Daemon \\"
echo "    --method io.github.kakarot.Daemon.Execute status '{}'"
