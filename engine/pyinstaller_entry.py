"""Entry point for the frozen (PyInstaller) engine bundled inside Zip2Markdown.app."""
import sys

from zip2md.cli import main

if __name__ == "__main__":
    sys.exit(main())
