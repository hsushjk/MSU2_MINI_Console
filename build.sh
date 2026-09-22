#!/bin/bash

set -e

export PYTHONPATH="$PWD:$PYTHONPATH"

rm -rf build/ dist/ __pycache__/
rm -f *.spec

pyinstaller \
  --onefile \
  --clean \
  --name "MSU2_Console" \
  $EXTRA \
  --collect-submodules=msu2_core \
  --collect-submodules=widgets \
  --add-data "msu2_core:msu2_core" \
  --add-data "widgets:widgets" \
  --hidden-import=pyserial \
  --hidden-import=serial \
  --hidden-import=serial.tools.list_ports \
  --hidden-import=PIL \
  --hidden-import=PIL.Image \
  --hidden-import=PIL.ImageDraw \
  --hidden-import=PIL.ImageFont \
  --hidden-import=psutil \
  --hidden-import=pynput \
  --hidden-import=pynput.keyboard \
  --hidden-import=pynput.mouse \
  --hidden-import=dbus \
  --optimize=1 \
  msu2_mini.py

chmod +x dist/MSU2_Console

echo "succeed"
ls -lh dist/MSU2_Console