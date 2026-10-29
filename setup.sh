#!/usr/bin/env bash
set -e

echo "Starting Kakarot Setup for Fedora..."

# 1. Install system dependencies via dnf
echo "Installing system dependencies..."

# 2. Set up Python virtual environment and install requirements
echo "Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
fi

# 3. Fix File-Copy Drift: create a .pth file so the daemon reads `services` natively
echo "Configuring relative repository paths..."
PTH_DIR=$(python3 -c 'import site; print(site.getsitepackages()[0])')
echo "$(pwd)" > "$PTH_DIR/kakarot.pth"

# 4. Generate and register a systemd user service
echo "Configuring systemd user service..."
SYSTEMD_USER_DIR="$HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
SERVICE_FILE="$SYSTEMD_USER_DIR/kakarot.service"

cat << EOF > "$SERVICE_FILE"
[Unit]
Description=Kakarot AI Desktop Daemon
After=network.target

[Service]
Type=simple
ExecStart=$(pwd)/venv/bin/python -m kakarot_daemon.daemon
WorkingDirectory=$(pwd)
Restart=on-failure
MemoryHigh=128M
MemoryMax=192M
Environment="PATH=$(pwd)/venv/bin:\$PATH"
Environment="PYTHONPATH=$(pwd)"
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable kakarot.service
systemctl --user restart kakarot.service

# 5. Desktop entry: install_gui.sh owns this now.
# A second launcher for the same app makes GNOME pin an extra, unmatched dock
# icon: the window's WM_CLASS is io.github.kakarot, and only the RDNN entry
# declares that StartupWMClass. Retire any leftover pre-RDNN entry instead.
echo "Checking desktop entries..."
DESKTOP_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR"
LEGACY_DESKTOP_FILE="$DESKTOP_DIR/kakarot.desktop"
if [ -f "$LEGACY_DESKTOP_FILE" ]; then
    echo "Removing superseded kakarot.desktop (use io.github.kakarot.desktop)."
    rm -f "$LEGACY_DESKTOP_FILE"
fi

update-desktop-database "$DESKTOP_DIR" || true

echo "Setup complete! The Kakarot daemon is running."
echo "Install the native GNOME front end with ./install_gui.sh"
