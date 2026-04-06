# TradeJournal — Inteligentný Opčný Denník

Streamlit aplikácia pre sledovanie a analýzu opčných obchodov s prepojením na Interactive Brokers.

## Funkcie

- **Dashboard** — SD línie (1SD/2SD) z IV, PoP pre aktuálne pozície, IBKR pripojenie
- **Trade Log** — Zadávanie obchodov (single/multi-leg), P&L sledovanie
- **Konzultácie & Poznámky** — Markdown záznamy priradené k Trade_ID / Group_ID, historia log
- **Strategy Modeler** — Roll simulátor so sliderom pre strike, real-time PoP aktualizácia

## Inštalácia

Na systémoch s **PEP 668** (napr. Ubuntu 24.04, Debian 12+) globálny `pip install` zlyhá s chybou *externally-managed-environment*. Použi **virtuálne prostredie**:

```bash
cd /cesta/k/TradeJournal
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Alebo jedným príkazom (z koreňa repozitára):

```bash
bash scripts/setup_venv.sh && source .venv/bin/activate
```

Potom vždy pred prácou: `source .venv/bin/activate` (alebo spúšťaj príkazy cez `.venv/bin/python` / `.venv/bin/streamlit`).

## Spustenie

```bash
source .venv/bin/activate   # ak ešte nie je aktivované
streamlit run streamlit_app.py
```

## IBKR Pripojenie

- TWS Paper Trading: port **7497**
- TWS Live: port **7496**
- IB Gateway Live: port **4001**
- IB Gateway Paper: port **4002**

Uisti sa, že v TWS/Gateway máš povolené API pripojenie:
`Edit → Global Configuration → API → Settings → Enable ActiveX and Socket Clients`
