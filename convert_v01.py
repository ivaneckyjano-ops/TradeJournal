import csv
from odf.opendocument import load
from odf.table import Table, TableRow, TableCell
from odf.text import P
import sys

input_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_FINAL_v.00.ods"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_v.01.csv"

def to_comma_number(val):
    if not val:
        return "0,00"
    # Odstránenie všetkých druhov medzier
    cleaned = val.replace(' ', '').replace('\xa0', '').replace('\u202f', '')
    # Ak tam bola bodka, nahradíme ju čiarkou (len ak je to číslo)
    try:
        # Najprv skúsime, či je to platné číslo (aj s bodkou aj s čiarkou)
        f_val = float(cleaned.replace(',', '.'))
        # Vrátime s čiarkou a na dve desatinné miesta pre lepšiu prehľadnosť
        return f"{f_val:.2f}".replace('.', ',')
    except ValueError:
        return val # Ak to nie je číslo, necháme pôvodný text

try:
    doc = load(input_file)
    all_tables = doc.spreadsheet.getElementsByType(Table)
    table = all_tables[0]
    rows = table.getElementsByType(TableRow)
    
    with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f, delimiter=';')
        
        header_written = False
        for i, row in enumerate(rows):
            cells = row.getElementsByType(TableCell)
            row_data = []
            
            for cell in cells:
                p_elements = cell.getElementsByType(P)
                text_val = ""
                if p_elements:
                    text_val = "".join([str(node) for node in p_elements[0].childNodes if hasattr(node, 'data') or hasattr(node, 'text')])
                row_data.append(text_val.strip())
            
            # Kontrola riadkov
            if not any(row_data): continue
            
            # Prvý riadok je hlavná hlavička
            if not header_written:
                writer.writerow(row_data)
                header_written = True
                continue
            
            # Ak je to ten druhý riadok s duplicitnou hlavičkou (obsahuje Príjem, Výdaj atď.), preskočíme ho
            if "Príjem(P)" in row_data or ("Výdaj(V)" in row_data and i < 5):
                print(f"Preskakujem riadok {i+1} (duplicitná hlavička)")
                continue

            # Spracovanie dátových riadkov
            # Číselné stĺpce: F(5), G(6), H(7), I(8), J(9), K(10), L(11)
            processed_row = []
            for j, val in enumerate(row_data):
                if j in [5, 6, 7, 8, 9, 10, 11] and i > 0:
                    processed_row.append(to_comma_number(val))
                else:
                    processed_row.append(val)
            
            writer.writerow(processed_row)
                
    print(f"Hotovo! Nová verzia bola uložená: {output_file}")

except Exception as e:
    print(f"Chyba: {e}")
    sys.exit(1)
