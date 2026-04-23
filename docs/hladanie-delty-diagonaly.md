# Manuál: Hľadanie delty — diagonály

Táto stránka v TradeJournal hľadá **diagonálne spready** (dve rovnaké typy opcií — Call alebo Put — na **dvoch rôznych expiráciách**) podľa toho, ako blízko je **čistá delta** zvolenej **cieľovej delte**, a podľa zvoleného **triedenia** alebo **filtrov**. Všetko ide z **lokálnej databázy reťazcov** (`data/option_chains/*.db`), nie z živého Barchart API.

---

## 1. Čo musíš mať pripravené

1. **Import reťazcov** do DB Grékov (súbory `.db` pod `data/option_chains/`). Pre jeden ticker potrebuješ aspoň **dve rôzne expirácie** v tej istej snímke (as-of), inak nebudú žiadne výsledky.
2. V riadkoch reťazca musia byť vyplnené aspoň **delta** a **theta** pre daný typ opcie (Call/Put), ktorý zodpovedá zvolenej stratégii.
3. **Voliteľne — spot a OTM:** ak chceš filtrovať podľa **OTM short nohy** v pomere k podkladovému spotu, vyplň **Spot** (predvolená hodnota sa berie z tabuľky **Symbolov** v aplikácii). Pri spot = 0 sa OTM filter nepoužíva.

---

## 2. Pojmy: skoršia vs. neskoršia expirácia (v kalendári)

- **Skoršia expirácia** = **skorší** dátum expirácie (v páre ide o nohu s **menším** DTE, ak sú obe v budúcnosti).
- **Neskoršia expirácia** = **neskorší** dátum (väčší DTE).

Filtre DTE v Pokročilých sa viažú na tieto dve expirácie, **nie** priamo na pomenovanie stĺpca „Short“ v tabuľke: pri **long** diagonále je **Short** na skoršej expirácii, pri **short** diagonále je **Short** na neskoršej (pozri hint pod DTE v UI).

Stratégie (long/short call alebo put diagonál) majú v aplikácii popísané, ktorá noha je **short** a ktorá **long** — závisí to od znamienok váh pri skoršej a neskoršej expirácii v kalendári.

Gréky v reťazci sú za predpokladu **long 1 kontrakt**; **čistá delta / theta / vega / gamma** sú **vážený súčet** podľa stratégie (short noha má opačné znamienko ako long pri rovnakej „surovej“ delte z reťazca).

---

## 3. Základné ovládače (nad tlačidlom Hľadať)

| Pole | Význam |
|------|--------|
| **Ticker** | Symbol z dostupných `.db` reťazcov. |
| **Dátum snímky (as-of)** | Deň, ku ktorému sú uložené dáta reťazca; **DTE** v tabuľke sa počíta odtiaľ. |
| **Stratégia** | Typ diagonálu (long/short, call/put). |
| **Cieľová čistá delta** | **Cieľ** v jednotkách **reťazca** (0–1), nie horná medz dát. V **tabuľke výsledkov** je stĺpec **Čistá delta ×100** len na čítanie (napr. 0,05 → **5**). Triedenie a tolerancia používajú surovú deltu: `|čistá delta − cieľ|`. Orez ±2 okolo nuly: cieľ **0**, tolerancia **2**. |
| **Max. počet výsledkov** | Horný limit riadkov po zoradení a filtroch. |
| **Max. strike-ov na expiráciu** | Výkon — z každej expirácie sa berie len podvzorka strike-ov, aby neexplodoval počet kombinácií. |
| **Spot** | Podkladová cena pre výpočet **OTM short** (ak máš zapnutý príslušný filter a spot > 0). Predvolba z **Symbolov**; pri zmene tickeru sa widget resetuje vlastným kľúčom. |
| **Obmedziť rozsah strike** | Ak je zapnuté, do výpočtu vstupujú len kontrakty, kde **obidve** nohy majú strike v intervale **Strike od** … **Strike do**. |

---

## 4. Pokročilé filtre a režim triedenia

V expanderi **„Pokročilé filtre a režim triedenia“** sú filtre viazané na **checkbox**. **Predvolené čísla vo widgetoch** zodpovedajú **striktnému** skríningu (vhodné napr. pre likvidné akcie); tlačidlo **Širšie filtre (ETF / kratší reťazec)** nastaví naraz širšie pásma (často GLD). Pri **0 výsledkoch** po **Hľadať** uvidíš stručný návrh zjemnenia a v expanderi **Detail** diagnostiku z DB.

### Odporúčané predvolby (v aplikácii)

| Parameter | Predvolba | Poznámka |
|-----------|-----------|----------|
| Tolerancia delty | **2** okolo cieľa | S cieľovou deltou **0** ≈ pás ±2. |
| Theta min / max | **3** / **8** | Pri zapnutom **×100** sa porovnáva `čistá theta × 100` (väčšie čísla). |
| Čistá vega min / max | **0,10** / **0,20** | Jednotky **z importu na 1 akciu** — nie „Vega $“ z Spread Buildera (×100). |
| Čistá gamma min / max | **−0,03** / **0** | Z reťazca. |
| DTE skoršej exp. min / max | **40** / **55** | Dni do expirácie **skoršieho** dátumu v páre (od snímky). Stĺpec *Short — DTE* tým pásmom zodpovedá len pri **long** diagonáli. |
| DTE neskoršej exp. min / max | **90** / **140** | Dni do expirácie **neskoršieho** dátumu. Pri **short** call/put diagonáli je *Short — DTE* v tomto pásme. |
| Min. OTM short | **0,10** (10 %) | Pomer k **spotu**; bez spotu sa filter neaplikuje. |
| Max. \|debit\| / šírka strike | **0,25** (25 %) | Na akciu. |
| Max. rel. spread short / long | **0,08** / **0,05** | \((ask-bid)/|mid|\). |
| Min. open interest | **100** | Obidve nohy musia mať aspoň túto hodnotu. |

Pri **0 výsledkoch** rozšíri alebo vypni filtre — dáta z rôznych dní/tickerov nemusia spadať do tohto pásma.

### Triedenie

- **Klasické (delta → theta):** najprv najmenšia odchýlka `|čistá delta − cieľ|`, potom **vyššia** čistá theta (theta z reťazca; znamienko závisí od pozície — pozri texty pod tabuľkou v aplikácii).
- **Skóre (potom delta):** zoradenie podľa stĺpca **Skóre** (zostupne), pri rovnakom skóre podľa presnosti delty. Skóre je **heuristika** v kóde (nie trhové odporúčanie): zohľadňuje presnosť delty, čistú theta, čistú vega, veľkosť abs. gammy a pomer **|debit na akciu| / šírka strike** (debit na akciu = long ask − short bid z príslušných noh, ak sú ceny v DB).

### Filtre (stručne)

| Filter | Účel |
|--------|------|
| **Tolerancia delty** | Nechať len riadky, kde `|čistá delta − cieľ| ≤` zadaná odchýlka. |
| **Min. / max. čistá theta** | Spodný a horný prah; voliteľne **×100** (rovnaká škála pre oba prahy). |
| **Čistá vega / gamma (min/max)** | Obmedzenie rozsahu čistej vegy alebo gammy pozície. |
| **DTE skoršej / neskoršej (min/max)** | Predfilter párov expirácií podľa dní do expirácie od snímky (skorší vs. neskorší dátum v kalendári, nie vždy = Short/Long — pozri §2). |
| **Min. OTM short** | Vyžaduje **spot > 0**. Call: \((K_{short} - S) / S\); Put: \((S - K_{short}) / S\). |
| **Max. \|debit\| / šírka strike** | Pomer absolutnej hodnoty debetu na akciu a vzdialenosti strike-ov short vs. long. |
| **IV short ≥ IV long** | Obidve IV musia byť v dátach; voliteľná **marža** znamená podmienku `IV_short ≥ IV_long + marža`. |
| **Max. rel. spread** | Horný limit \((ask - bid) / |mid|\) na short alebo long nohe. Ak je relatívny spread **neznámy** (napr. chýba mid), riadok **filtrom nepadne** — neplatné hodnoty sa pri tomto filtri berú ako „prejdú“. |
| **Min. OI / volume** | Spodný prah pre **obidve** nohy naraz (open interest resp. volume z importu). |

---

## 5. Výstupná tabuľka

- **Short / Long** stĺpce: DTE, expirácia, strike, bid (short) / ask (long) podľa stratégie.
- **Čistá gamma** — z modelu reťazca (pôvodná škála).
- **Čistá delta**, **čistá theta** a **čistá vega** — v tabuľke **× 100** oproti surovej hodnote z DB (čitateľnejšie; filtre v Pokročilých stále používajú surovú škálu). Stĺpce sú označené príponou **×100**.
- **Debit/kredit ($/1 lot ×100)** — orientačný náklad pri otvorení **jedného** kontraktu: \((\text{long ask} - \text{short bid}) \times 100\). Ak v DB chýbajú bid/ask, môže byť prázdne.
- **Skóre** — vypočítaná heuristika je v tabuľke **vždy** (ak je v dátach stĺpec skóre); **primárne zoradenie** podľa skóre sa použije len pri voľbe triedenia **Skóre**. Pri **Klasickom** triedení riadky zoraďuje delta a theta, stĺpec Skóre môžeš použiť na vlastné porovnanie.

---

## 6. Uloženie a Spread Builder

1. Po vyhľadaní označ v stĺpci **Uložiť** riadky a ulož ich do lokálnej DB uložených diagonál (tlačidlo pod tabuľkou).
2. V sekcii **Uložené diagonály** môžeš záznamy zmazať (**Zmazať**) alebo **presne jeden** riadok poslať do **Spread Buildera** (**Do Buildera** + tlačidlo odoslania) — spot/IV sa berú ako pri CSV variantoch (Symboly + logika v kóde).

---

## 7. Obmedzenia a kvalita dát

- Hodnoty závisia od **snímky a importu**; mimo obchodných hodín môžu byť bid/ask/mid neúplné.
- **Žiadne živé API** — len to, čo je v lokálnych DB a v Symboloch (pre spot).
- Výsledok nie je obchodné odporúčanie; slúži na **prehľad a skríning** v rámci tvojich dát.

---

## Doplnok: vzorec skóre (technicky)

V kóde je skóre (zjednodušene) funkcia čistej delty `d`, čistej theta `t`, čistej vegy `v`, abs. čistej gammy `g`, pomeru `r = |debit_na_akciu| / max(|šírka_strike|, ε)`:

\[
\text{skóre} = \frac{30}{|d|+\varepsilon} + 10t + 2v - 100|g| - 50r
\]

kde \(\varepsilon\) je malé číslo proti deleniu nulou. Toto **nie je** finančná metrika z trhu; slúži len na zoradenie kandidátov v rámci aplikácie.

---

*Súbor manuálu v repozitári: `docs/hladanie-delty-diagonaly.md` — pri úpravách stránky ho prosím aktualizuj spolu s expanderom / textami v UI.*
