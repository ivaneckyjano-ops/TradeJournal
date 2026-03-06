import streamlit as st

st.title("Pomocník — Návod na použitie")

st.markdown("""
> Denník je **čisto lokálny** — nikdy neposiela príkazy do TWS. Všetky zmeny sú len v tvojej SQLite databáze.
""")

# ─── Navigácia ────────────────────────────────────────────────────────────────
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "Začíname", "Trade Log", "Konzultácie", "Roll Simulátor", "IBKR Pripojenie"
])

# ══════════════════════════════════════════════════════════════════════════════
with tab1:
    st.header("Začíname — prvé kroky")

    st.subheader("1. Spustenie aplikácie")
    st.code("Journal", language="bash")
    st.markdown("Otvor prehliadač na **http://localhost:8501**")

    st.subheader("2. Prepoj sa na IBKR (voliteľné)")
    st.markdown("""
- Spusti **TWS** alebo **IB Gateway**
- V TWS: `Edit → Global Configuration → API → Settings`
  - ✅ Enable ActiveX and Socket Clients
  - ❌ Read-Only API — **zruš zaškrtnutie** (inak nebude fungovať import)
- V denníku: **Dashboard → IBKR Pripojenie → Pripojiť**
  - Port **7496** = TWS Live
  - Port **7497** = TWS Paper
""")

    st.subheader("3. Importuj pozície")
    st.markdown("""
- **Dashboard → "Importuj pozície z IBKR"** — načíta aktuálne otvorené opčné pozície
- **Dashboard → "Importuj Fills"** — načíta obchody vykonané v aktuálnej TWS session
- Staršie uzavreté obchody zadaj **manuálne** v Trade Log
""")

    st.subheader("4. Zoskup nohy stratégie")
    st.markdown("""
- **Trade Log → Upraviť / Zoskupiť**
- Každej nohe nastav rovnaké **Group ID** (napr. `AMZN_DIA_MAR26`)
- Group ID prepojí nohy v analytike, Timeline a Poznámkach
""")

    st.subheader("5. Sleduj vývoj stratégie")
    st.markdown("""
- **Konzultácie → Strategy Timeline** — chronologický príbeh stratégie
- Každý vstup, výstup a poznámka zoradené podľa dátumu
""")

# ══════════════════════════════════════════════════════════════════════════════
with tab2:
    st.header("Trade Log")

    st.subheader("Pridať obchod manuálne")
    st.markdown("""
| Pole | Popis |
|---|---|
| **Ticker** | Symbol akcie (napr. AMZN) |
| **Stratégia** | Typ stratégie (Diagonal, Iron Condor...) |
| **Group ID** | Identifikátor skupiny nôh — rovnaký pre všetky nohy jednej stratégie |
| **Typ nohy** | Short = predávaná opcia, Long = kupovaná opcia |
| **Opcia** | Call alebo Put |
| **Strike** | Realizačná cena opcie |
| **Expiry** | Dátum expirácie |
| **Kontrakty** | Počet kontraktov (1 kontrakt = 100 akcií) |
| **Entry cena** | Zaplatená/prijatá prémia za 1 kontrakt |
| **IV** | Implied Volatility pri vstupe (napr. 0.30 = 30%) — slúži na výpočet PoP |
| **Spot** | Cena akcie pri vstupe — slúži na výpočet PoP |
""")

    st.subheader("Uzavrieť pozíciu")
    st.markdown("""
- Záložka **"Otvorené pozície"** → sekcia **"Uzavrieť pozíciu"**
- Vyber obchod, zadaj Exit cenu a dátum
- Pozícia sa označí ako **Closed** a vypočíta sa P&L
- ⚠️ Toto **neposiela príkaz do TWS** — je to len záznam v denníku
""")

    st.subheader("Rozdeliť pozíciu (napr. 2 kontrakty → 2× 1 kontrakt)")
    st.markdown("""
- Záložka **"Upraviť / Zoskupiť"** → sekcia **"Rozdeliť pozíciu"**
- Vhodné keď IBKR importuje Long ×2 ale v skutočnosti sú to 2 samostatné nohy v rôznych stratégiách
- Zadaj rôzne Group ID pre každú nohu
""")

    st.subheader("P&L výpočet")
    st.markdown("""
```
P&L = (Exit cena − Entry cena) × Kontrakty × 100

Short noga: profit = cena klesla (predal si draho, kupuješ lacno)
Long noga:  profit = cena stúpla (kúpil si lacno, predávaš draho)
```
""")

    st.subheader("Čo je Group ID?")
    st.markdown("""
Group ID je ľubovoľný text ktorý prepojí viacero nôh do jednej stratégie.

**Príklad — Diagonal AMZN:**
- Short AMZN May 215 Call → Group ID: `AMZN_DIA_MAR26`
- Long AMZN Jul 205 Call → Group ID: `AMZN_DIA_MAR26`

**Odporúčaný formát:** `TICKER_STRATÉGIA_MESIACROK`
napr. `AMZN_DIA_MAR26`, `TSLA_IC_APR26`, `SPY_STRANGLE_MAY26`
""")

# ══════════════════════════════════════════════════════════════════════════════
with tab3:
    st.header("Konzultácie a Poznámky")

    st.subheader("Nová poznámka")
    st.markdown("""
- Podporuje **Markdown** formátovanie
- Priraď k **Trade ID** (konkrétna noha) alebo **Group ID** (celá stratégia)
- Príklady použitia:
  - Dôvod vstupu do pozície
  - Plán pri rolovaní
  - Analýza pred expiraciou
  - Výsledok a ponaučenie
""")

    st.subheader("Markdown — základné formátovanie")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown("**Zdrojový text:**")
        st.code("""## Nadpis
**tučné**, *kurzíva*

- odrážka 1
- odrážka 2

> citát / poznámka

`kód`
""", language="markdown")
    with col2:
        st.markdown("**Výsledok:**")
        st.markdown("""## Nadpis
**tučné**, *kurzíva*

- odrážka 1
- odrážka 2

> citát / poznámka

`kód`
""")

    st.subheader("Strategy Timeline")
    st.markdown("""
- Vyber **Group ID** a uvidíš celý príbeh stratégie chronologicky
- Zobrazuje: vstupy (🟢 Long / 🔴 Short), výstupy (⬛), poznámky (📝)
- Ideálne na revíziu stratégie po expirácii
""")

# ══════════════════════════════════════════════════════════════════════════════
with tab4:
    st.header("Roll Simulátor")

    st.subheader("Na čo slúži")
    st.markdown("""
Simuluje **zmenu strike pri rolovaní** a okamžite ukazuje:
- Novú **Probability of Profit (PoP)**
- **SD pásma** (1SD ~68%, 2SD ~95%)
- **Bell curve** — rozdelenie pravdepodobnosti cien pri expirácii
- **PoP Sweep** — ako sa PoP mení pri rôznych strike hodnotách
""")

    st.subheader("SD Línie — vzorec")
    st.markdown("""
```
SD pohyb = Spot × IV × √(DTE / 365)

1SD pásmo = Spot ± SD pohyb     → ~68% pravdepodobnosť zostať vnútri
2SD pásmo = Spot ± 2×SD pohyb   → ~95% pravdepodobnosť zostať vnútri
```

**Príklad:** AMZN = $200, IV = 30%, DTE = 30 dní
- SD pohyb = 200 × 0.30 × √(30/365) = **±$17.20**
- 1SD: $182.80 – $217.20
- 2SD: $165.60 – $234.40
""")

    st.subheader("PoP — Probability of Profit")
    st.markdown("""
Vypočítava sa cez **Black-Scholes model (N(d2))**:

| Typ | Vzorec | Interpretácia |
|---|---|---|
| Short Call | N(−d2) | Pravdepodob. že cena zostane **pod** strike |
| Short Put | N(d2) | Pravdepodob. že cena zostane **nad** strike |
| Diagonal | PoP short nohy | Aproximácia — PoP celého spreadu |

> PoP je **teoretická pravdepodobnosť** — nezohľadňuje IV skew ani volatility smile.
""")

    st.subheader("Ako používať Roll Simulátor")
    st.markdown("""
1. Zadaj **Spot** cenu, **IV** a **DTE**
2. Vyber typ stratégie (Short Call, Diagonal...)
3. Pôvodný strike zadaj ako aktuálny strike tvojej pozície
4. Posúvaj **slider** na nový strike (roll target)
5. Sleduj zmenu PoP a porovnávaciu tabuľku
6. Záložka **"PoP Sweep"** ukáže optimálne strike pásmo
""")

# ══════════════════════════════════════════════════════════════════════════════
with tab5:
    st.header("IBKR Pripojenie")

    st.subheader("Porty")
    st.markdown("""
| Port | Popis |
|---|---|
| **7496** | TWS — Live účet |
| **7497** | TWS — Paper trading |
| **4001** | IB Gateway — Live |
| **4002** | IB Gateway — Paper |
""")

    st.subheader("Nastavenie TWS")
    st.markdown("""
1. `Edit → Global Configuration → API → Settings`
2. ✅ **Enable ActiveX and Socket Clients** — zapnúť
3. ❌ **Read-Only API** — vypnúť (inak import nefunguje)
4. **Trusted IP Addresses** — pridaj `127.0.0.1`
5. **Socket port** — nastav na 7496 alebo 7497
6. Reštartuj TWS po zmene nastavení
""")

    st.subheader("Client ID")
    st.markdown("""
- Každé pripojenie musí mať unikátne **Client ID**
- Denník používa ID 10 (automaticky skúša 10–15 ak je obsadené)
- Ak sa nedá pripojiť: v TWS skontroluj `API → Active Connections`
  a odpoj staré session
""")

    st.subheader("Čo funguje BEZ pripojenia")
    st.markdown("""
- Manuálne zadávanie obchodov ✅
- SD Línie a PoP výpočty (s manuálne zadanou IV a Spot cenou) ✅
- Roll Simulátor ✅
- Poznámky a Timeline ✅
""")

    st.subheader("Čo vyžaduje pripojenie")
    st.markdown("""
- Import pozícií z portfólia ✅ (Read-Only)
- Import Fills (história exekúcií) ✅ (Read-Only)
- Automatické načítanie Spot ceny pre SD línie ✅ (Read-Only)
""")

    st.divider()
    st.info("Všetky import funkcie sú **Read-Only** — denník nikdy neposiela príkazy do TWS.")
