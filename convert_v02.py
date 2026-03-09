import csv
from odf.opendocument import load
from odf.table import Table, TableRow, TableCell
from odf.text import P
import sys

input_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_FINAL_v.00.ods"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_v.02.csv"

def to_comma_number(val):
    if not val or val.strip() == "":
        return "0,00"
    # Odstránenie všetkých druhov medzier
    cleaned = val.replace(' ', '').replace('\xa0', '').replace('\u202f', '')
    try:
        # Najprv skúsime, či je to platné číslo (aj s bodkou aj s čiarkou)
        f_val = float(cleaned.replace(',', '.'))
        # Vrátime s čiarkou a na dve desatinné miesta
        return f"{f_val:.2f}".replace('.', ',')
    except ValueError:
        return val # Ak to nie je číslo (napr. symbol), necháme pôvodný text

try:
    doc = load(input_file)
    all_tables = doc.spreadsheet.getElementsByType(Table)
    table = all_tables[0]
    rows = table.getElementsByType(TableRow)
    
    with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
        # Dôležité: Separátor bodkočiarka pre SK LibreOffice
        writer = csv.writer(f, delimiter=';')
        
        header_written = False
        for i, row in enumerate(rows):
            cells = row.getElementsByType(TableCell)
            row_data = []
            
            # Musíme prejsť aspoň 12 stĺpcov (A až L), aj keď sú v riadku bunky prázdne
            for j in range(12): 
                if j < len(cells):
                    cell = cells[j]
                    p_elements = cell.getElementsByType(P)
                    text_val = ""
                    if p_elements:
                        text_val = "".join([str(node) for node in p_elements[0].childNodes if hasattr(node, 'data') or hasattr(node, 'text')])
                    row_data.append(text_val.strip())
                else:
                    row_data.append("")
            
            # Kontrola riadkov
            if not any(row_data): continue
            
            # Prvý riadok je hlavná hlavička
            if not header_written:
                writer.writerow(row_data)
                header_written = True
                continue
            
            # Ak je to ten druhý riadok s duplicitnou hlavičkou, preskočíme ho
            if "Príjem(P)" in row_data[4:12]: # Hľadáme to v okolí popisu
                continue

            # Číselné stĺpce: F(5) až L(11)
            processed_row = []
            for j, val in enumerate(row_data):
                if j in [5, 6, 7, 8, 9, 10, 11]:
                    processed_row.append(to_comma_number(val))
                else:
                    processed_row.append(val)
            
            writer.writerow(processed_row)
                
    print(f"Hotovo! Verzia v.02 bola uložená: {output_file}")

except Exception as e:
    print(f"Chyba: {e}")
    sys.exit(1)
