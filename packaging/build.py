"""配布用バイナリを作る（macOS: .app の zip、Windows: フォルダの zip）。

  uv run --group build python packaging/build.py

手元でも GitHub Actions（.github/workflows/build.yml）でも同じこのスクリプトを使う。
PyInstaller はクロスコンパイルできないので、Windows 版は Windows 上（Actions）で作る。

  出力: dist/book-viewer-<版>-macos-<arch>.zip   … Book Viewer.app
        dist/book-viewer-<版>-windows-x64.zip    … Book Viewer/Book Viewer.exe ほか

onefile（exe 1 つ）にしないのは、PySide6 が大きく、起動のたびに一時フォルダへ展開して遅くなるため。
"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_NAME = "Book Viewer"


def version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from book_viewer import __version__
    return __version__


def build() -> Path:
    args = [
        sys.executable, "-m", "PyInstaller",
        str(ROOT / "packaging" / "launcher.py"),
        "--name", APP_NAME,
        "--windowed",                   # コンソールを出さない / macOS は .app を作る
        "--noconfirm", "--clean",
        "--paths", str(ROOT / "src"),
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
        "--collect-submodules", "book_viewer",
    ]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "io.github.mashi727.book-viewer"]
    subprocess.run(args, check=True, cwd=ROOT)
    return ROOT / "dist"


def archive(dist: Path) -> Path:
    ver = version()
    if sys.platform == "darwin":
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        out = dist / f"book-viewer-{ver}-macos-{arch}.zip"
        # .app の中の Qt フレームワークはシンボリックリンクを含むので、shutil ではなく ditto で固める
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
                        str(dist / f"{APP_NAME}.app"), str(out)], check=True)
    elif sys.platform == "win32":
        out = dist / f"book-viewer-{ver}-windows-x64.zip"
        shutil.make_archive(str(out.with_suffix("")), "zip", dist, APP_NAME)
    else:
        out = dist / f"book-viewer-{ver}-linux-{platform.machine()}.tar.gz"
        shutil.make_archive(str(out).removesuffix(".tar.gz"), "gztar", dist, APP_NAME)
    return out


def executable(dist: Path) -> Path:
    if sys.platform == "darwin":
        return dist / f"{APP_NAME}.app" / "Contents" / "MacOS" / APP_NAME
    if sys.platform == "win32":
        return dist / APP_NAME / f"{APP_NAME}.exe"
    return dist / APP_NAME / APP_NAME


def main() -> int:
    dist = build()
    out = archive(dist)
    print(f"built: {out}  ({out.stat().st_size / 1e6:.1f} MB)")
    print(f"executable: {executable(dist)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
