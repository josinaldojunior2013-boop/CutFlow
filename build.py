"""Build a one-file Windows executable with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("Execute build.py no Windows para gerar o arquivo .exe.")

    project_dir = Path(__file__).resolve().parent
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--windowed",
        "--collect-all",
        "imageio_ffmpeg",
        "--name",
        "CapCutLightRenderer",
        str(project_dir / "renderer.py"),
    ]
    subprocess.run(command, cwd=project_dir, check=True)


if __name__ == "__main__":
    main()