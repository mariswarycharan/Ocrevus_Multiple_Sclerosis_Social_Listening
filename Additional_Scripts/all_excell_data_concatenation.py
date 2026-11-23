"""
=============================================================================
 Concatenate ALL Excel files in a folder into ONE consolidated master Excel.
-----------------------------------------------------------------------------
 Rules followed:
   * Reads ONLY the FIRST sheet of every Excel file in the folder.
   * Every row is taken AS-IS. No renaming, no cleaning, no type conversion,
     no dropping of rows/columns, no re-ordering of the original columns.
   * Adds ONE extra column "post ID" as the FIRST column -> 1, 2, 3, 4, ...
   * Saves a single consolidated .xlsx file.
   * No argparse - everything is controlled by global variables below.
=============================================================================
"""

import os
import pandas as pd

# ============================ GLOBAL SETTINGS ==============================
INPUT_FOLDER      = r"Sprinklr_Raw_Data_Month_Wise\Unzipped folder data"                        # folder having only Excel files
OUTPUT_FILE       = r"Sprinklr_Raw_Data_Month_Wise\Consolidated_Master_Data.xlsx"  # final consolidated file

ID_COLUMN_NAME    = "Post ID"        # serial-number column added as first column
SHEET_TO_READ     = 0                # 0 = first sheet of every workbook
OUTPUT_SHEET_NAME = "Master Data"    # sheet name in the output file
EXCEL_EXTENSIONS  = (".xlsx", ".xlsm", ".xls")
EXCEL_ROW_LIMIT   = 104857600          # max rows allowed in one Excel sheet
# ===========================================================================


def get_excel_files(folder_path):
    """Return a sorted list of all Excel files present in the given folder."""
    if not os.path.isdir(folder_path):
        raise NotADirectoryError(f"Input folder not found: {folder_path}")

    output_abs = os.path.abspath(OUTPUT_FILE)
    files = []

    for file_name in sorted(os.listdir(folder_path)):
        full_path = os.path.join(folder_path, file_name)

        if not os.path.isfile(full_path):                 # skip sub-folders
            continue
        if file_name.startswith("~$"):                    # skip Excel temp/lock files
            continue
        if not file_name.lower().endswith(EXCEL_EXTENSIONS):
            continue
        if os.path.abspath(full_path) == output_abs:      # skip our own output file
            continue

        files.append(full_path)

    return files


def read_first_sheet(file_path):
    """
    Read the first sheet exactly as it is.
      dtype=object          -> no automatic type conversion of any column
      keep_default_na=False -> text like 'NA', 'N/A', 'None', 'null' stays as text
                               (blank cells stay blank, they are not lost)
      header=0              -> first row of the sheet is kept as the header
    """
    return pd.read_excel(
        file_path,
        sheet_name=SHEET_TO_READ,
        header=0,
        dtype=object,
        keep_default_na=False,
        na_values=[],
    )


def consolidate(files):
    """Read every file's first sheet and stack all rows one below the other."""
    frames = []
    master_columns = []   # column order = order in which they first appear
    total_rows = 0

    for idx, file_path in enumerate(files, start=1):
        df = read_first_sheet(file_path)

        for col in df.columns:
            if col not in master_columns:
                master_columns.append(col)

        frames.append(df)
        total_rows += len(df)
        print(f"[{idx}/{len(files)}] {os.path.basename(file_path):<50} "
              f"rows: {len(df):>7} | cols: {len(df.columns)}")

    if not frames:
        raise ValueError("No data found to consolidate.")

    # Stack all rows; headers are matched by name, values untouched
    master = pd.concat(frames, ignore_index=True, sort=False)

    # Keep original column order (only if headers are unique, else leave as-is)
    if master.columns.is_unique:
        master = master.reindex(columns=master_columns)
    else:
        print("\n[WARN] Duplicate header names found in the data - "
              "keeping default concatenation column order.")

    if ID_COLUMN_NAME in master.columns:
        print(f"[WARN] A column named '{ID_COLUMN_NAME}' already exists in the source data. "
              f"The new serial-number column is still added as the first column.")

    # Add the serial number column as the FIRST column: 1, 2, 3, ...
    master.insert(0, ID_COLUMN_NAME, range(1, len(master) + 1))

    print(f"\nFiles consolidated : {len(files)}")
    print(f"Total data rows    : {total_rows}")
    print(f"Final shape        : {master.shape[0]} rows x {master.shape[1]} columns")

    return master


def save_output(master_df):
    """Save the consolidated dataframe to a single Excel file."""
    if len(master_df) + 1 > EXCEL_ROW_LIMIT:
        raise ValueError(
            f"Consolidated data has {len(master_df)} rows, which exceeds the Excel "
            f"sheet limit of {EXCEL_ROW_LIMIT - 1} data rows. Save as CSV instead."
        )

    out_dir = os.path.dirname(os.path.abspath(OUTPUT_FILE))
    os.makedirs(out_dir, exist_ok=True)

    master_df.to_excel(
        OUTPUT_FILE,
        sheet_name=OUTPUT_SHEET_NAME,
        index=False,
        engine="openpyxl",
    )
    print(f"\n[DONE] Consolidated file saved at:\n{OUTPUT_FILE}")


def main():
    print(f"Input folder : {INPUT_FOLDER}")
    files = get_excel_files(INPUT_FOLDER)

    if not files:
        print("No Excel files found in the input folder. Nothing to do.")
        return

    print(f"Excel files found : {len(files)}\n")
    master_df = consolidate(files)
    save_output(master_df)


if __name__ == "__main__":
    main()