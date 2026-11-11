#!/bin/bash
# Сборка PDF2MD.app для macOS → dist/PDF2MD.app и dist/PDF2MD-mac.zip
set -e
cd "$(dirname "$0")"
PY=${PY:-python3}
[ -d .venv ] || $PY -m venv .venv
.venv/bin/python -m pip --version >/dev/null 2>&1 || .venv/bin/python -m ensurepip >/dev/null
.venv/bin/python -m pip install -q -U pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m PyInstaller --noconfirm --clean pdf2md.spec
cd dist && rm -f PDF2MD-mac.zip && ditto -c -k --keepParent PDF2MD.app PDF2MD-mac.zip
echo "Готово: dist/PDF2MD.app"
