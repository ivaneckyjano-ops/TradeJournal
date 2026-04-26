import pandas as pd

from core import saved_diagonals_compare as sdc


def test_compare_orders_by_skore_when_present():
    df = pd.DataFrame(
        {
            "ID": [1, 2],
            "Ticker": ["FDX", "FDX"],
            "Čistá delta ×100": [1.0, 2.0],
            "Čistá theta (+ príjem / − strata) ×100": [5.0, 5.0],
            "Čistá vega ×100": [0.1, 0.1],
            "Debit/kredit ($/1 lot ×100)": [100.0, 200.0],
            "Skóre": [10.0, 20.0],
        }
    )
    out, md, protocol = sdc.compare_saved_diagonals(df)
    assert len(out) == 2
    assert out.iloc[0]["Poradie"] == 1
    assert int(out.iloc[0]["ID"]) == 2
    sko = str(out.iloc[0].get("Skóre/heur. (pôv.)", ""))
    assert "20" in sko or "20.0000" in sko
    assert "1. miesto" in md and "2. miesto" in md
    assert "Spôsob porovnania" in protocol and "Vstupné dáta" in protocol


def test_compare_heuristic_without_skore():
    df = pd.DataFrame(
        {
            "ID": [1, 2],
            "Čistá delta ×100": [0.5, 0.5],
            "Čistá theta (+ príjem / − strata) ×100": [8.0, 4.0],
            "Čistá vega ×100": [0.2, 0.2],
            "Debit/kredit ($/1 lot ×100)": [100.0, 100.0],
        }
    )
    out, md, _p = sdc.compare_saved_diagonals(df)
    assert int(out.iloc[0]["ID"]) == 1
    assert "heurist" in md.lower() or "theta" in md.lower() or "báz" in md.lower()


def test_better_short_bid_can_outrank_same_base_score():
    """Rovnaké Skóre: vyšší Short — bid by mal dostať lepší kompozit (likvidita)."""
    df = pd.DataFrame(
        {
            "ID": [33, 34],
            "Short — bid": [0.2, 0.75],
            "Čistá delta ×100": [1.0, 1.0],
            "Čistá theta (+ príjem / − strata) ×100": [5.0, 5.0],
            "Čistá vega ×100": [0.1, 0.1],
            "Debit/kredit ($/1 lot ×100)": [100.0, 100.0],
            "Skóre": [15.0, 15.0],
        }
    )
    out, _md, _p = sdc.compare_saved_diagonals(df)
    assert int(out.iloc[0]["ID"]) == 34
    assert float(out.iloc[0]["Short bid (kval.) 0–100"]) > float(out.iloc[1]["Short bid (kval.) 0–100"])


def test_long_strike_lt_short_still_compared():
    """Long strike < Short strike sa nevyraďuje — oba riadky sa zaradia do poradia."""
    df = pd.DataFrame(
        {
            "ID": [32, 33],
            "Short — strike": [382.5, 460.0],
            "Long — strike": [380.0, 540.0],  # ID 32: long < short (dovolené)
            "Čistá delta ×100": [1.0, 1.0],
            "Čistá theta (+ príjem / − strata) ×100": [12.0, 2.4],
            "Čistá vega ×100": [0.35, 0.06],
            "Debit/kredit ($/1 lot ×100)": [1730.0, 97.0],
            "Skóre": [296685.0, 297029.0],
        }
    )
    out, md, protocol = sdc.compare_saved_diagonals(df)
    assert len(out) == 2
    out_ids = {int(x) for x in out["ID"].tolist()}
    assert out_ids == {32, 33}
    assert "Počet v porovnaní" in md or "porovnan" in md.lower()
    assert "1. miesto" in md
    assert "32" in protocol and "33" in protocol


def test_all_long_lt_short_still_ranks_both():
    """Aj keď pri oboch long < short, oba sa porovnajú (nie prázdna tabuľka)."""
    df = pd.DataFrame(
        {
            "ID": [1, 2],
            "Short — strike": [400.0, 420.0],
            "Long — strike": [390.0, 410.0],
            "Čistá theta (+ príjem / − strata) ×100": [5.0, 3.0],
            "Debit/kredit ($/1 lot ×100)": [100.0, 80.0],
        }
    )
    out, _md, _p = sdc.compare_saved_diagonals(df)
    assert len(out) == 2
    assert {int(x) for x in out["ID"].tolist()} == {1, 2}


def test_theta_debit_delta_columns_correctly_mapped():
    """Stĺpce Θ, Debit a |Δ| musia obsahovať správne hodnoty (nie ticker alebo iný stĺpec)."""
    df = pd.DataFrame(
        {
            "ID": [1, 2],
            "Ticker": ["FDX", "FDX"],
            "Čistá delta ×100": [3.0, 5.0],
            "Čistá theta (+ príjem / − strata) ×100": [7.5, 4.2],
            "Čistá vega ×100": [0.1, 0.1],
            "Debit/kredit ($/1 lot ×100)": [500.0, 200.0],
            "Skóre": [10.0, 20.0],
        }
    )
    out, _md, _p = sdc.compare_saved_diagonals(df)
    # Θ(×100) nesmie byť "—" (to by nastalo pri starom bug-u kde sa mapoval ticker stĺpec)
    theta_val = str(out.iloc[0].get("Θ(×100)", "—"))
    assert theta_val != "—", f"Θ(×100) by malo byť číslo, nie '—'; dostali sme: {theta_val!r}"
    # Debit $ musí byť číselná hodnota (nie theta)
    debit_val = str(out.iloc[0].get("Debit $", "—"))
    assert debit_val != "—", f"Debit $ by malo byť číslo, nie '—'; dostali sme: {debit_val!r}"
