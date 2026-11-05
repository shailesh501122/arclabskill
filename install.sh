#!/usr/bin/env bash
# Install the arc-lab-series Claude skill.
#
#   ./install.sh               # personal: ~/.claude/skills/arc-lab-series
#   ./install.sh --project     # current project: ./.claude/skills/arc-lab-series
#   ./install.sh --dest DIR    # custom skills directory
#
# Remote one-liner (downloads this repo first):
#   curl -fsSL https://raw.githubusercontent.com/shailesh501122/arclabskill/main/install.sh | bash
set -euo pipefail

SKILL=arc-lab-series
REPO_TARBALL="${ARCLAB_TARBALL:-https://github.com/shailesh501122/arclabskill/archive/refs/heads/main.tar.gz}"
DEST_ROOT="$HOME/.claude/skills"

while [ $# -gt 0 ]; do
  case "$1" in
    --project) DEST_ROOT="$PWD/.claude/skills" ;;
    --dest) DEST_ROOT="$2"; shift ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

SRC=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/skills/$SKILL/SKILL.md" ]; then
  SRC="$SCRIPT_DIR/skills/$SKILL"
else
  TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
  echo "Downloading $REPO_TARBALL ..."
  curl -fsSL "$REPO_TARBALL" | tar -xz -C "$TMP"
  SRC="$(find "$TMP" -type d -path "*/skills/$SKILL" | head -n1)"
  [ -n "$SRC" ] || { echo "skill not found in download" >&2; exit 1; }
fi

mkdir -p "$DEST_ROOT"
rm -rf "${DEST_ROOT:?}/$SKILL"
cp -R "$SRC" "$DEST_ROOT/$SKILL"
chmod +x "$DEST_ROOT/$SKILL/scripts/render_short.py"
echo "Installed $SKILL -> $DEST_ROOT/$SKILL"

# dependencies
if ! python3 -c "import PIL, numpy" 2>/dev/null; then
  echo "Installing Pillow and numpy ..."
  python3 -m pip install --user -q -r "$DEST_ROOT/$SKILL/scripts/requirements.txt" \
    || echo "Could not install Pillow automatically; run: pip install Pillow"
fi
command -v ffmpeg >/dev/null || echo "NOTE: ffmpeg is required. Install it (apt install ffmpeg / brew install ffmpeg / winget install ffmpeg)."

python3 "$DEST_ROOT/$SKILL/scripts/render_short.py" --check || true
echo
echo "Done. In Claude Code, ask: \"Make an Arc Lab episode about <topic>\"."
