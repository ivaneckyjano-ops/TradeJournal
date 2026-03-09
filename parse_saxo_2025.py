import csv
import re
import sys

input_file = "/tmp/saxo_2025.txt"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/prehlad_Saxo2025.csv"

def parse_pdf():
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        csv_data = [["Sekcia", "Inštrument", "ID pozície/obchodu", "Dátum", "Valuta", "Popis", "Množstvo", "Cena", "Mena", "Príjem/Výdaj", "Zostatok"]]
        
        current_section = ""
        page_num = 0

        for line in lines:
            line_s = line.strip()
            if not line_s: continue
            
            # Detekcia strany a sekcie
            if "Strana" in line and "z 54" in line:
                page_match = re.search(r'Strana (\d+)', line)
                if page_match:
                    page_num = int(page_match.group(1))
                    if 3 <= page_num <= 4:
                        current_section = "Holdingy"
                    elif 5 <= page_num <= 11:
                        current_section = "Zatvorené pozície"
                    elif 12 <= page_num <= 52:
                        current_section = "Transakcie"
                    else:
                        current_section = ""
            
            if not (3 <= page_num <= 52): continue

            # Spracovanie Holdingov (Strana 3-4)
            if current_section == "Holdingy":
                # Inštrument Symbol ID pozície Mena nástroja Otvorený ...
                # Plug Power Jan2027 1.5 P PLUG/15F27 P1.5:xcbf 7128968754 USD 08-apr-2025 -1 ...
                match = re.search(r'(\d{10})\s+([A-Z]{3})\s+(\d{2}-[a-z]{3}-\d{4})\s+([\d-]+)', line_s)
                if match:
                    id_pos, mena, datum, qty = match.groups()
                    instr = line_s[:match.start()].strip()
                    csv_data.append(["Holdingy", instr, id_pos, datum, "", "Otvorená pozícia", qty, "", mena, "", ""])

            # Spracovanie Zatvorených pozícií (Strana 5-11)
            elif current_section == "Zatvorené pozície":
                # Inštrument Identifikácia zatvorenej pozície Zavrieť typ Otvorený dátum Dátum uzavretia Množstvo ...
                match = re.search(r'(\d{10})\s+(Buy|Sell|Expiry|Assign|Deliver)\s+(\d{2}-[a-z]{3}-\d{4})\s+(\d{2}-[a-z]{3}-\d{4})\s+([\d-]+)', line_s)
                if match:
                    id_pos, typ, d_open, d_close, qty = match.groups()
                    instr = line_s[:match.start()].strip()
                    csv_data.append(["Zatvorené pozície", instr, id_pos, d_open, d_close, typ, qty, "", "EUR", "", ""])

            # Spracovanie Transakcií (Strana 12-52)
            elif current_section == "Transakcie":
                # Dátum Dátum valuty ID obchodu Inštrument Popis Suma Mena Zostatok
                # 31-dec-2025 02-jan-2026 6251234567 ...
                match = re.search(r'^(\d{2}-[a-z]{3}-\d{4})\s+(\d{2}-[a-z]{3}-\d{4})\s+(\d{10})', line_s)
                if match:
                    d_trans, d_val, id_obchodu = match.groups()
                    rest = line_s[match.end():].strip()
                    # Sumy sú na konci, oddelené medzerou a s čiarkou
                    sum_parts = re.findall(r'[\d\s-]*[\d-]+,[\d]{2}', rest)
                    if sum_parts:
                        zostatok = sum_parts[-1].strip()
                        suma = sum_parts[-2].strip() if len(sum_parts) > 1 else "0,00"
                        popis = rest.replace(suma, "").replace(zostatok, "").strip()
                        csv_data.append(["Transakcie", "", id_obchodu, d_trans, d_val, popis, "", "", "EUR", suma, zostatok])

        # Zápis do CSV
        with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerows(csv_data)
        
        print(f"Súbor bol úspešne pretransformovaný do {output_file}")

    except Exception as e:
        print(f"Chyba: {e}")
        sys.exit(1)

parse_pdf()
