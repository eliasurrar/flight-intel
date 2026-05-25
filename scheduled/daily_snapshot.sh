#!/bin/bash
# flight-intel — daily snapshot runner.
# Runs backend/snapshot.py for all pre-defined trips and pushes the result
# to docs/ + GitHub Pages.

set -e

REPO=/Users/openclaw/projects/flight-intel
cd "$REPO"

LOG="$REPO/scheduled/logs/daily_snapshot.$(date +%Y-%m-%d).log"
mkdir -p "$REPO/scheduled/logs"

{
  echo "=== flight-intel snapshot $(date) ==="
  .venv/bin/python backend/snapshot.py 2>&1 || echo "snapshot.py exit=$?"

  # Mirror snapshots into docs/ for GitHub Pages
  mkdir -p docs/snapshots
  rsync -a --delete data/snapshots/ docs/snapshots/
  cp frontend/trips.html docs/trips.html
  cp frontend/index.html docs/index.html 2>/dev/null || true

  # Commit + push if there are changes
  if [[ -n "$(git status --porcelain docs/ data/snapshots/)" ]]; then
    git add docs/ data/snapshots/
    git commit -m "snapshot: $(date +%Y-%m-%d) $(date +%H:%M) auto" || true
    git push origin main 2>&1 || echo "push failed"
  else
    echo "no changes to commit"
  fi
  echo "=== done $(date) ==="
} >> "$LOG" 2>&1
