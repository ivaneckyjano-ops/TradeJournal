#!/bin/bash
# Backup script for TradeJournal
cd "$(dirname "$0")"
git add .
TIMESTAMP=$(date +"%Y-%m-%d %H:%M:%S")
git commit -m "Auto-backup: $TIMESTAMP"
git push origin main
