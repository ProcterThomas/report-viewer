#!/usr/bin/env bash
# Install read-aloud for the report viewer: a Python venv in readaloud/.venv with Piper TTS and PyMuPDF, and the
# en_US-lessac-medium voice (~60 MB) in readaloud/voices, checked against the sha256 sums in requirements-service.txt.
# Needs internet once. Run again to repair; it skips what is already there.
set -euo pipefail
cd "$(dirname "$0")/readaloud"
PY=${PYTHON:-python3}
VOICE=en_US-lessac-medium

# The venv: uv if it's installed, else python -m venv; where ensurepip is missing (Debian/Ubuntu without
# python3-venv), a bare venv plus pip's official get-pip.py.
if [ ! -x .venv/bin/python ]; then
  if command -v uv >/dev/null; then
    uv venv --quiet --python "$PY" .venv
  elif ! "$PY" -m venv .venv 2>/dev/null; then
    rm -rf .venv
    "$PY" -m venv --without-pip .venv
    curl -fsSL https://bootstrap.pypa.io/get-pip.py -o .venv/get-pip.py
    .venv/bin/python .venv/get-pip.py --quiet
  fi
  echo "Created readaloud/.venv with $(.venv/bin/python --version)"
fi
if command -v uv >/dev/null && ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  pipi() { uv pip install --quiet --python .venv/bin/python "$@"; }
else
  pipi() { .venv/bin/python -m pip install --quiet "$@"; }
fi
# Exact pins were tested with Python 3.14. On another Python they may have no wheels: fall back to the same Piper
# version (readalong_server relies on its internals) and current PyMuPDF/onnx.
if ! pipi -r requirements-service.txt; then
  echo "Pinned install failed on this Python; trying piper-tts==1.8.0 with current pymupdf and onnx"
  pipi "piper-tts==1.8.0" pymupdf onnx
fi

mkdir -p voices
if [ ! -f "voices/$VOICE.onnx" ] || [ ! -f "voices/$VOICE.onnx.json" ]; then
  echo "Downloading the $VOICE voice"
  .venv/bin/python -m piper.download_voices "$VOICE" --download-dir voices
fi
sum() { if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi | cut -d' ' -f1; }
ok=1
for f in "$VOICE.onnx" "$VOICE.onnx.json"; do
  want=$(grep -E "^#\s+$f\s" requirements-service.txt | awk '{print $NF}')
  got=$(sum "voices/$f")
  if [ "$want" != "$got" ]; then echo "CHECKSUM MISMATCH for voices/$f (expected $want, got $got)"; ok=0; fi
done
[ $ok = 1 ] || { echo "Delete readaloud/voices and run this again."; exit 1; }

echo "Checking that the voice loads"
.venv/bin/python - <<'EOF'
from pathlib import Path
from pdf_align import load_voice
load_voice(Path("voices/en_US-lessac-medium.onnx"), Path("voices/en_US-lessac-medium.onnx.json"), intra_op_threads=1, cpu_mem_arena=False)
print("Read-aloud is ready. Restart report_viewer.py if it is running.")
EOF
