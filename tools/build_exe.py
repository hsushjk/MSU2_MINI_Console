import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEP = ";" if os.name == "nt" else ":"
DATA_DIRS = ("msu2_core", "widgets", "assets")
HIDDEN = (
    "serial",
    "serial.tools.list_ports",
    "PIL",
    "PIL.Image",
    "PIL.ImageDraw",
    "PIL.ImageFont",
    "PIL.ImageOps",
    "psutil",
    "pynput",
    "pynput.keyboard",
    "pynput.mouse",
    "numpy",
)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("console", "noconsole"), default="console")
    ap.add_argument("--name", default="MSU2_Console")
    ap.add_argument("--dist", default=str(ROOT / "dist"))
    ap.add_argument("--work", default=str(ROOT / "build"))
    ap.add_argument("--icon", default="")
    ap.add_argument("--no-numpy", action="store_true")
    ap.add_argument("--no-clean", action="store_true")
    args = ap.parse_args(argv)

    dist = Path(args.dist).resolve()
    work = Path(args.work).resolve()
    dist.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--onefile",
        "--noconfirm",
        "--optimize",
        "1",
        "--name",
        args.name,
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(work),
    ]
    if not args.no_clean:
        cmd.append("--clean")
    if args.mode == "console":
        cmd.append("--console")
    else:
        cmd.append("--windowed")
    icon = Path(args.icon) if args.icon else ROOT / "icon.ico"
    if os.name == "nt" and icon.is_file():
        cmd += ["--icon", str(icon.resolve())]
    for name in DATA_DIRS:
        src = ROOT / name
        if src.is_dir():
            cmd += ["--add-data", "%s%s%s" % (src, SEP, name)]
    for pkg in ("msu2_core", "widgets"):
        cmd += ["--collect-submodules", pkg]
    for mod in HIDDEN:
        if args.no_numpy and mod == "numpy":
            cmd += ["--exclude-module", "numpy"]
            continue
        try:
            found = importlib.util.find_spec(mod) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            cmd += ["--exclude-module", mod]
            continue
        cmd.append("--hidden-import")
        cmd.append(mod)
    cmd.append(str(ROOT / "msu2_mini.py"))

    print(" ".join(cmd))
    code = subprocess.call(cmd, cwd=str(ROOT))
    if code != 0:
        return code

    suffix = ".exe" if os.name == "nt" else ""
    out = dist / (args.name + suffix)
    print("OUTPUT %s %d" % (out, out.stat().st_size if out.is_file() else -1))
    return 0


if __name__ == "__main__":
    sys.exit(main())