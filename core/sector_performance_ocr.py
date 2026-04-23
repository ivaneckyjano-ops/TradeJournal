"""
OCR a parsovanie tabuliek výkonnosti sektorov (napr. screenshot z Barchart).

Tesseract musí byť nainštalovaný v OS; Python balíky: ``pytesseract``, ``opencv-python-headless``.
"""

from __future__ import annotations

import re
import numpy as np
import pandas as pd

_FLOAT_TOKEN = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*%?")


def ocr_stack_available() -> bool:
    try:
        import cv2  # noqa: F401
        import pytesseract  # noqa: F401

        return True
    except ImportError:
        return False


# Široké „horizontálne“ tabuľky (Barchart) majú malú výšku — staré škálovanie podľa max(h,w)
# obrázok zmenšovalo a Tesseract prečítal len zlomok riadkov.
_MIN_HEIGHT_PX = 880
_MAX_UPSCALE = 4.0
_MAX_LONG_SIDE_PX = 3400
_STRIP_WIDTH_PX = 2400
_STRIP_OVERLAP_PX = 280


def _resize_gray_for_table_ocr(gray: np.ndarray) -> np.ndarray:
    """
    Zväčší nízku výšku (typicky široký screenshot tabuľky).

    Ak je po zväčšení najdlhší bok stále nad limitom, zmenší sa **jednotne** len vtedy,
    ak výška riadkov ostane aspoň ~88 % cieľa — inak necháme väčší obrázok a šírku
    spracuje ``_ocr_binary_tiled`` (inak by sa znova zmenšila výška a OCR by čítal len zlomok).
    """
    import cv2

    h, w = int(gray.shape[0]), int(gray.shape[1])
    if h < 1 or w < 1:
        return gray
    sf = 1.0
    if h < _MIN_HEIGHT_PX:
        sf = min(_MAX_UPSCALE, float(_MIN_HEIGHT_PX) / float(h))
    nh, nw = int(round(h * sf)), int(round(w * sf))
    md = max(nh, nw)
    if md > _MAX_LONG_SIDE_PX:
        s2 = float(_MAX_LONG_SIDE_PX) / float(md)
        nh2, nw2 = int(round(nh * s2)), int(round(nw * s2))
        landscape = nw >= nh
        # Široký „nízky“ snímok: zmenšenie podľa max. boku by zničilo výšku riadkov → tiling namiesto toho
        if landscape and nh2 < int(_MIN_HEIGHT_PX * 0.88):
            pass
        else:
            nh, nw = nh2, nw2
    if nh == h and nw == w:
        return gray
    return cv2.resize(gray, (nw, nh), interpolation=cv2.INTER_CUBIC)


def _binarize(gray: np.ndarray) -> np.ndarray:
    import cv2

    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    _, th = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return th


def _ocr_binary_tiled(th: np.ndarray, *, psm: int) -> str:
    """Pre veľmi široké tabuľky OCR po zvislých prúžkoch (celá výška), aby sa neorezali stĺpce."""
    import pytesseract

    h, w = int(th.shape[0]), int(th.shape[1])
    cfg = f"--psm {int(psm)}"
    if w <= _STRIP_WIDTH_PX:
        return pytesseract.image_to_string(th, config=cfg)
    texts: list[str] = []
    x = 0
    step = max(_STRIP_WIDTH_PX - _STRIP_OVERLAP_PX, _STRIP_WIDTH_PX // 2)
    while x < w:
        x2 = min(w, x + _STRIP_WIDTH_PX)
        strip = th[:, x:x2]
        texts.append(pytesseract.image_to_string(strip, config=cfg))
        if x2 >= w:
            break
        x += step
    return "\n".join(texts)


def ocr_image_bytes_to_text(image_bytes: bytes, *, psm: int = 6) -> str:
    """
    Predspracovanie (OTSU) + Tesseract.

    Pri **nízkej výške** (typicky široký screenshot celej tabuľky) obrázok najprv **zväčšíme**,
    aby mali riadky dostatok pixelov; pri veľkej šírke OCR po **prúžkoch** zľava doprava.
    """
    import cv2

    nparr = np.frombuffer(image_bytes, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("Nepodarilo sa dekódovať obrázok (cv2.imdecode).")
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = _resize_gray_for_table_ocr(gray)
    th = _binarize(gray)
    return _ocr_binary_tiled(th, psm=psm)


def _header_line(line: str) -> bool:
    u = line.lower()
    keys = (
        "1-day",
        "1 day",
        "5-day",
        "5 day",
        "1-month",
        "1 month",
        "3-month",
        "3 month",
        "ytd",
        "1-year",
        "1 year",
        "weight",
        "% weight",
        "sector",
        "industry",
        "name",
        "symbol",
    )
    hits = sum(1 for k in keys if k in u)
    return hits >= 2


def parse_sector_performance_text(raw: str) -> pd.DataFrame:
    """
    Z OCR textu vytiahne riadky ``sector`` + percentá (posledných 5 čísel = 1d, 5d, 1m, 3m, 1y).

    Ak je v riadku stĺpec váhy pred výkonmi, očakáva sa, že percentá výkonu sú **posledné**
    číselné tokeny v riadku (váha sa tak „spadne“ mimo tail).
    """
    rows: list[dict] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or len(line) < 4:
            continue
        if _header_line(line):
            continue
        m = re.search(r"-?\d", line)
        if not m:
            continue
        sector = line[: m.start()].strip(" |-–—\t")
        if len(sector) < 2:
            continue
        tail = line[m.start() :]
        nums = [float(x.replace(",", ".")) for x in _FLOAT_TOKEN.findall(tail)]
        if len(nums) >= 5:
            t = nums[-5:]
            rows.append(
                {
                    "sector": sector,
                    "pct_1d": t[0],
                    "pct_5d": t[1],
                    "pct_1m": t[2],
                    "pct_3m": t[3],
                    "pct_1y": t[4],
                }
            )
        elif len(nums) == 4:
            rows.append(
                {
                    "sector": sector,
                    "pct_1d": nums[0],
                    "pct_5d": nums[1],
                    "pct_1m": nums[2],
                    "pct_3m": nums[3],
                    "pct_1y": None,
                }
            )
    return normalize_sector_dataframe(pd.DataFrame(rows))


def normalize_sector_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["sector", "pct_1d", "pct_5d", "pct_1m", "pct_3m", "pct_1y"])
    out = df.copy()
    out["sector"] = (
        out["sector"].astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    )
    for c in ("pct_1d", "pct_5d", "pct_1m", "pct_3m", "pct_1y"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out.drop_duplicates(subset=["sector"], keep="last").reset_index(drop=True)


def dataframe_to_payload_rows(df: pd.DataFrame) -> dict:
    """Serializovateľný payload pre ``insert_sector_performance_snapshot``."""
    dfn = normalize_sector_dataframe(df)
    records = dfn.replace({np.nan: None}).to_dict(orient="records")
    return {"rows": records}


def payload_rows_to_dataframe(payload: dict) -> pd.DataFrame:
    rows = (payload or {}).get("rows") or []
    if not rows:
        return normalize_sector_dataframe(pd.DataFrame())
    return normalize_sector_dataframe(pd.DataFrame(rows))
