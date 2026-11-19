"""
find_duplicates.py

Purpose
-------
Detect duplicate posts in a dataset based on THREE columns:
    - OriginalAuthor
    - OriginalAuthorProfileLink
    - Message

Logic
-----
1. Before comparing, the key columns are CLEANED (see CLEANING STEPS below)
   so that things like extra spaces, different letter casing, or a
   trailing slash on a link don't cause two rows that are "really" the
   same to be treated as different.
2. Rows are grouped together only when ALL THREE cleaned key columns match:
   same OriginalAuthor, same OriginalAuthorProfileLink, AND same Message.
   (This fixes the earlier problem where two posts had the same author +
   profile link but were actually different content -- those are now
   correctly kept apart, because Message must also match.)
3. From each group, the FIRST occurrence is treated as the "unique"
   representative row -> goes to the "Unique" sheet.
4. Every other occurrence in that group (2nd, 3rd, ...) -> goes to the
   "Duplicate" sheet.
5. The output workbook has 3 sheets, and the ORIGINAL (uncleaned) values
   are what get written out -- cleaning is only used to decide grouping,
   it never changes your actual data:
       - "Raw Data"   : the original data, untouched
       - "Unique"     : one representative row per group
       - "Duplicate"  : all the extra rows from groups that repeated
6. Sanity check: len(Unique) + len(Duplicate) must equal len(Raw Data).

How to use
----------
Just edit the two paths below (INPUT_FILE_PATH, OUTPUT_FILE_PATH) and run:
    python find_duplicates.py
"""

import re
import sys
import pandas as pd


# =========================================================================
# EDIT THESE PATHS
# =========================================================================
INPUT_FILE_PATH = r"Sprinklr_MS_Raw_Data\QuickSearch (2)\Whatarethemostrecentposts_b8e0b3b6-bd01-47ac-8402-977a724376b1_1.xlsx"     # your source file, .csv or .xlsx/.xls/.xlsm
OUTPUT_FILE_PATH = "Cleaned_unique_twitter_data_MS.xlsx"   # the 3-tab result file this script creates
SHEET_NAME = 0                     # if INPUT_FILE_PATH is Excel with multiple
                                    # sheets, put the sheet name here (e.g.
                                    # "Sheet1"). Ignored for .csv. 0 = first sheet.
# =========================================================================


# The columns that define a duplicate group.
# Edit this list if your header names differ.
KEY_COLUMNS = ["OriginalAuthor", "OriginalAuthorProfileLink", "Message"]

# -------------------------------------------------------------------------
# CLEANING STEPS applied ONLY for comparison purposes (the original values
# in your output stay exactly as they were in the source file).
# -------------------------------------------------------------------------
STRIP_WHITESPACE = True          # remove leading/trailing spaces
LOWERCASE = True                 # make comparison case-insensitive
COLLAPSE_INTERNAL_SPACES = True  # multiple spaces/newlines/tabs -> one space
                                  # (handles multi-line Message text safely)

# Applied ONLY to link-type columns -- stripping a trailing "/" doesn't make
# sense for a Message column, so it's scoped to just this list.
URL_TRAILING_SLASH_COLUMNS = ["OriginalAuthorProfileLink"]


def load_data(input_path: str, sheet_name) -> pd.DataFrame:
    """Load the dataset from .csv or .xlsx/.xls/.xlsm into a DataFrame."""
    lower = input_path.lower()
    if lower.endswith(".csv"):
        df = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    elif lower.endswith((".xlsx", ".xls", ".xlsm")):
        df = pd.read_excel(
            input_path,
            sheet_name=sheet_name,
            dtype=str,
            keep_default_na=False,
        )
    else:
        raise ValueError(
            f"Unsupported file type: {input_path}. Use .csv or .xlsx/.xls/.xlsm"
        )
    return df


def validate_columns(df: pd.DataFrame) -> None:
    missing = [c for c in KEY_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(
            f"Missing required column(s): {missing}\n"
            f"Columns found in file: {list(df.columns)}"
        )


def clean_value(value: str, column_name: str) -> str:
    """Apply the configured cleaning steps to a single cell value."""
    text = "" if value is None else str(value)

    if STRIP_WHITESPACE:
        text = text.strip()

    if COLLAPSE_INTERNAL_SPACES:
        text = re.sub(r"\s+", " ", text)

    if column_name in URL_TRAILING_SLASH_COLUMNS:
        text = text.rstrip("/")

    if LOWERCASE:
        text = text.lower()

    return text


def split_unique_duplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Group rows by CLEANED KEY_COLUMNS (whitespace-trimmed, case-insensitive).
    First row of each group -> unique_df.
    Remaining rows of each group -> duplicate_df.
    Original (uncleaned) values are preserved in the output.
    """
    df = df.copy()

    # Build temporary cleaned columns purely for comparison.
    temp_key_cols = []
    for col in KEY_COLUMNS:
        temp_col = f"__clean__{col}"
        df[temp_col] = df[col].map(lambda v, c=col: clean_value(v, c))
        temp_key_cols.append(temp_col)

    # duplicated(..., keep="first") marks every row EXCEPT the first
    # occurrence of each cleaned (Author + ProfileLink + Message) combo.
    is_extra_occurrence = df.duplicated(subset=temp_key_cols, keep="first")

    unique_df = df.loc[~is_extra_occurrence].drop(columns=temp_key_cols).copy()
    duplicate_df = df.loc[is_extra_occurrence].drop(columns=temp_key_cols).copy()

    return unique_df, duplicate_df


def write_output(
    df_raw: pd.DataFrame,
    df_unique: pd.DataFrame,
    df_duplicate: pd.DataFrame,
    output_path: str,
) -> None:
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_raw.to_excel(writer, sheet_name="Raw Data", index=False)
        df_unique.to_excel(writer, sheet_name="Unique", index=False)
        df_duplicate.to_excel(writer, sheet_name="Duplicate", index=False)

    # Light formatting pass: bold headers, freeze top row, autosize columns.
    from openpyxl import load_workbook
    from openpyxl.styles import Font

    wb = load_workbook(output_path)
    for sheet_name in ["Raw Data", "Unique", "Duplicate"]:
        ws = wb[sheet_name]
        ws.freeze_panes = "A2"
        for cell in ws[1]:
            cell.font = Font(bold=True, name="Arial")
        for col_cells in ws.columns:
            length = max((len(str(c.value)) if c.value is not None else 0) for c in col_cells)
            ws.column_dimensions[col_cells[0].column_letter].width = min(max(length + 2, 10), 60)
    wb.save(output_path)


def main():
    print(f"Reading: {INPUT_FILE_PATH}")
    df_raw = load_data(INPUT_FILE_PATH, SHEET_NAME)
    validate_columns(df_raw)

    total_rows = len(df_raw)
    print(f"Total rows read: {total_rows}")

    df_unique, df_duplicate = split_unique_duplicate(df_raw)

    unique_count = len(df_unique)
    duplicate_count = len(df_duplicate)

    print(f"Unique rows (one per group):     {unique_count}")
    print(f"Duplicate rows (extra in group): {duplicate_count}")
    print(f"Sum (Unique + Duplicate):        {unique_count + duplicate_count}")

    # Mandatory sanity check requested by the user.
    if unique_count + duplicate_count != total_rows:
        print(
            "ERROR: Unique + Duplicate does NOT equal total row count. "
            "Something is wrong -- aborting before writing output.",
            file=sys.stderr,
        )
        sys.exit(1)
    else:
        print("Check passed: Unique + Duplicate == Total rows.")

    write_output(df_raw, df_unique, df_duplicate, OUTPUT_FILE_PATH)
    print(f"\nDone. Output written to: {OUTPUT_FILE_PATH}")
    print("Sheets: 'Raw Data', 'Unique', 'Duplicate'")


if __name__ == "__main__":
    main()