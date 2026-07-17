import pandas as pd
import requests

# URL of the NAV file
url = "https://portal.amfiindia.com/spages/NAVAll.txt"

# Download the file
response = requests.get(url)
response.raise_for_status()

lines = response.text.splitlines()

records = []

current_category = None
current_amc = None
header_found = False

for line in lines:
    line = line.strip()

    # Skip blank lines
    if not line:
        continue

    # Skip until header is found
    if not header_found:
        if line.startswith("Scheme Code;"):
            header_found = True
        continue

    # Data rows always contain exactly 6 semicolon-separated fields
    parts = line.split(";")

    if len(parts) == 6:
        records.append({
            "Category": current_category,
            "AMC": current_amc,
            "Scheme Code": parts[0],
            "ISIN Div Payout / Growth": parts[1],
            "ISIN Div Reinvestment": parts[2],
            "Scheme Name": parts[3],
            "NAV": parts[4],
            "Date": parts[5]
        })
    else:
        # Non-data lines

        # Scheme category
        if ("Schemes" in line or "Scheme" in line):
            current_category = line

        # AMC name
        else:
            current_amc = line

# Create DataFrame
df = pd.DataFrame(records)

# Convert datatypes
df["Scheme Code"] = pd.to_numeric(df["Scheme Code"], errors="coerce")
df["NAV"] = pd.to_numeric(df["NAV"], errors="coerce")
df["Date"] = pd.to_datetime(df["Date"], format="%d-%b-%Y", errors="coerce")

# Save to CSV
df.to_csv("amfi_nav.csv", index=False)

print(df.head())
print(f"\nTotal records: {len(df):,}")
print("Saved to amfi_nav.csv")