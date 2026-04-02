#!/bin/bash
# Spustí TradeJournal Streamlit aplikáciu
cd "$(dirname "$0")"
pkill -f "streamlit run streamlit_app" 2>/dev/null
sleep 2
echo "Spúšťam TradeJournal na http://localhost:8501 ..."
.venv/bin/streamlit run streamlit_app.py --server.headless true --server.port 8501
