import csv, re

input_file = "/tmp/saxo_2025.txt"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/prehlad_Saxo2025.csv"

with open(input_file, 'r') as f:
    all_lines = f.readlines()

n = len(all_lines)
page_of = [0] * n
sec_of  = [None] * n
pg, sec = 0, None

for i, line in enumerate(all_lines):
    m = re.search(r'Strana (\d+) z 54', line)
    if m: pg = int(m.group(1))
    page_of[i] = pg
    if 'Holdingy - (802356INET)' in line:            sec = 'holdingy'
    elif 'Zatvorené pozície - (802356INET)' in line: sec = 'zatvorene'
    elif 'Transakcie - (802356INET)' in line:        sec = 'transakcie'
    sec_of[i] = sec

SKIP = ('Saxo Bank','Philip Heymans','info@saxo','Ján Ivanecký','Mena: EUR',
        'Účet (účty)','Vykazované','Vytvorený v:','Úvod','Táto ','Obchodovanie',
        'materskou','Inštrument','Identifikácia','zatvorenej pozície',
        'Zavrieť typ','zaúčtovaná suma','Realizovaný Z/S','Celkom',
        'Symbol','ID pozície','Mena nástroja','Konverzný kurz','Trhová hodnota',
        'Uplynutie','Otvorená cena','Súčasná cena','% zmeny','Zisk/strata',
        'Holdingy -','Zatvorené pozície -','Transakcie -','Strana ',
        'Salgovicka','Slovakia','Lubotice','Opcie na akcie','ID obchodu',
        'Produkt','Typ udalosti','Dátum obchodu','Dátum splatnosti','Otvorený dátum',
        'Dátum uzavretia','Správa o','01-jan-2025 až','01-jan-2025 - 31-dec-2025',
        'Open(otv','ose(zatv','Z/S','náklady','obchodu','splatnosti','K\n')

def is_code(t):
    if re.match(r'^[A-Z]{2,}/\d', t): return True
    if ':xcbf' in t or ':xcme' in t: return True
    if re.match(r'^[CP]\d+[.,]', t): return True
    return False

def clean_text(t):
    t = t.strip()
    if not t or any(k in t for k in SKIP): return ''
    if is_code(t): return ''
    if re.match(r'^\d{2}-\S{3}-\d{4}$', t): return ''
    if re.match(r'^-$', t): return ''
    return t

# FIX: regex čísel - zabraňuje spurióznym matchom (napr. "2027     1,50" → "20271,50")
# Čísla v SK formáte: voliteľný mínus, 1-3 číslice, voliteľne (medzera + 3 číslice), čiarka, číslice
NUM_RE = re.compile(r'-?\d{1,3}(?:\s\d{3})*,\d+')

def nums(text):
    return [re.sub(r'\s', '', x) for x in NUM_RE.findall(text)]

# ── Blok pre Zatvorené a Transakcie ──
# Zastav sa na prázdnom riadku ALEBO na inom dátovom riadku (dátum alebo 10-miestne ID + typ)
DATA_LINE_RE = re.compile(
    r'^\s{2}\d{2}-\S{3}-\d{4}\s'         # Transakcie: začína dátumom
    r'|^\s+\d{10}\s+(Buy|Sell|Expiry)'    # Zatvorené: začína ID + typom
)

def block_instr(ci, max_below=10):
    s = ci - 1
    while s >= 0:
        t = all_lines[s]
        if not t.strip(): break
        if DATA_LINE_RE.match(t): break
        s -= 1
    s += 1
    # Dole: max_below neprázdnych riadkov (zabraňuje zasahovaniu do susedného záznamu)
    below_lines = []
    e = ci + 1
    nonempty_count = 0
    while e < n and nonempty_count < max_below:
        t = all_lines[e]
        if not t.strip(): break
        if DATA_LINE_RE.match(t): break
        below_lines.append(e)
        nonempty_count += 1
        e += 1
    parts = []
    for j in list(range(s, ci)) + below_lines:
        t = clean_text(all_lines[j])
        if t: parts.append(t)
    return ' '.join(parts).strip()

# ── Špeciálna funkcia pre Holdingy (záznamy nie sú vždy oddelené prázdnymi riadkami) ──
def holdingy_instr(ci, raw_line, match_start):
    # Text pred ID na tom istom riadku
    before_id = raw_line[:match_start].strip()
    parts = [before_id] if before_id and clean_text(before_id) else []

    # Choď hore max. 4 riadky, zastav sa na prázdnom riadku alebo inom data riadku
    j = ci - 1
    above = []
    while j >= 0 and len(above) < 4:
        t = all_lines[j].strip()
        if not t: break                        # prázdny riadok = koniec bloku
        if re.search(r'\b\d{10}\b', t): break  # iný data riadok
        ct = clean_text(t)
        if ct: above.insert(0, ct)
        j -= 1
    parts = above + parts

    # Jeden riadok dole (napr. "1.5 P" alebo "Jan2027 3.5 C")
    if ci + 1 < n:
        t = all_lines[ci + 1].strip()
        ct = clean_text(t)
        if ct and not re.search(r'\b\d{10}\b', t):
            # Kontrola či to nie je zaèiatok ïalšieho záznamu (najbližší prázdny riadok je ïalej)
            if ci + 2 < n and all_lines[ci + 2].strip():
                pass  # ïalší riadok tiež nie je prázdny → môže patriť inému záznamu, preskočíme
            else:
                parts.append(ct)

    return ' '.join(parts).strip()

rows_h, rows_z, rows_t = [], [], []

for i, line in enumerate(all_lines):
    pg = page_of[i]
    if pg < 2 or pg > 52: continue
    sec = sec_of[i]
    raw = line.rstrip('\n')

    # ── HOLDINGY ──
    if sec == 'holdingy':
        m = re.search(r'\b(\d{10})\b\s+(USD|EUR)\s+(\d{2}-\S{3}-\d{4})\s+(-?\d+)', raw)
        if m:
            id_pos, mena, datum, qty = m.groups()
            ns = nums(raw[m.end():])
            instr = holdingy_instr(i, raw, m.start())
            rows_h.append([instr, id_pos, datum, qty, mena,
                ns[1] if len(ns)>1 else '',   # cena otvorenia
                ns[2] if len(ns)>2 else '',   # súčasná cena
                ns[4] if len(ns)>4 else '',   # zisk/strata
                ns[5] if len(ns)>5 else '',   # trhová hodnota
                ns[6] if len(ns)>6 else ''])  # strike

    # ── ZATVORENÉ POZÍCIE ──
    elif sec == 'zatvorene':
        m = re.search(r'\b(\d{10})\b\s+(Buy|Sell|Expiry|Assign|Deliver)\s+'
                      r'(\d{2}-\S{3}-\d{4})\s+(\d{2}-\S{3}-\d{4})\s+(-?\d+)', raw)
        if m:
            id_pos, typ, d_open, d_close, qty = m.groups()
            ns = nums(raw[m.end():])
            instr = block_instr(i)
            rows_z.append([instr, id_pos, d_open, d_close, typ, qty,
                ns[0] if len(ns)>0 else '',   # cena open
                ns[1] if len(ns)>1 else '',   # cena close
                ns[2] if len(ns)>2 else '',   # otvorená zaúčt. suma
                ns[3] if len(ns)>3 else '',   # zatvorená zaúčt. suma
                ns[4] if len(ns)>4 else ''])  # realizovaný Z/S

    # ── TRANSAKCIE ──
    elif sec == 'transakcie':
        m = re.match(r'\s{2}(\d{2}-\S{3}-\d{4})\s+(-|\d{2}-\S{3}-\d{4})\s+(-|\d{10})', raw)
        if m:
            d_trans, d_val, id_ob = m.groups()
            smer_m = re.search(r'\b(Kúpiť|Predať)\b', raw)
            smer   = smer_m.group(1) if smer_m else ''
            oc_m   = re.search(r'\b(OTVORIŤ|ZATVORIŤ|EXPIRÁCIA|UPLATNENIE|PRIRADENIE|DODANIE)\b', raw)
            oc     = oc_m.group(1) if oc_m else ''
            anchor = oc_m.end() if oc_m else (smer_m.end() if smer_m else m.end())
            qty_m  = re.search(r'\s+(-?\d+)\s', raw[anchor:])
            qty    = qty_m.group(1) if qty_m else ''
            ns     = nums(raw[anchor:])

            # Nástroj: pre Akcia je text na riadku medzi ID a menou (USD/EUR)
            instr = ''
            mena_m = re.search(r'\b(USD|EUR)\b', raw[m.end():])
            if mena_m:
                fragment = raw[m.end(): m.end()+mena_m.start()].strip()
                fragment = re.sub(r'\b(Akcia|Akciová|opcia)\b', '', fragment).strip()
                if fragment: instr = fragment
            blk = block_instr(i, max_below=2)  # max 2 riadky dole (opcia + typ)
            blk = re.sub(r'\b(Akciová|opcia)\b', '', blk).strip()
            instr = f'{instr} {blk}'.strip() if instr and blk else (instr or blk)

            # Špeciálne transakcie (dividendy, poplatky) – ID je '-'
            if id_ob == '-' and not instr:
                s = i-1
                while s >= 0 and all_lines[s].strip(): s -= 1
                e = i+1
                while e < n and all_lines[e].strip(): e += 1
                for j in range(s, e):
                    if j == i: continue
                    ct = clean_text(all_lines[j])
                    if ct: instr = ct; break

            rows_t.append([d_trans, d_val, id_ob, instr, smer, oc, qty,
                ns[0] if len(ns)>0 else '',   # cena
                ns[1] if len(ns)>1 else '',   # konverzný kurz
                ns[2] if len(ns)>2 else '',   # realizovaný Z/S
                ns[3] if len(ns)>3 else '',   # zaúčtovaná suma
                ns[4] if len(ns)>4 else '',   # zaúčtované náklady
                ns[5] if len(ns)>5 else ''])  # celkové náklady

with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
    writer = csv.writer(f, delimiter=';')

    writer.writerow(['# HOLDINGY - Otvorené pozície k 31.12.2025'])
    writer.writerow(['Inštrument','ID pozície','Dátum otvorenia','Množstvo','Mena',
                     'Cena otvorenia','Súčasná cena','Zisk/strata','Trhová hodnota','Strike'])
    writer.writerows(rows_h)
    writer.writerow([])

    writer.writerow(['# ZATVORENÉ POZÍCIE'])
    writer.writerow(['Inštrument','ID','Dátum otvorenia','Dátum zatvorenia','Typ',
                     'Množstvo','Cena Open','Cena Close',
                     'Otvorená zaúčt. suma','Zatvorená zaúčt. suma','Realizovaný Z/S'])
    writer.writerows(rows_z)
    writer.writerow([])

    writer.writerow(['# TRANSAKCIE'])
    writer.writerow(['Dátum','Valuta','ID obchodu','Inštrument','Smer','Open/Close',
                     'Množstvo','Cena','Konverzný kurz',
                     'Realizovaný Z/S','Zaúčtovaná suma','Zaúčtované náklady','Celkové náklady'])
    writer.writerows(rows_t)

print(f'Hotovo: Holdingy={len(rows_h)}, Zatvorené={len(rows_z)}, Transakcie={len(rows_t)}')
