#!/usr/bin/env bash
# PostScript (TWS screenshot) → PNG → text (Tesseract). Vyžaduje: gs, tesseract.
set -euo pipefail
PS_FILE="${1:?Použitie: $0 cesta/k/suboru.ps [výstupný_adresár]}"
OUT_DIR="${2:-./ps_ocr_out}"
mkdir -p "$OUT_DIR"
BASE="$(basename "$PS_FILE" .ps)"
PNG="$OUT_DIR/${BASE}.png"
TXT="$OUT_DIR/${BASE}_ocr.txt"
TXT_NUM="$OUT_DIR/${BASE}_numbers.txt"

gs -q -dNOPAUSE -dBATCH -sDEVICE=png16m -r300 -sOutputFile="$PNG" "$PS_FILE"
tesseract "$PNG" "${TXT%.txt}" -l eng --psm 6
# len číselné tokeny (orientačné; dátumy sa rozpadnú na časti)
grep -oE '[-+]?[0-9]+\.?[0-9]*' "$TXT" | grep -vE '^[0-9]{1,2}$' | head -500 > "$TXT_NUM" || true
echo "OK: $PNG"
echo "    $TXT"
echo "    $TXT_NUM (filtrované čísla, prvých 500)"
