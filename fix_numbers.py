import pandas as pd
import sys
import shutil

file_path = "/home/narbon/Dokumenty/Archiv/SAXO/Saxo_Danove_Podklady_20242025_FINAL_v.00.ods"

def to_numeric(val):
    if pd.isna(val) or val == '':
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        cleaned = val.replace(' ', '').replace(',', '.').replace('\xa0', '').replace('\u202f', '')
        try:
            return float(cleaned)
        except ValueError:
            return val
    return val

try:
    shutil.copy2(file_path, file_path + ".bak")
    print(f"Zaloha vytvorena: {file_path}.bak")

    all_sheets = pd.read_excel(file_path, sheet_name=None, engine='odf')
    
    with pd.ExcelWriter(file_path, engine='odf') as writer:
        for sheet_name, df in all_sheets.items():
            print(f"Spracovavam list: {sheet_name}")
            cols_to_convert = [5, 6, 7]
            for col_idx in cols_to_convert:
                if col_idx < df.shape[1]:
                    df.iloc[:, col_idx] = df.iloc[:, col_idx].apply(to_numeric)
            df.to_excel(writer, sheet_name=sheet_name, index=False)
    print("Subor bol uspesne aktualizovany.")
except Exception as e:
    print(f"Chyba: {e}")
    sys.exit(1)
