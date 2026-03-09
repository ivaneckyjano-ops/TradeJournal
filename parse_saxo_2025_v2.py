import csv
import re
import sys

input_file = "/tmp/saxo_2025.txt"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/prehlad_Saxo2025.csv"

def parse_pdf():
    try:
        with open(input_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        csv_data = [["Sekcia", "Inštrument", "ID pozície/obchodu", "Dátum", "Valuta", "Smer", "Množstvo", "Cena", "Mena", "Suma/Realizovaný Z/S", "Zostatok", "Popis"]]
        
        current_section = ""
        page_num = 0
        instr_buffer = ""

        for i, line in enumerate(lines):
            line_s = line.strip()
            if not line_s: 
                instr_buffer = "" # Reset buffer on empty line
                continue
            
            # Detekcia strany
            if "Strana" in line and "z 54" in line:
                page_match = re.search(r'Strana (\d+)', line)
                if page_match:
                    page_num = int(page_match.group(1))
                    if 3 <= page_num <= 4: current_section = "Holdingy"
                    elif 5 <= page_num <= 11: current_section = "Zatvorené pozície"
                    elif 12 <= page_num <= 52: current_section = "Transakcie"
                    else: current_section = ""
                continue
            
            if not (3 <= page_num <= 52): continue

            # --- HOLDINGY ---
            if current_section == "Holdingy":
                # Hľadáme riadok s ID (10 číslic)
                match = re.search(r'(\d{10})\s+([A-Z]{3})\s+(\d{2}-[a-z]{3}-\d{4})\s+([\d-]+)', line_s)
                if match:
                    id_pos, mena, datum, qty = match.groups()
                    # Inštrument je pravdepodobne v riadkoch nad týmto
                    instr = instr_buffer.strip()
                    csv_data.append(["Holdingy", instr, id_pos, datum, "", "", qty, "", mena, "", "", "Otvorená pozícia"])
                    instr_buffer = ""
                else:
                    # Ak to nie je ID riadok, ukladáme do buffera ako možný názov inštrumentu
                    if not any(keyword in line_s for keyword in ["Inštrument", "Symbol", "ID pozície"]):
                        instr_buffer += " " + line_s

            # --- ZATVORENÉ POZÍCIE ---
            elif current_section == "Zatvorené pozície":
                match = re.search(r'(\d{10})\s+(Buy|Sell|Expiry|Assign|Deliver)\s+(\d{2}-[a-z]{3}-\d{4})\s+(\d{2}-[a-z]{3}-\d{4})\s+([\d-]+)', line_s)
                if match:
                    id_pos, typ, d_open, d_close, qty = match.groups()
                    instr = instr_buffer.strip()
                    csv_data.append(["Zatvorené pozície", instr, id_pos, d_open, d_close, typ, qty, "", "EUR", "", "", ""])
                    instr_buffer = ""
                else:
                    if not any(keyword in line_s for keyword in ["Inštrument", "Identifikácia", "Zavrieť typ"]):
                        instr_buffer += " " + line_s

            # --- TRANSAKCIE ---
            elif current_section == "Transakcie":
                # Dátum Dátum valuty ID obchodu ...
                match = re.search(r'^(\d{2}-[a-z]{3}-\d{4})\s+(\d{2}-[a-z]{3}-\d{4})\s+(\d{10})', line_s)
                if match:
                    d_trans, d_val, id_obchodu = match.groups()
                    rest = line_s[match.end():].strip()
                    
                    # Detekcia smeru
                    smer = ""
                    if "Kúpiť" in rest: smer = "Kúpiť"
                    elif "Predať" in rest: smer = "Predať"
                    
                    # Detekcia množstva a ceny (napr. 100 4,500)
                    qty_price = re.findall(r'\s+([\d-]+)\s+([\d,]+)\s+[\d,]+', rest)
                    qty = qty_price[0][0] if qty_price else ""
                    price = qty_price[0][1] if qty_price else ""
                    
                    # Sumy na konci
                    sum_parts = re.findall(r'[\d\s-]*[\d-]+,[\d]{2}', rest)
                    if sum_parts:
                        zostatok = sum_parts[-1].strip()
                        suma = sum_parts[-2].strip() if len(sum_parts) > 1 else "0,00"
                        popis = rest
                        csv_data.append(["Transakcie", "", id_obchodu, d_trans, d_val, smer, qty, price, "EUR", suma, zostatok, popis])

        # Zápis do CSV
        with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerows(csv_data)
        
        print(f"Súbor bol úspešne pretransformovaný do {output_file}")

    except Exception as e:
        print(f"Chyba: {e}")
        sys.exit(1)

parse_pdf()
