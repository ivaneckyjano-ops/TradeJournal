import os
import tempfile

import pandas as pd

from core import saved_diagonals_db as sdb


def test_save_list_delete_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        old = sdb._DB_PATH
        sdb._DB_PATH = os.path.join(td, "saved_diagonals.db")
        try:
            df = pd.DataFrame(
                {
                    "Stratégia": ["Test stratégia"],
                    "Typ": ["Call"],
                    "Čistá delta": [0.01],
                    "Debit/kredit ($/1 lot ×100)": [150.0],
                }
            )
            n = sdb.save_rows("TEST", "2026-04-16", "long_call_diagonal", df)
            assert n == 1
            out = sdb.list_saved()
            assert len(out) == 1
            assert out.iloc[0]["Ticker"] == "TEST"
            assert abs(float(out.iloc[0]["Debit/kredit ($/1 lot ×100)"]) - 150.0) < 1e-6
            rid = int(out.iloc[0]["ID"])
            assert sdb.delete_by_ids([rid]) == 1
            assert sdb.list_saved().empty
        finally:
            sdb._DB_PATH = old
