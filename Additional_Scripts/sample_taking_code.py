import pandas as pd
import math

# ==========================================
# GLOBAL VARIABLES
# ==========================================
# Total number of sample rows you want to extract
TOTAL_SAMPLE_SIZE = 5000 

# File path and Column details
INPUT_FILE = r"Output\Entire data output\Unique_twiter_data_only_for_MS_classified.xlsx"
SENTIMENT_COLUMN = 'Entity Bucket'

# Entity Bucket
# Ocrevus Only
# Ocrevus vs. Competitor
# Competitor Only
# General MS (No Drug)
# Noise / Others


# Define the percentage distribution (Must add up to 1.0 / 100%)
SAMPLING_PERCENTAGES = {
    'Ocrevus Only': 0.20,          # 30%
    'Ocrevus vs. Competitor': 0.20, # 25%
    'Competitor Only': 0.20,       # 20%
    'General MS (No Drug)': 0.20,   # 15%
    'Noise / Others': 0.20         # 10%
}

# ==========================================
# FUNCTIONS
# ==========================================
def calculate_dynamic_sample_sizes(total_size, percentages):
    """
    Dynamically calculates the exact number of rows to sample for each class.
    Handles rounding errors to ensure the final sum exactly equals TOTAL_SAMPLE_SIZE.
    """
    sizes = {}
    remaining_size = total_size
    classes = list(percentages.keys())
    
    # Calculate sizes for all classes except the last one
    for cls in classes[:-1]:
        # Round to nearest integer to avoid fractional rows
        sizes[cls] = int(round(total_size * percentages[cls]))
        remaining_size -= sizes[cls]
    
    # Assign the remaining rows to the last class to guarantee exact total count
    sizes[classes[-1]] = remaining_size
    
    return sizes

def extract_stratified_sample(file_path):
    """
    Reads the huge dataset and extracts the stratified sample based on global variables.
    """
    print(f"Loading data from {file_path}... (This may take a moment for large files)")
    # Load the Excel dataset
    df = pd.read_excel(file_path)
    
    # 1. Dynamically calculate how many rows to take for each sentiment
    sample_sizes = calculate_dynamic_sample_sizes(TOTAL_SAMPLE_SIZE, SAMPLING_PERCENTAGES)
    print(f"\nTarget Sample Sizes Calculated: {sample_sizes}")
    
    sampled_frames = []
    
    # 2. Extract the exact number of rows for each sentiment category
    for sentiment, target_count in sample_sizes.items():
        # Filter the dataframe for the specific sentiment (e.g., 'POSITIVE')
        subset = df[df[SENTIMENT_COLUMN] == sentiment]
        
        available_count = len(subset)
        
        # Safety check: Ensure the dataset actually has enough rows for this category
        if available_count < target_count:
            print(f"Warning: Only {available_count} rows available for {sentiment}, "
                  f"but {target_count} requested. Taking all available.")
            actual_count = available_count
        else:
            actual_count = target_count
            
        # Sample the specific number of rows
        # random_state ensures reproducibility if you need to run it again
        sampled_subset = subset.sample(n=actual_count, random_state=42)
        sampled_frames.append(sampled_subset)
        
    # 3. Combine all sampled subsets into one final DataFrame
    final_sample = pd.concat(sampled_frames)
    
    # 4. Shuffle the final dataset so it isn't strictly grouped by sentiment
    final_sample = final_sample.sample(frac=1, random_state=42).reset_index(drop=True)
    
    return final_sample

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    # Run the sampling function
    sampled_data = extract_stratified_sample(INPUT_FILE)
    
    # Define output file name
    output_file = r'Output\Entire data output\Sample_data_2000_MS_classified.xlsx'
    
    # Save the sampled data to a new Excel file
    sampled_data.to_excel(output_file, index=False)
    
    print(f"\n✅ Success! Sampled {len(sampled_data)} rows and saved to '{output_file}'.")
    
    # Display the final distribution to verify the percentages
    print("\n--- Final Sample Distribution ---")
    print(sampled_data[SENTIMENT_COLUMN].value_counts())
    print("---------------------------------")