#!/usr/bin/env bash
# One paper-trading cycle on the server: pull, scan, simulate, publish data.json.
# Never places orders. Called every 5 minutes by the nexa-quant systemd timer.
set -uo pipefail
APP=/opt/nexa-quant
cd "$APP/repo"
export PATH="$APP/venv/bin:$PATH"
# Credentials for the read-only account check (optional).
[ -f /etc/nexa-quant.env ] && set -a && . /etc/nexa-quant.env && set +a

git rebase --abort >/dev/null 2>&1 || true
git fetch -q origin main && git reset -q --hard origin/main

python scan.py
python paper.py
python news.py
python account_check.py

git add data.json
if ! git diff --cached --quiet; then
  git commit -q -m "chore: refresh read-only market snapshot [skip ci]"
  for attempt in 1 2 3; do
    git push -q origin HEAD:main && break
    git pull -q --rebase origin main
  done
fi
