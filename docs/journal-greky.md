# Návod — Journal — Gréky a skupiny

Stránka slúži na **dlhodobý zápis** opčných Grékov a IV pri otvorených nohách (čo TWS sám o sebe nearchivuje), **skupiny**, **net súčty** a **časovú históriu** po uložení. Živé portfólio z brokera dopĺňa záložka **TWS (živé OPT)**.

---

## Predpoklady

- V **Trade Log** (alebo importom z IB na **Dashboarde**) máš obchody so stavom **Open**.
- Pre živý výpis z brokera: **Interactive Brokers** — pripojenie cez panel **IBKR** na stránke **Dashboard** v menu aplikácie (host, port, Client ID, tlačidlo *Pripojiť*).

---

## Čo kde nájdeš

| Záložka | Účel |
|--------|------|
| **TWS (živé OPT)** | Čítací prehľad opčných pozícií z IB (`OPT`). Ceny a P&L z portfólia; stĺpce Δ, Θ, Vega, IV sú **odhad z Black–Scholes** z aktuálnych cien IB (podkladový spot z prvého **STK** v portfóliu, ak je k dispozícii). Pri každom obnovení stránky sú dáta znova načítané — nie je to „posledná uložená obrazovka“. |
| **Zápis journal** | Tabuľka podľa **skupín**: upravíš skupinu nohy, vstupné / aktuálne **Δ, Θ (USD/deň), Vega, IV** a uložíš. |
| **Net podľa skupiny** | Jednoduché súčty hodnôt z denníka po skupinách (vstup vs aktuál, kde sú vyplnené). |
| **Časový vývoj (graf)** | Výber nohy → graf a tabuľka **uložených snímok** z journalu. |

---

## Filter tickeru

V expanderi **Filter** môžeš obmedziť zobrazenie na jeden ticker (zoznam z **Symboly**). Metriky, skupiny aj záložky potom pracujú len s týmito nohami.

---

## Pripojený IB a pozície v TWS

- Ak je **pripojený IB** a v účte sú aspoň nejaké **OPT**, stránka zobrazí **iba nohy z denníka**, ktoré **zhodujú** opčnú pozíciu v TWS (rovnaký kľúč ako kontrola na Dashboarde: ticker, strike, expirácia, typ opcie, Long/Short).
- Ak máš v denníku otvorené nohy, ale **žiadna** nezodpovedá TWS, zobrazí sa **informačná hláška** — dáta v DB sa nemažú; skontroluj formát expirácie, strike alebo synchronizáciu z Dashboardu.
- Ak **IB nie je** pripojený alebo v účte **nie sú OPT**, párovanie s TWS sa **nespúšťa**: vidíš **všetky** nohy so stavom *Open* z databázy (živý prehľad TWS v tomto režime nedáva zmysel).

---

## Zápis journal (krok za krokom)

1. Otvor záložku **Zápis journal**.
2. Pre každú skupinu uprav **Skupina** (výber musí sedieť so záložkou Skupiny / Trade Log).
3. Vyplň alebo uprav stĺpce **Θ / Δ / Vega / IV** (vstup a aktuál podľa popisu pod tabuľkou).
4. Stlač **Uložiť journal (Gréky, IV, Vega, skupina)**.
   - Zmeny sa zapíšu do tabuľky `trades`.
   - Ak sa zmenili aktuálne hodnoty (Δ, Θ, Vega, IV), pridá sa aj **bod do histórie** pre graf.

Stĺpce **TWS …** a **TWS kontr.** (ak sú viditeľné) sú **len na čítanie** — porovnanie s modelom z IB; do DB sa neuložia, kým ich neprepíšeš ručne do vlastných stĺpcov a neuložíš.

---

## Časový vývoj

- Body pribúdajú **len pri uložení journalu**, ak je aspoň jedna z aktuálnych hodnôt (Δ, Θ, Vega, IV) vyplnená a zmenená oproti predchádzajúcemu stavu v DB.
- Výber nohy v záložke **Časový vývoj** zobrazí graf a zoznam snímok.

---

## Údaje v databáze (stručne)

- **Vstup** (pri zadaní obchodu alebo v Trade Log): môžeš mať `iv_at_entry`, `delta_at_entry`, `theta_at_entry`; cez journal aj **Vega vstup**.
- **Priebežne:** ukladáš cez **Uložiť journal** → aktuálne polia a voliteľne nový riadok v histórii Grékov.
- **Uzavretie obchodu** (`Closed`): riadok ostáva, Gréky sa **automaticky nevymažú** — slúži to ako archív.
- **Úplné zmazanie** nohy z denníka: až keď obchod **vymažeš** v aplikácii (tam, kde sa volá mazanie záznamu); vtedy sa zmaže aj súvisiaca história snímok (cascade).

Podrobnejšie správanie pri zmiznutí pozície z TWS vs stav *Open* v DB je popísané v konverzácii / logike stránky: TWS pohľad nohu „schová“, DB ostáva, kým ju neuzavrieš alebo nevymažeš.

---

## Súvisiace stránky

- **Dashboard** — pripojenie IB, import pozícií a fills, kontrola **Denník ↔ TWS**.
- **Trade Log** — vstup a uzavretie obchodov, skupiny.
- **Sektory — insight** (voliteľný expander na tejto stránke) — váhy podľa sektorov, ak máš dáta z Barchart OCR.

---

## Tipy

- Ak Gréky v stĺpci TWS ostanú prázdne, v portfóliu často chýba **akcia (STK)** s rozumnou trhovou cenou — BS potrebuje podklad.
- **IV** v tabuľkách myslí ako desatinné číslo (napr. `0,35` = 35 %).
- Theta **Θ** v journali je v **USD za deň za celú nohu** (nie per share), v súlade so zvyškom aplikácie.
