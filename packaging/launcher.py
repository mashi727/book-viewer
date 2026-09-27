"""PyInstaller の入口（パッケージの外に置き、相対 import を含む book_viewer を普通に import する）。"""
import sys

from book_viewer.app import main

sys.exit(main())
