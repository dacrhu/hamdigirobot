"""HAM Digi Robot -- multi-mode amateur radio digital transceiver desktop application."""

# Single source of truth for the app's release version -- read from here by
# pyproject.toml (see [tool.hatch.version]), the About dialog
# (ui.main_window._show_about), and the macOS bundle metadata
# (packaging/hamdigirobot.spec's CFBundleShortVersionString). Bump only this line
# on a release; everything else derives from it.
__version__ = "0.1.0"
