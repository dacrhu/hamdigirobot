"""PyInstaller entry point for the Windows/macOS builds.

PyInstaller needs a concrete .py script to analyze, not a
package:function reference like the "hamdigirobot" console-script entry point
pyproject.toml declares -- this tiny bootstrap just calls straight into
that same function. See .github/workflows/windows-macos-build.yml for how
it's invoked (`pyinstaller packaging/hamdigirobot.spec`, which points Analysis
at this file), and packaging/build_appimage.sh for the Linux AppImage's
own (unrelated) build path, which doesn't use PyInstaller at all.
"""

import sys

from hamdigirobot.app import main

if __name__ == "__main__":
    sys.exit(main())
