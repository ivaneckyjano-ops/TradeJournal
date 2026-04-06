#!/usr/bin/env bash
# Vytvorí .venv v koreni TradeJournal a nainštaluje requirements.txt (obíde PEP 668).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
echo ""
echo "OK. Aktivuj prostredie:  source .venv/bin/activate"
echo "Spustenie aplikácie:     streamlit run streamlit_app.py"
