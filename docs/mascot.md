# Floating Kakarot mascot

Install the desktop runtime and PyQt6 from the repository root:

```sh
python3 -m pip install -r requirements-mascot.txt
python3 -m kakarot_overlay
```

The mascot reads the bundled `avatar.png` (or `kakarot_cover.png`) relative
to the checkout, then talks to the running daemon through its private
per-user socket at `$XDG_RUNTIME_DIR/kakarot/kakarot.sock` (or the configured
`KAKAROT_RUNTIME_DIR`). It polls daemon status without blocking the GUI and
routes submitted commands through the daemon's sandbox and audit pipeline.
No system tray or shell extension is required.
