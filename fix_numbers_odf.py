from odf.opendocument import load
from odf.table import Table, TableRow, TableCell
from odf.text import P
import sys

file_path = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_FINAL_v.00.ods"

def clean_value(val):
    if not val:
        return "0.0"
    # Odstranenie medzier, ciarok, atd.
    cleaned = val.replace(' ', '').replace(',', '.').replace('\xa0', '').replace('\u202f', '')
    try:
        # Skusime, ci je to cislo
        float(cleaned)
        return cleaned
    except ValueError:
        return None

try:
    doc = load(file_path)
    # Prejdeme vsetky tabulky (listy)
    for table in doc.spreadsheet.getElementsByType(Table):
        print(f"Spracovavam list: {table.getAttribute('name')}")
        rows = table.getElementsByType(TableRow)
        
        # Prvy riadok je casto hlavicka, preskocime ho (volitelne)
        # Ak chceme konvertovat vsetko okrem prveho riadku:
        for i, row in enumerate(rows):
            if i == 0: continue # Preskoc hlavicku
            
            cells = row.getElementsByType(TableCell)
            # F=5, G=6, H=7
            for col_idx in [5, 6, 7]:
                if col_idx < len(cells):
                    cell = cells[col_idx]
                    # Ziskame text z bunky
                    p_elements = cell.getElementsByType(P)
                    if p_elements:
                        text_val = "".join([str(p) for p in p_elements[0].childNodes])
                        numeric_val = clean_value(text_val)
                        
                        if numeric_val is not None:
                            # Nastavime bunku ako cislo
                            cell.setAttribute("valuetype", "float")
                            cell.setAttribute("value", numeric_val)
                            # Aktualizujeme text v bunke (volitelne, ale dobre pre vizualizaciu)
                            # p_elements[0].childNodes[0].data = numeric_val # Toto nemusi vzdy fungovat ak je tam viac nodes
                            
    doc.save(file_path)
    print("Subor bol uspesne aktualizovany cez odfpy.")
except Exception as e:
    print(f"Chyba: {e}")
    sys.exit(1)
