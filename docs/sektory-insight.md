# Manuál: Sektory — insight z tabuliek

Stránka spracuje **screenshoty tabuliek výkonnosti sektorov** (napr. z Barchart), uloží ich ako **snímky** do lokálnej databázy a z nich + z **otvorených pozícií v denníku** vygeneruje textové **varovania**, **prehľad podobných sektorov** a **návrhy diverzifikácie**.

Dôležité: ide o **podobnosť výkonnostných vektorov** v tabuľke (percentá za 1 deň, 5 dní, …), **nie** o štatistickú koreláciu cien podkladov.

---

## 1. Čo musíš mať pripravené

1. **Obrázky tabuliek** — ideálne ostrý výrez len na tabuľku (menej šumu = lepší OCR). Podporované sú bežné formáty (PNG, JPG, …).
2. **Dva typy snímku**
   - **Krátkodobý** — tabuľka s kratšími horizontmi (napr. dnes, 5 dní, mesiac, 3 mesiace), podľa toho, čo pravidelne exportuješ z Barchart.
   - **Dlhodobý** — tabuľka s dlhšími horizontmi (napr. vrátane roka), ak ju máš; report vie fungovať aj **len s krátkodobým** snímkom, ale časť textov (dlhodobý výkon, doplnenie k 1r) bude chýbať.
3. **Sektory v Symboly** — výber **Sektor** v záložke **Symboly** má **rovnaké názvy riadkov** ako **posledný uložený krátkodobý** snímok tabuľky z Barchart (často po anglicky, presne ako v tabuľke). Expander **„Prehľad sektorov“** na stránke **Sektory — insight** zobrazuje ten istý zoznam. Reporty pod kapotou tieto názvy mapujú na spoločné štítky (`journal_sector_from_table_row_name` v `core/journal_sectors.py`).
4. **Otvorené obchody** — váhy v reporte berú z **Trade Logu** len **otvorené** nohy; orientačne sa používa súčet **|vstupná prémia| × kontrakty × 100** na ticker, potom agregácia na sektor.

### Prehľad sektorov (Barchart)

Expander **„Prehľad sektorov (Barchart → rovnaký zoznam ako v Symboly)“** generuje `barchart_insight_sector_guide_markdown()` v `core/sector_select_options.py`.

---

## 2. Inštalácia OCR (voliteľné, ale odporúčané)

- **Python:** v prostredí, v ktorom spúšťaš Streamlit, nainštaluj závislosti z `requirements.txt` (najmä `pytesseract`, `opencv-python-headless`).
- **Systém:** nainštaluj balík **Tesseract OCR** (`tesseract` musí ísť spustiť z terminálu). Bez neho stránka stále funguje, ale tlačidlo **Spustiť OCR z obrázka** zlyhá — môžeš namiesto toho **vložiť text** z tabuľky (napr. skopírovaný z iného nástroja) a použiť **Parsovať text z poľa**.

---

## 3. Postup na stránke

### 3.1 Krátkodobý a dlhodobý stĺpec

V ľavom a pravom bloku:

| Krok | Čo urobíš |
|------|-----------|
| 1 | Nahraj **Obrázok tabuľky** alebo nechaj obrázok prázdny a priprav **text** do poľa nižšie. |
| 2 | **Spustiť OCR z obrázka** — po spracovaní sa v expanderi **Surový text z posledného OCR** zobrazí surový výstup (pre kontrolu chýb). |
| 3 | **Parsovať text z poľa** — parsuje sa **iba** obsah textového poľa (po kliknutí), aby sa tabuľka pri každom obnovení stránky sama nemenila. |
| 4 | V **tabuľke** skontroluj a prípadne oprav riadky (názov sektora, percentá). Parser berie **posledných päť čísel** v riadku ako **1d, 5d, 1m, 3m, 1y** (ak je v riadku aj váha, zvyčajne „spadne“ mimo týchto piatich). |
| 5 | **Uložiť snímok (short)** alebo **(long)** — uloží aktuálne dáta z editora do databázy. Pre report sa používajú **vždy posledné uložené** snímky daného typu. |

### 3.2 Skontroluj ticker (diverzifikácia)

Po uložení **krátkodobého** snímku sa v časti reportu zobrazí pole **Ticker** a tlačidlo **Vyhodnotiť**. Ticker musí byť v **Symboly** so zvoleným sektorom. Vyhodnotenie použije **otvorené** nohy z denníka (váhy z |prémia|×100), posledný krátkodobý snímok (kosínusová podobnosť výkonnostných stĺpcov medzi riadkami tabuľky) a voliteľne dlhodobý snímok (1r). Nejde o investičnú radu — len orientáciu z týchto dát.

### 3.3 Report

Pod blokmi nahrávania:

- **Mapovanie portfólia → tabuľka** — ktoré sektory z denníka sa podarilo spárovať s riadkom v OCR tabuľke (presná alebo čiastočná zhoda názvu).
- **Varovania** — napr. vysoká váha v jednom sektore, veľa expozície v skupine **podobného krátkodobého správania**.
- **Klastre** — sektory, ktoré majú v krátkom snímku veľmi podobný vektor výkonov (kosínusová podobnosť).
- **Možní diverzifikátori** — sektory z tabuľky, ktoré sú **málo podobné** na tie, kde máš expozíciu (podľa mapovania).
- **Dlhodobý výkon** — ak máš uložený **dlhodobý** snímok a v ňom stĺpec **1r**, zobrazí sa niekoľko najsilnejších sektorov.

---

## 4. Kde to ešte uvidíš

Na stránke **Journal — Gréky** (záložka **Prehľad**) je expander **Sektory — insight (Barchart OCR)** — stručný výstup z posledných snímkov a odkaz späť na túto stránku.

---

## 5. Riešenie problémov

| Problém | Čo skús |
|--------|---------|
| **Krátky horizont** — OCR len zlomok tabuľky, dlhý snímok ide celý | Často ide o **veľmi široký a nízky** screenshot (veľa stĺpcov v jednom riadku). Aplikácia tabuľku **zväčší podľa výšky** a pri veľkej šírke robí OCR po **prúžkoch** zľava doprava. Ak stále chýbajú riadky: ostrejší PNG, väčší výrez len na tabuľku, alebo **vlož text ručne**. |
| Málo alebo žiadne riadky po OCR | Väčší výrez, ostrejší PNG; alebo vlož text ručne a uprav v editore. |
| Zlý názov sektora | Oprav v editore pred uložením; v **Symboly** zjednoť názov sektora s tabuľkou. |
| „Nespárované“ sektory | Doplň / uprav **sector** pri tickeroch; alebo uprav názov v uloženej tabuľke tak, aby sedel s textom z Barchart. |
| Report bez portfólia | Ak nemáš otvorené obchody, časť textov o váhach sa nevyplní — snímky a klastre z tabuľky predsa uvidíš. |

---

## 6. Údaje a súkromie

Všetko beží **lokálne** (OCR cez Tesseract na tvojom počítači). Snímky sa ukladajú ako **JSON riadkov** v tabuľke `sector_performance_snapshots` v `data/journal.db` — nie ako súbor obrázka.
