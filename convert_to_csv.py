import pandas as pd
import sys

input_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_FINAL_v.00.ods"
output_file = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_NUMERIC.csv"

def to_numeric(val):
    if pd.isna(val) or val == '':
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        # Odstránenie všetkých druhov medzier a nahradenie čiarky bodkou
        cleaned = val.replace(' ', '').replace(',', '.').replace('\xa0', '').replace('\u202f', '')
        try:
            return float(cleaned)
        except ValueError:
            return val
    return val

try:
    # Načítanie ODS (skúsime prvý list)
    df = pd.read_excel(input_file, engine='odf')
    
    # Indexy stĺpcov F, G, H (5, 6, 7 pri indexovaní od 0)
    cols_to_fix = [5, 6, 7]
    
    for col_idx in cols_to_fix:
        if col_idx < df.shape[1]:
            col_name = df.columns[col_idx]
            print(f"Konvertujem stĺpec: {col_name}")
            df.iloc[:, col_idx] = df.iloc[:, col_idx].apply(to_numeric)
    
    # Uloženie do CSV s bodkočiarkou ako oddeľovačom (vhodné pre SK/CZ Excel/LibreOffice)
    df.to_csv(output_file, index=False, sep=';', encoding='utf-8-sig')
    print(f"Hotovo! Súbor bol uložený ako: {output_file}")

except Exception as e:
    print(f"Chyba pri konverzii: {e}")
    sys.exit(1)
