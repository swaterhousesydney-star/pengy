#!/bin/sh
# Pengy installer.
#   curl -fsSL https://raw.githubusercontent.com/swaterhousesydney-star/pengy/main/install.sh | sh
#
# Downloads one Python file to ~/.local/bin/pengy and makes it executable.
# No dependencies, no virtualenv, no package manager. Read it before you pipe
# it — that is the deal with every install script and this one is 60 lines.
set -eu

# Defaults to the repo itself, which always exists. PENGY_SRC points it at a
# mirror or your own domain once you have one.
SRC="${PENGY_SRC:-https://raw.githubusercontent.com/swaterhousesydney-star/pengy/main}"
BIN="${PENGY_BIN:-$HOME/.local/bin}"

say() { printf '\033[95mpengy\033[0m %s\n' "$1" >&2; }
die() { printf '\033[91mpengy\033[0m %s\n' "$1" >&2; exit 1; }

# --- python -----------------------------------------------------------------
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
      PY=$(command -v "$candidate")
      break
    fi
  fi
done
[ -n "$PY" ] || die "needs Python 3.9 or newer on PATH. Install it, then run this again."

# --- fetch ------------------------------------------------------------------
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsSL "$1" -o "$2"
  elif command -v wget >/dev/null 2>&1; then wget -qO "$2" "$1"
  else die "needs curl or wget."; fi
}

mkdir -p "$BIN"
TMP=$(mktemp) || die "could not create a temp file."
trap 'rm -f "$TMP"' EXIT INT TERM

say "downloading…"
fetch "$SRC/pengy.py" "$TMP" || die "could not download $SRC/pengy.py — check the URL and your connection."
head -n 1 "$TMP" | grep -q '^#!/usr/bin/env python3' || die "that download does not look like pengy. Aborting."

# --- install ----------------------------------------------------------------
printf '#!%s\n' "$PY" > "$BIN/pengy"
tail -n +2 "$TMP" >> "$BIN/pengy"
chmod +x "$BIN/pengy"
say "installed to $BIN/pengy"

case ":$PATH:" in
  *":$BIN:"*) ;;
  *)
    say ""
    say "$BIN is not on your PATH. Add this to your shell profile:"
    say ""
    say "    export PATH=\"\$HOME/.local/bin:\$PATH\""
    say ""
    ;;
esac

"$BIN/pengy" doctor || true
say ""
say 'try:  pengy run "write the tests for src/checkout.ts"'
