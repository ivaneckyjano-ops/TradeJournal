"""Prahové hodnoty Steady Yields (delta shortu = abs(delta), DTE v dňoch)."""

# Semafor
DELTA_GREEN_MAX = 0.25
DELTA_ORANGE_MAX = 0.35
DELTA_RED_MIN = 0.40
ROLL_DTE_TRIGGER = 21

# Roll / kredit
DEFAULT_SLIPPAGE_USD_PER_CONTRACT = 0.02  # rezerva na strane kontraktu (×100 = $2 na kontrakt ak ×100)

# Scanner (predvolené)
DEFAULT_MIN_OPEN_INTEREST = 500
DEFAULT_MAX_SPREAD_PCT_MID = 2.0
DEFAULT_MAX_IV_RANK_ENTRY = 30.0
DEFAULT_MAX_TICKERS_PER_SECTOR = 3
