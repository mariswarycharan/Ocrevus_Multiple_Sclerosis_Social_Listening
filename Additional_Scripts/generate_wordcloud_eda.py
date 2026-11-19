"""
generate_wordcloud_eda.py
--------------------------
Reads a column of text from an Excel file, cleans it, generates a Word Cloud
image, and produces a basic EDA (Exploratory Data Analysis) report + charts
for that column.

Library used for the word cloud: `wordcloud` (amueller/word_cloud) — it is
the most widely used, actively maintained, MIT-licensed word cloud library
for Python, with easy PIL/Matplotlib integration and full control over
stopwords, masks, colors, fonts, etc. That is why it is used here.

------------------------------------------------------------------------
HOW TO USE
------------------------------------------------------------------------
1. Install dependencies (one time):
       pip install pandas openpyxl wordcloud matplotlib

2. Edit the variables in the CONFIG section below (excel path, column
   name, output file names) OR run from the command line, e.g.:

       python generate_wordcloud_eda.py \
           --excel_file "data.xlsx" \
           --column "Review" \
           --output_image "wordcloud.png"

3. Run the script:
       python generate_wordcloud_eda.py

   Outputs produced (all in the same folder unless you give full paths):
       - <output_image>                 -> the word cloud (PNG image)
       - <output_image_base>_eda.txt    -> EDA text report
       - <output_image_base>_freq.png   -> top-word frequency bar chart
       - <output_image_base>_lengths.png-> word length distribution chart
------------------------------------------------------------------------
"""

import os
import re
import string
import argparse
from collections import Counter

import pandas as pd
import matplotlib
matplotlib.use("Agg")  # no GUI needed, just save files
import matplotlib.pyplot as plt
from wordcloud import WordCloud, STOPWORDS


# =========================================================================
# CONFIG — the three inputs you asked for. Edit these directly, or pass
# them as command line arguments (command line values override these).
# =========================================================================
EXCEL_FILE_PATH = 'Cleaned_twitter_data.xlsx'       # path to your Excel file (.xlsx)
COLUMN_NAME = "Message"         # column to build the word cloud from
OUTPUT_IMAGE_PATH = "wordcloud.png" # output word cloud image path


# =========================================================================
# STEP 1: Text cleaning
# =========================================================================

# A solid built-in general-purpose English stopword list (no nltk download
# needed). We also merge in wordcloud's own STOPWORDS set for extra
# coverage, and you can add domain-specific junk words to CUSTOM_STOPWORDS.
CUSTOM_STOPWORDS = {
    "im", "ive", "dont", "didnt", "doesnt", "cant", "wont", "isnt",
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on",
    "for", "with", "is", "are", "was", "were", "be", "been", "being",
    "this", "that", "these", "those", "it", "its", "as", "at", "by",
    "from", "so", "if", "then", "than", "too", "very", "just", "also",
    "not", "no", "yes", "will", "would", "could", "should", "can",
    "there", "here", "such", "we", "you", "your", "yours", "i", "he",
    "she", "they", "them", "his", "her", "their", "our", "us", "my",
    "me", "nan"
}
ALL_STOPWORDS = set(w.lower() for w in STOPWORDS) | CUSTOM_STOPWORDS


def clean_and_tokenize(text_series, min_word_len=3, extra_stopwords=None):
    """
    Takes a pandas Series of raw text, cleans it, and returns:
      - cleaned_text (single big string, useful for WordCloud)
      - tokens (list of individual cleaned words, useful for EDA)
    Cleaning steps:
      1. Drop missing values, force to string
      2. Lowercase
      3. Remove URLs, emails, HTML tags
      4. Remove punctuation and digits
      5. Collapse extra whitespace
      6. Tokenize on whitespace
      7. Drop stopwords and very short tokens
    """
    stopwords = ALL_STOPWORDS | set(extra_stopwords or [])

    raw_text = " ".join(text_series.dropna().astype(str).tolist())

    text = raw_text.lower()
    text = re.sub(r"http\S+|www\.\S+", " ", text)          # URLs
    text = re.sub(r"\S+@\S+", " ", text)                    # emails
    text = re.sub(r"<.*?>", " ", text)                       # HTML tags
    text = re.sub(r"[^a-z\s]", " ", text)                    # keep letters only
    text = re.sub(r"\s+", " ", text).strip()                 # collapse spaces

    tokens = [
        w for w in text.split()
        if len(w) >= min_word_len and w not in stopwords
    ]

    cleaned_text = " ".join(tokens)
    return cleaned_text, tokens


# =========================================================================
# STEP 2: Word cloud generation
# =========================================================================

def generate_wordcloud(cleaned_text, output_image_path,
                        width=1600, height=900, max_words=200,
                        background_color="white"):
    wc = WordCloud(
        width=width,
        height=height,
        max_words=max_words,
        background_color=background_color,
        collocations=False,   # avoid duplicate word-pairs
        stopwords=ALL_STOPWORDS,
    ).generate(cleaned_text)

    wc.to_file(output_image_path)
    print(f"[OK] Word cloud saved to: {output_image_path}")
    return wc


# =========================================================================
# STEP 3: EDA (Exploratory Data Analysis) on the target column
# =========================================================================

def run_eda(df, column_name, tokens, eda_report_path,
            freq_chart_path, length_chart_path, top_n=30):

    col = df[column_name]

    total_rows = len(df)
    non_null_rows = col.dropna().shape[0]
    missing_rows = total_rows - non_null_rows
    missing_pct = round((missing_rows / total_rows) * 100, 2) if total_rows else 0

    # entry-level (per-row) stats, computed on non-null rows only
    row_texts = col.dropna().astype(str)
    row_word_counts = row_texts.apply(lambda t: len(t.split()))
    row_char_counts = row_texts.apply(len)

    total_words_cleaned = len(tokens)
    unique_words_cleaned = len(set(tokens))
    avg_word_len = round(sum(len(w) for w in tokens) / total_words_cleaned, 2) if tokens else 0
    lexical_diversity = round(unique_words_cleaned / total_words_cleaned, 4) if total_words_cleaned else 0

    freq = Counter(tokens)
    top_words = freq.most_common(top_n)

    # ---- text report ----
    lines = []
    lines.append("=" * 70)
    lines.append(f"EDA REPORT — Column: '{column_name}'")
    lines.append("=" * 70)
    lines.append("")
    lines.append("1. BASIC COLUMN STATS")
    lines.append("-" * 70)
    lines.append(f"Total rows in file            : {total_rows}")
    lines.append(f"Non-null (filled) rows        : {non_null_rows}")
    lines.append(f"Missing/blank rows             : {missing_rows} ({missing_pct}%)")
    if non_null_rows:
        lines.append(f"Avg words per row (raw text)  : {round(row_word_counts.mean(), 2)}")
        lines.append(f"Max words in a single row      : {row_word_counts.max()}")
        lines.append(f"Min words in a single row      : {row_word_counts.min()}")
        lines.append(f"Avg characters per row         : {round(row_char_counts.mean(), 2)}")
    lines.append("")
    lines.append("2. STATS AFTER CLEANING (stopwords/punctuation/numbers removed)")
    lines.append("-" * 70)
    lines.append(f"Total cleaned words (tokens)   : {total_words_cleaned}")
    lines.append(f"Unique cleaned words           : {unique_words_cleaned}")
    lines.append(f"Average word length             : {avg_word_len} characters")
    lines.append(f"Lexical diversity (unique/total): {lexical_diversity}")
    lines.append("")
    lines.append(f"3. TOP {top_n} MOST FREQUENT WORDS")
    lines.append("-" * 70)
    for rank, (word, count) in enumerate(top_words, start=1):
        lines.append(f"{rank:>3}. {word:<20} {count}")
    lines.append("")
    lines.append("=" * 70)
    lines.append("End of report")
    lines.append("=" * 70)

    with open(eda_report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"[OK] EDA text report saved to: {eda_report_path}")

    # ---- chart 1: top word frequency bar chart ----
    if top_words:
        words, counts = zip(*top_words[:20])
        plt.figure(figsize=(10, 8))
        plt.barh(words[::-1], counts[::-1], color="steelblue")
        plt.xlabel("Frequency")
        plt.title(f"Top {min(20, len(top_words))} Most Frequent Words — '{column_name}'")
        plt.tight_layout()
        plt.savefig(freq_chart_path, dpi=150)
        plt.close()
        print(f"[OK] Frequency bar chart saved to: {freq_chart_path}")

    # ---- chart 2: word length distribution ----
    if tokens:
        word_lengths = [len(w) for w in tokens]
        plt.figure(figsize=(8, 5))
        plt.hist(word_lengths, bins=range(1, max(word_lengths) + 2), color="darkorange", edgecolor="black")
        plt.xlabel("Word length (characters)")
        plt.ylabel("Count")
        plt.title(f"Word Length Distribution — '{column_name}'")
        plt.tight_layout()
        plt.savefig(length_chart_path, dpi=150)
        plt.close()
        print(f"[OK] Word length distribution chart saved to: {length_chart_path}")

    return {
        "total_rows": total_rows,
        "non_null_rows": non_null_rows,
        "missing_rows": missing_rows,
        "total_words_cleaned": total_words_cleaned,
        "unique_words_cleaned": unique_words_cleaned,
        "top_words": top_words,
    }


# =========================================================================
# MAIN
# =========================================================================

def main():
    parser = argparse.ArgumentParser(description="Generate a word cloud + EDA report from an Excel column.")
    parser.add_argument("--excel_file", default=EXCEL_FILE_PATH, help="Path to the input Excel (.xlsx) file")
    parser.add_argument("--column", default=COLUMN_NAME, help="Name of the text column to analyze")
    parser.add_argument("--output_image", default=OUTPUT_IMAGE_PATH, help="Path to save the word cloud PNG")
    parser.add_argument("--sheet", default=1, help="Sheet name or index to read (default: first sheet)")
    parser.add_argument("--top_n", type=int, default=30, help="How many top words to list in the EDA report")
    args = parser.parse_args()

    excel_file_path = args.excel_file
    column_name = args.column
    output_image_path = args.output_image

    # derive companion output paths from the image path
    base, _ = os.path.splitext(output_image_path)
    eda_report_path = base + "_eda.txt"
    freq_chart_path = base + "_freq.png"
    length_chart_path = base + "_lengths.png"

    # ---- load the Excel file ----
    if not os.path.exists(excel_file_path):
        raise FileNotFoundError(f"Excel file not found: {excel_file_path}")

    print(f"[..] Reading Excel file: {excel_file_path}")
    df = pd.read_excel(excel_file_path, sheet_name=args.sheet)

    if column_name not in df.columns:
        raise ValueError(
            f"Column '{column_name}' not found. Available columns: {list(df.columns)}"
        )

    # ---- clean the target column ----
    print(f"[..] Cleaning column: '{column_name}'")
    cleaned_text, tokens = clean_and_tokenize(df[column_name])

    if not tokens:
        raise ValueError(
            f"No usable words found in column '{column_name}' after cleaning. "
            "Check that the column actually contains free-form text."
        )

    # ---- generate the word cloud (mandatory output) ----
    print("[..] Generating word cloud...")
    generate_wordcloud(cleaned_text, output_image_path)

    # ---- run EDA + charts ----
    print("[..] Running EDA...")
    run_eda(
        df, column_name, tokens,
        eda_report_path=eda_report_path,
        freq_chart_path=freq_chart_path,
        length_chart_path=length_chart_path,
        top_n=args.top_n,
    )

    print("\nAll done. Files created:")
    print(f"  - {output_image_path}   (word cloud image)")
    print(f"  - {eda_report_path}   (EDA text report)")
    print(f"  - {freq_chart_path}   (top-word frequency chart)")
    print(f"  - {length_chart_path}   (word length distribution chart)")


if __name__ == "__main__":
    main()