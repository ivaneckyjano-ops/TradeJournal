import csv
from odf.opendocument import load
from odf.table import Table, TableRow, TableCell
from odf.text import P
import sys

input_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_FINAL_v.00.ods"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_NUMERIC.csv"

def clean_to_float(val):
    if not val:
        return "0.0"
    # Odstránenie všetkých druhov medzier, čiarku zmeníme na bodku
    cleaned = val.replace(' ', '').replace(',', '.').replace('\xa0', '').replace('\u202f', '')
    try:
        # Len ak je to naozaj číslo
        f = float(cleaned)
        return str(f)
    except ValueError:
        return val # Ak to nie je číslo (napr. text), necháme pôvodný text

try:
    doc = load(input_file)
    # Prejdeme listy (vezmeme prvý)
    all_tables = doc.spreadsheet.getElementsByType(Table)
    if not all_tables:
        print("Nenašli sa žiadne listy v ODS.")
        sys.exit(1)
        
    table = all_tables[0]
    rows = table.getElementsByType(TableRow)
    
    with open(output_file, 'w', encoding='utf-8-sig', newline='') as f:
        # Použijeme bodkočiarku ako oddeľovač, ktorý je štandardom v LibreOffice/Excel
        writer = csv.writer(f, delimiter=';')
        
        for i, row in enumerate(rows):
            cells = row.getElementsByType(TableCell)
            row_data = []
            
            for j, cell in enumerate(cells):
                # Získame text z bunky
                p_elements = cell.getElementsByType(P)
                text_val = ""
                if p_elements:
                    text_val = "".join([str(node) for node in p_elements[0].childNodes if hasattr(node, 'data') or hasattr(node, 'text')])
                
                # Ak sme v stĺpcoch F, G, H (indexy 5, 6, 7) a nie sme v hlavičke
                if j in [5, 6, 7] and i > 0:
                    row_data.append(clean_to_float(text_val))
                else:
                    row_data.append(text_val)
            
            if any(row_data): # Zapíšeme len riadky, ktoré nie sú úplne prázdne
                writer.writerow(row_data)
                
    print(f"Hotovo! Súbor bol bezpečne uložený ako: {output_file}")

except Exception as e:
    print(f"Chyba pri konverzii: {e}")
    sys.exit(1)
