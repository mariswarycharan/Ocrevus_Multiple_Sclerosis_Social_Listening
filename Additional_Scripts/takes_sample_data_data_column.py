import os
import pandas as pd

# ==========================================
# GLOBAL VARIABLES
# ==========================================
TOTAL_SAMPLE_SIZE = 5000

INPUT_FILE = r"Output\Entire data output\Unique_twiter_data_only_for_MS_classified.xlsx"
OUTPUT_FILE = rf"Output\Entire data output\Sample_data_{TOTAL_SAMPLE_SIZE}_MS_classified.xlsx"

BUCKET_COLUMN = 'Entity Bucket'
DATE_COLUMN = 'CreatedTime'          # e.g. 2026-04-30 18:00:11
DATE_FORMAT = '%Y-%m-%d %H:%M:%S'    # year-month-day hour:minute:second

RANDOM_STATE = 42

# Small buckets: ALL their rows are always taken (no matter how few).
TAKE_ALL_BUCKETS = ['Ocrevus Only', 'Ocrevus vs. Competitor']

# Remaining rows (TOTAL - take-all rows) are split across these buckets by weight.
# Higher weight = bigger share. Only the ratio matters (4:3:2 = 44%:33%:22%).
# If a bucket does not have enough rows for its share, it gives everything it has
# and the shortfall is re-split among the other buckets by the same weights.
BUCKET_WEIGHTS = {
    'General MS (No Drug)': 4,
    'Competitor Only': 3,
    'Noise / Others': 1,
}


# ==========================================
# FUNCTIONS
# ==========================================
def parse_created_time(series):
    """
    Converts CreatedTime to datetime. Tries the exact format first, then flexible
    parsing for anything that did not match. Unparseable values become NaT.
    """
    parsed = pd.to_datetime(series, format=DATE_FORMAT, errors='coerce')
    failed = parsed.isna() & series.notna()
    if failed.any():
        parsed.loc[failed] = pd.to_datetime(series[failed], errors='coerce')
    return parsed


def allocate_across_buckets(total, take_all, weights, capacities):
    """
    Decides how many rows to take from each bucket so the total hits `total`
    (as long as the data has enough rows overall).

    1. take_all buckets: everything they have.
    2. The rest is split by weight; any bucket that cannot fill its share is capped
       at its capacity and the leftover is re-split among the remaining buckets.
    """
    alloc = {}
    remaining = total

    for b in take_all:
        alloc[b] = capacities.get(b, 0)
        remaining -= alloc[b]

    if remaining <= 0:
        print("Warning: take-all buckets alone already reach/exceed the total sample size.")
        return alloc

    active = [b for b in weights if capacities.get(b, 0) > 0]
    for b in weights:
        alloc.setdefault(b, 0)

    while remaining > 0 and active:
        wsum = sum(weights[b] for b in active)
        shares = {b: remaining * weights[b] / wsum for b in active}
        capped = [b for b in active if capacities[b] <= shares[b]]

        if not capped:
            # Nobody is limited: round shares to integers (largest remainder method)
            floors = {b: int(shares[b]) for b in active}
            leftover = remaining - sum(floors.values())
            order = sorted(active, key=lambda b: shares[b] - floors[b], reverse=True)
            for b in order[:leftover]:
                floors[b] += 1
            for b in active:
                alloc[b] = floors[b]
            remaining = 0
            break

        for b in capped:
            alloc[b] = capacities[b]
            remaining -= capacities[b]
        active = [b for b in active if b not in capped]

    if remaining > 0:
        print(f"Warning: the data does not have enough rows overall. Short by {remaining} rows.")
    return alloc


def allocate_equally(total, capacities):
    """
    Splits `total` as equally as possible across months. A month with fewer rows than
    its equal share gives everything it has; the shortfall is re-split equally over
    the months that still have spare rows.
    """
    alloc = {m: 0 for m in capacities}
    remaining = min(total, sum(capacities.values()))
    active = [m for m, cap in capacities.items() if cap > 0]

    while remaining > 0 and active:
        base, extra = divmod(remaining, len(active))
        for i, m in enumerate(active):
            share = base + (1 if i < extra else 0)
            give = min(share, capacities[m] - alloc[m])
            alloc[m] += give
            remaining -= give
        active = [m for m in active if alloc[m] < capacities[m]]

    return alloc


def sample_bucket_monthly(subset, target_count, bucket_name):
    """Samples `target_count` rows from one bucket, split equally across its months."""
    months = sorted(subset['_month'].unique())
    capacities = subset.groupby('_month').size().to_dict()
    alloc = allocate_equally(target_count, capacities)

    frames = []
    for m in months:
        n = alloc[m]
        if n > 0:
            frames.append(subset[subset['_month'] == m].sample(n=n, random_state=RANDOM_STATE))

    print(f"  {bucket_name} ({target_count}): " + ", ".join(f"{m}={alloc[m]}" for m in months))
    return pd.concat(frames) if frames else subset.iloc[0:0]


def extract_stratified_sample(file_path):
    print(f"Loading data from {file_path}... (This may take a moment for large files)")
    df = pd.read_excel(file_path)

    # --- Parse dates ---
    df['_parsed_date'] = parse_created_time(df[DATE_COLUMN])
    invalid = df['_parsed_date'].isna().sum()
    if invalid:
        print(f"Warning: {invalid} rows have a missing/invalid '{DATE_COLUMN}' and are excluded.")
        df = df[df['_parsed_date'].notna()].copy()

    df['_month'] = df['_parsed_date'].dt.to_period('M').astype(str)
    print(f"Months found in data: {sorted(df['_month'].unique())}")

    # --- Available rows per bucket (usable rows, after date cleaning) ---
    capacities = df[BUCKET_COLUMN].value_counts().to_dict()
    print(f"\nAvailable rows per bucket: {capacities}")

    # --- Decide how many rows per bucket ---
    sample_sizes = allocate_across_buckets(TOTAL_SAMPLE_SIZE, TAKE_ALL_BUCKETS, BUCKET_WEIGHTS, capacities)
    print(f"Target rows per bucket: {sample_sizes}  (sum = {sum(sample_sizes.values())})")

    # --- Monthly-balanced sampling inside each bucket ---
    print("\nMonthly allocation per bucket:")
    sampled_frames = []
    for bucket, target_count in sample_sizes.items():
        if target_count <= 0:
            continue
        subset = df[df[BUCKET_COLUMN] == bucket]
        sampled_frames.append(sample_bucket_monthly(subset, target_count, bucket))

    final_sample = pd.concat(sampled_frames)
    final_sample = final_sample.sample(frac=1, random_state=RANDOM_STATE).reset_index(drop=True)
    return final_sample


# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    sampled_data = extract_stratified_sample(INPUT_FILE)

    sampled_data = sampled_data.rename(columns={'_month': 'Year-Month'}).drop(columns=['_parsed_date'])

    out_dir = os.path.dirname(OUTPUT_FILE)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    sampled_data.to_excel(OUTPUT_FILE, index=False)

    print(f"\n✅ Success! Sampled {len(sampled_data)} rows and saved to '{OUTPUT_FILE}'.")

    print("\n--- Final Sample Distribution (per bucket) ---")
    print(sampled_data[BUCKET_COLUMN].value_counts())

    print("\n--- Bucket x Month Distribution ---")
    print(pd.crosstab(sampled_data[BUCKET_COLUMN], sampled_data['Year-Month'], margins=True))
    print("---------------------------------")