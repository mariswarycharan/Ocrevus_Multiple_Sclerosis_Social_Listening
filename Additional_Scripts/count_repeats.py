"""
count_repeats.py

Purpose
-------
For every post in the unique tab ("Data - Share of voice"), count how many
times that same post content was repeated in the duplicate tab
("Duplicate Data (N2) - Share of "), and write that count into a new column
on the unique tab.

Logic
-----
1. Read both tabs. The post text is in the "Message" column of each tab.
2. Normalise every Message (lowercase, remove URLs, remove the "RT @user:"
   prefix, collapse whitespace) so tiny differences don't matter.
3. Match every duplicate row to exactly ONE unique row:
     a) exact match on the normalised text  -> score 100
     b) otherwise fuzzy match (rapidfuzz) against all unique posts and take
        the best-scoring one, provided score >= SIMILARITY_THRESHOLD.
        (If two uniques score the same, the one with the same OriginalAuthor
        wins.)
   Because each duplicate is assigned to only one unique post, nothing is
   double counted, so:  sum(Repeated Count) == number of rows in duplicate tab.
4. Write the counts into a new column on the unique tab, keep ALL other tabs
   and formatting, and save to a NEW file (your input file is never changed).
5. Optional audit tab ("Repeat Match Audit") lists every duplicate row, which
   unique row it was matched to and the similarity score, so you can review
   the borderline ones.
6. Tally check printed at the end: sum of the new column vs duplicate rows.

How to use
----------
    pip install pandas openpyxl rapidfuzz
    (edit the paths below)
    python count_repeats.py
"""

import re
import sys
from copy import copy

import numpy as np
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font
from rapidfuzz import fuzz, process

# =========================================================================
# EDIT THESE SETTINGS
# =========================================================================
INPUT_FILE_PATH = r"backend-data.xlsx"   # workbook with both tabs
OUTPUT_FILE_PATH = "Twitter_analysis_with_repeat_count.xlsx"

UNIQUE_SHEET_NAME = "Data - Share of voice"
DUPLICATE_SHEET_NAME = "Duplicate Data (N2) - Share of "   # trailing space is OK/optional

MESSAGE_COLUMN = "message"           # header of the post text, in BOTH tabs
AUTHOR_COLUMN = "OriginalAuthor"     # only used as a tie-breaker; ignored if missing
NEW_COLUMN_NAME = "Repeated Count"   # column added to the unique tab
HEADER_ROW = 1                       # Excel row number holding the headers

SIMILARITY_THRESHOLD = 85            # 0-100. Higher = stricter. 85-90 is a good range.
SCORER = fuzz.ratio                  # alternative: fuzz.token_sort_ratio (ignores word order)

REMOVE_URLS = True                   # t.co links etc. are ignored when comparing
REMOVE_RT_PREFIX = True              # "RT @someone:" at the start is ignored
REMOVE_MENTIONS = False              # set True to ignore @handles anywhere in the text

ADD_AUDIT_SHEET = True               # adds the "Repeat Match Audit" tab
AUDIT_SHEET_NAME = "Repeat Match Audit"
# =========================================================================


def normalize(value) -> str:
    """Clean a Message for comparison only (original text is never changed)."""
    text = "" if value is None else str(value)
    text = text.lower()
    if REMOVE_RT_PREFIX:
        text = re.sub(r"^\s*rt\s+@\w+:?\s*", "", text)
    if REMOVE_URLS:
        text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    if REMOVE_MENTIONS:
        text = re.sub(r"@\w+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def find_sheet(sheet_names, wanted: str) -> str:
    """Find a tab by name, tolerating case / trailing-space / truncated names."""
    key = wanted.strip().lower()
    for n in sheet_names:                       # 1. exact
        if n == wanted:
            return n
    for n in sheet_names:                       # 2. same after trim + lowercase
        if n.strip().lower() == key:
            return n
    starts = [n for n in sheet_names if n.strip().lower().startswith(key)
              or key.startswith(n.strip().lower())]   # 3. prefix (Excel cuts names at 31 chars)
    if len(starts) == 1:
        return starts[0]
    raise ValueError(
        f"Could not uniquely find a tab for '{wanted}'.\nTabs in file: {sheet_names}"
    )


def read_tab(path: str, sheet: str) -> pd.DataFrame:
    df = pd.read_excel(path, sheet_name=sheet, header=HEADER_ROW - 1,
                       dtype=str, keep_default_na=False)
    df.columns = [str(c).strip() for c in df.columns]
    if MESSAGE_COLUMN not in df.columns:
        raise ValueError(f"Column '{MESSAGE_COLUMN}' not found in '{sheet}'.\n"
                         f"Columns found: {list(df.columns)}")
    return df


def match_duplicates(uniq: pd.DataFrame, dup: pd.DataFrame):
    """
    Assign each duplicate row to one unique row.
    Returns (counts per unique row, list of audit records).
    """
    u_text = [normalize(v) for v in uniq[MESSAGE_COLUMN]]
    d_text = [normalize(v) for v in dup[MESSAGE_COLUMN]]

    has_author = AUTHOR_COLUMN in uniq.columns and AUTHOR_COLUMN in dup.columns
    u_auth = uniq[AUTHOR_COLUMN].str.strip().str.lower().to_numpy() if has_author else None
    d_auth = dup[AUTHOR_COLUMN].str.strip().str.lower().to_numpy() if has_author else None

    counts = np.zeros(len(uniq), dtype=int)
    assigned = [None] * len(dup)      # (unique_idx, score, match_type)

    # ---- Pass 1: exact match on normalised text (fast) --------------------
    first_idx = {}
    for i, t in enumerate(u_text):
        if t:
            first_idx.setdefault(t, i)
    pending = []
    for j, t in enumerate(d_text):
        if t and t in first_idx:
            assigned[j] = (first_idx[t], 100.0, "Exact")
        elif t:
            pending.append(j)

    # ---- Pass 2: fuzzy match for the rest ---------------------------------
    if pending:
        scores = process.cdist([d_text[j] for j in pending], u_text,
                               scorer=SCORER, dtype=np.float32, workers=-1)
        ranking = scores.copy()
        if has_author:  # tiny bonus so equal scores resolve to the same author
            same = np.array([d_auth[j] for j in pending])[:, None] == u_auth[None, :]
            ranking = ranking + same * 0.01
        best = ranking.argmax(axis=1)
        for row, j in enumerate(pending):
            score = float(scores[row, best[row]])
            if score >= SIMILARITY_THRESHOLD:
                assigned[j] = (int(best[row]), score, "Fuzzy")
            else:
                assigned[j] = (int(best[row]), score, "Below threshold")  # not counted

    # ---- Build counts + audit records -------------------------------------
    audit = []
    for j, a in enumerate(assigned):
        if a is None:   # blank message
            audit.append((j, None, None, "Blank message"))
            continue
        i, score, kind = a
        if kind != "Below threshold":
            counts[i] += 1
        audit.append((j, i, score, kind))
    return counts, audit


def write_output(uniq, dup, counts, audit):
    wb = load_workbook(INPUT_FILE_PATH)
    uniq_title = find_sheet(wb.sheetnames, UNIQUE_SHEET_NAME)
    ws = wb[uniq_title]

    # Find existing column or the next free one after the last header.
    headers = {str(c.value).strip(): c.column for c in ws[HEADER_ROW] if c.value is not None}
    if NEW_COLUMN_NAME in headers:
        col = headers[NEW_COLUMN_NAME]
    else:
        col = max(headers.values()) + 1
        ref = ws.cell(row=HEADER_ROW, column=col - 1)
        new_hdr = ws.cell(row=HEADER_ROW, column=col, value=NEW_COLUMN_NAME)
        if ref.has_style:
            new_hdr.font, new_hdr.fill = copy(ref.font), copy(ref.fill)
            new_hdr.border, new_hdr.alignment = copy(ref.border), copy(ref.alignment)
        else:
            new_hdr.font = Font(bold=True)
    ws.column_dimensions[ws.cell(row=HEADER_ROW, column=col).column_letter].width = 16

    for i, c in enumerate(counts):
        ws.cell(row=HEADER_ROW + 1 + i, column=col, value=int(c))

    if ADD_AUDIT_SHEET:
        if AUDIT_SHEET_NAME in wb.sheetnames:
            del wb[AUDIT_SHEET_NAME]
        a = wb.create_sheet(AUDIT_SHEET_NAME)
        a.append(["Duplicate tab row", "Matched unique tab row", "Similarity",
                  "Match type", "Duplicate Message", "Matched Unique Message"])
        for j, i, score, kind in audit:
            a.append([
                HEADER_ROW + 1 + j,
                None if i is None else HEADER_ROW + 1 + i,
                None if score is None else round(score, 1),
                kind,
                dup[MESSAGE_COLUMN].iloc[j],
                None if i is None else uniq[MESSAGE_COLUMN].iloc[i],
            ])
        for cell in a[1]:
            cell.font = Font(bold=True)
        a.freeze_panes = "A2"
        for letter, w in zip("ABCDEF", (18, 22, 12, 16, 60, 60)):
            a.column_dimensions[letter].width = w

    wb.save(OUTPUT_FILE_PATH)


def main():
    print(f"Reading: {INPUT_FILE_PATH}")
    names = pd.ExcelFile(INPUT_FILE_PATH).sheet_names
    uniq_sheet = find_sheet(names, UNIQUE_SHEET_NAME)
    dup_sheet = find_sheet(names, DUPLICATE_SHEET_NAME)
    print(f"Unique tab    : '{uniq_sheet}'")
    print(f"Duplicate tab : '{dup_sheet}'")

    uniq = read_tab(INPUT_FILE_PATH, uniq_sheet)
    dup = read_tab(INPUT_FILE_PATH, dup_sheet)
    print(f"Unique rows: {len(uniq)}   Duplicate rows: {len(dup)}")

    counts, audit = match_duplicates(uniq, dup)
    write_output(uniq, dup, counts, audit)

    # ---- Tally check ------------------------------------------------------
    total_counted = int(counts.sum())
    kinds = pd.Series([k for _, _, _, k in audit]).value_counts()
    print("\nMatch summary:")
    for k, v in kinds.items():
        print(f"   {k:<16}: {v}")
    print(f"\nSum of '{NEW_COLUMN_NAME}' : {total_counted}")
    print(f"Rows in duplicate tab    : {len(dup)}")
    if total_counted == len(dup):
        print("CHECK PASSED: counts tally with the duplicate tab.")
    else:
        print(f"CHECK FAILED: {len(dup) - total_counted} duplicate row(s) were not matched "
              f"(see 'Below threshold' / 'Blank message' in the '{AUDIT_SHEET_NAME}' tab). "
              f"Lower SIMILARITY_THRESHOLD or review those rows.", file=sys.stderr)
    print(f"\nPosts that were repeated at least once: {int((counts > 0).sum())} of {len(uniq)}")
    print(f"Done. Output written to: {OUTPUT_FILE_PATH}")


if __name__ == "__main__":
    main()