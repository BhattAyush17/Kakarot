"""Allow ``python3 -m kakarot_gui`` to launch the front end."""

from __future__ import annotations

import sys

from kakarot_gui.app import main

if __name__ == "__main__":
    sys.exit(main())
