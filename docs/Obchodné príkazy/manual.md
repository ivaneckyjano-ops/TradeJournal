# Obchodné príkazy — manuál

Stránka **Obchodné príkazy** ukladá do aktuálnej DB podľa režimu **LIVE/PAPER** plán príkazu (čo, koľko, aký kontrakt, podmienky). Voliteľne vieš jeden uložený príkaz **odoslať do TWS** (`transmit=True`), ak máš pripojené Interactive Brokers a vyplnený kontrakt na zatvorenie.

Tento dokument popisuje **stavy**, **spúšťanie po uplatnení short nohy**, väzbu na denník a kontrolu voči IB.

---

## 1. Na čo slúži záložka

- **Plán v texte a číslach** — názov, ticker, smer (nákup/predaj), typ príkazu, množstvo, limity, poznámky.
- **Kontrakt na zatvorenie (IBKR)** — či ide o akciu (STK) alebo opciu (OPT), expirácia, strike, Call/Put — aby šiel príkaz kvalifikovať a odoslať.
- **Podmienky (iba zápis)** — napr. cena podkladu, či má predchádzať iný obchod; aplikácia ich sama nevyhodnocuje, sú pre teba ako checklist.
- **Voliteľné odoslanie do TWS** — po uložení a splnení podmienok nižšie.

---

## 2. Stav príkazu — čo znamená ktorý

| Stav (v aplikácii) | Technický kód | Kedy ho použiť |
|-------------------|---------------|----------------|
| **Koncept** | `draft` | Ešte nie je hotové: dopĺňaš limity, kontrakt, text, nechceš to omylom považovať za finálne. |
| **Pripravené** | `ready` | Plán je kompletný; **odporúčané**, keď čakáš na uplatnenie / správny moment a potom len overíš a odošleš. |
| **Odoslané / v platforme** | `submitted` | Príkaz bol odoslaný do TWS (alebo manuálne zaznamenaný v TWS poliach). |
| **Vykonané** | `filled` | Obchod prebehol (podľa tvojho zápisu v journali / reality). |
| **Zrušené** | `cancelled` | Plán neplatíš, úmyselne rušíš. |

**Čakáš až po uplatnení short nohy?**  
Samotné „čakanie“ **nie je** samostatný stav v databáze. Dáva zmysel:

- nechaj príkaz v **Koncepte**, kým dolaďuješ detaily;
- keď je všetko zadané a len čakáš na **assignment** a následnú kontrolu, daj **Pripravené**.

**Odoslané** a **Vykonané** až keď v TWS naozaj odošleš / obchod prebehne (alebo to tak zaznamenáš).

---

## 3. Spúšťacia logika pre odoslanie

- **Manuálne — odoslanie po kontrole** — bežný režim; pri odoslaní do TWS potrebuješ potvrdenie rizika a napísať `ASSIGN` do poľa (bez ohľadu na túto voľbu), podľa aktuálnej obrazovky.
- **Po uplatnení / priradení short nohy** — znamená, že príkaz má zmysel **až po** uplatnení / priradení príslušnej short nohy. Pri odoslaní do TWS musíš **naviac** potvrdiť zaškrtávacie políčko o uplatnení a stále napísať `ASSIGN`.

Žiadna automatická detekcia assignmentu z IB v základnom toku — ty potvrdzuješ, že situácia sedí.

---

## 4. Väzba na obchod v denníku

- Výber ukazuje **aktuálne otvorené opčné nohy** z denníka, s **celým popisom** (stratégia, ticker, typ nohy, strike, expirácia, kontrakty, skupina, vstup, …).
- Ak má uložený príkaz väzbu na obchod, ktorý už **nie je Open** (napr. uzavretý), zobrazí sa **doplnkový riadok** so stavom, aby sa výber nerozbil.

---

## 5. Sledovaná short noha a kontrola voči IB

Pri situácii „short v účte blokuje uzavretie long nohy“:

1. V príkaze nastav **Sledovanú short nohu** — musí to byť riadok z denníka typu **Short** so stavom **Open**.
2. **Ulož** príkaz.
3. Pripoj **IB**, podľa potreby **Stiahni pozície z IB**.
4. V expanderi záznamu použi **Skontrolovať short voči IB** — aplikácia porovná journal s aktuálnym snímkom pozícií a zapíše **čas a text** poslednej kontroly.

Výsledok je orientačný: ak short **stále sedí** v snímku, často ešte nepôjde rozumný príkaz na zatvorenie long strany; ak short **v snímku nie je**, môže to znamenať uzavretie / assignment — **vždy over v TWS**.

---

## 6. Odoslanie príkazu do TWS (skrátený checklist)

1. Stav **Koncept** alebo **Pripravené**.
2. Vyplnený **typ kontraktu** na zatvorenie **STK** alebo **OPT** (pre OPT aj expirácia, strike, Call/Put).
3. Pripojené IB.
4. Vo formulári odoslania: súhlas s rizikom, pri logike **Po uplatnení short nohy** aj potvrdenie assignmentu, do poľa presný text **`ASSIGN`**.
5. Po úspechu vie aplikácia doplniť Perm ID / Order ID a poznámku.

### Typ príkazu vs TWS (market vs korekcia)

- **Trh** — zodpovedá čistému **market** orderu; pri odoslaní sa **nepoužijú** polia Limit / Stop.
- **Limit ($)** a **Stop ($)** sú **predvolene skryté** — zobrazíš ich cez checkbox alebo **automaticky sa rozbalia**, keď v „Typ príkazu“ vyberieš **MTL**, **Limit** alebo **Stop**.
- **Trhový limit (MTL)** — správanie blízke trhu (marketable limit v TWS); výplň vieš **mierne korigovať** cez pole **Limit ($)**.
- **Stop** — riadenie cez **spúšťaciu cenu** v poli **Stop ($)** (nie „druhý limit“ k MTL vo väčšine jednoduchých príkazov).

| Zápis v Journali | Čo sa pošle do IB API |
|------------------|------------------------|
| **Trh** | Market order |
| **Trhový limit (MTL)** | `orderType = MTL`, limitná cena = pole **Limit ($)** — marketable limit ako v nápovede TWS (časť ako trh, zvyšok ako limit podľa výplne). |
| **Limit** | Čistý limitný príkaz |
| **Stop** | Stop — po dosiahnutí spúšťacej ceny trhový výkon (predajný stop pod trhom / nákupný stop nad trhom podľa smeru), ako v dokumentácii IB |

**Stop** v Journali používa pole **Stop ($)**; **MTL** a **Limit** používajú pole **Limit ($)**.

Ak niečo nesedí (stav, kontrakt), aplikácia odoslanie nedovolí — najprv **Ulož zmeny** vo formulári úpravy záznamu.

---

## 7. Filter zoznamu

Zaškrtnutím **Len príkazy „Po uplatnení short nohy“ v stave Koncept / Pripravené** zúžiš zoznam na plány čakajúce na assignment (ešte nie odoslané).

---

## 8. Časté otázky

**Musím mať žurnal najprv riadok short nohy?**  
Na **Sledovanú short nohu** áno — výber je z denníka. Na samotný text príkazu môžeš rátať s údajmi z IB (načítanie pozície z IB do formulára).

**Čo ak mi IB nepripojí?**  
Môžeš plán vyplniť ručne a uložiť; odoslanie a kontrola short vs IB vyžadujú IB.

**Je „Pripravené“ povinné pri čakaní na assignment?**  
Nie — ide o odporúčanie pre prehľadnosť. Ak ti vyhovuje nechať všetko v **Koncepte** do poslednej chvíle, môžeš.

---

*Posledná aktualizácia textu: súlad so správaním stránky Obchodné príkazy v TradeJournal.*
