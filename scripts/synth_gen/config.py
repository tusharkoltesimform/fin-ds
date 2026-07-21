"""Config loaded from environment, with .env (repo root) as a local fallback.

Same pattern as scripts/mf_loader/config.py. Random seed and the volume scale
factor are config, per Phase 0 ("Random seed for synthetic generation").
"""
import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(_REPO_ROOT / ".env")

PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_DATABASE = os.environ.get("PG_DATABASE", "forpocdb")
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")

# §Phase0: fixed seed so the "clean" synthetic dataset is reproducible.
RANDOM_SEED = int(os.environ.get("SYNTH_RANDOM_SEED", "20260720"))

# Scales every target row count in appendix.md's volume plan. 1.0 = full spec
# volume (~3.6M rows total across all phases). Use a small value for a smoke
# test before committing to the full run (same playbook as Phase 2's
# MF_SCHEME_LIMIT smoke test before the full load).
SCALE = float(os.environ.get("SYNTH_SCALE", "1.0"))

# MICRO overrides everything below: instead of scaling the appendix's volume
# plan down proportionally (which at very small N_CUSTOMERS can leave some
# customers with zero accounts of a given type, purely by chance), it targets
# a fixed, small customer count and sizes every other table so that coverage
# across account types is guaranteed by construction (see spine.assign_customers /
# mf_ownership.create_mf_folios) rather than left to random chance. Intended for
# "prove every module works end to end" demo datasets, not volume/perf testing.
MICRO = os.environ.get("SYNTH_MICRO", "0") == "1"

if MICRO:
    N_CUSTOMERS = int(os.environ.get("SYNTH_MICRO_CUSTOMERS", "3"))

    # 2x/2x/1x/1x customers so every customer gets >=1 of every account type
    # (assign_customers guarantees full coverage whenever count >= N_CUSTOMERS).
    N_DEPOSIT_ACCOUNTS = N_CUSTOMERS * 2
    N_CARD_ACCOUNTS = N_CUSTOMERS * 2
    N_LOAN_ACCOUNTS = N_CUSTOMERS * 1
    N_MF_FOLIOS = N_CUSTOMERS * 1

    N_DEPOSIT_TXN = N_DEPOSIT_ACCOUNTS * 10
    N_CARD_TXN = N_CARD_ACCOUNTS * 10
    N_PAYMENT_TXN = (N_DEPOSIT_ACCOUNTS + N_CARD_ACCOUNTS) * 3
    N_LOAN_TXN = N_LOAN_ACCOUNTS * 8
    N_MF_TXN = N_MF_FOLIOS * 3 * 6  # up to 3 schemes/folio, ~6 txns/scheme

    N_CREDIT_BUREAU_PULLS = N_CUSTOMERS * 3

    SCALE = round(N_CUSTOMERS / 50_000, 6)  # cosmetic, for the run log only
else:
    # Base target volumes, per appendix.md's volume plan (before SCALE is
    # applied). dim_account is NOT scaled independently -- it's the natural sum
    # of deposit_account + card_master + loan_account (each dim_account row is
    # created by its subtype generator), so its actual count will land near but
    # not exactly at the appendix's ~110,000 (a v1 volume-plan rounding, not a
    # hard target -- see implemented/phase3-synthetic-data-generation.md).
    N_CUSTOMERS = int(50_000 * SCALE)
    N_DEPOSIT_ACCOUNTS = int(70_000 * SCALE)
    N_CARD_ACCOUNTS = int(55_000 * SCALE)
    N_LOAN_ACCOUNTS = int(18_000 * SCALE)
    N_MF_FOLIOS = int(40_000 * SCALE)

    N_DEPOSIT_TXN = int(500_000 * SCALE)
    N_CARD_TXN = int(300_000 * SCALE)
    N_PAYMENT_TXN = int(200_000 * SCALE)
    N_LOAN_TXN = int(120_000 * SCALE)
    N_MF_TXN = int(350_000 * SCALE)

    N_CREDIT_BUREAU_PULLS = int(80_000 * SCALE)

# Batch commit size for the function-call loops (5 money-movement tables).
COMMIT_BATCH_SIZE = int(os.environ.get("SYNTH_COMMIT_BATCH", "500"))

# Observation window for the historical backfill: today back this many years.
HISTORY_YEARS = int(os.environ.get("SYNTH_HISTORY_YEARS", "4"))

# MF ownership uses a shorter, more recent window than the rest of the model
# (see mf_ownership.py) so the ~250,000-row mf_holding_snapshot monthly
# history target lands near the appendix's volume plan without generating a
# snapshot row per folio-scheme pair per month across the full 4-year window.
MF_HISTORY_MONTHS = int(os.environ.get("SYNTH_MF_HISTORY_MONTHS", "6"))

# customer_wealth_snapshot: one row per customer per month, for the last N
# calendar months. 50,000 customers * 3 months ~= 150,000, matching the
# appendix's target directly.
WEALTH_SNAPSHOT_MONTHS = int(os.environ.get("SYNTH_WEALTH_SNAPSHOT_MONTHS", "3"))

LOG_DIR = _REPO_ROOT / "logs"

INDIAN_CITIES = [
    ("Mumbai", "Maharashtra", "HDFC0000123"),
    ("Delhi", "Delhi", "ICIC0000456"),
    ("Bengaluru", "Karnataka", "SBIN0000789"),
    ("Chennai", "Tamil Nadu", "AXIS0000234"),
    ("Hyderabad", "Telangana", "PUNB0000567"),
    ("Pune", "Maharashtra", "HDFC0000890"),
    ("Kolkata", "West Bengal", "SBIN0001123"),
    ("Ahmedabad", "Gujarat", "ICIC0001456"),
    ("Jaipur", "Rajasthan", "AXIS0001789"),
    ("Lucknow", "Uttar Pradesh", "PUNB0002012"),
]
