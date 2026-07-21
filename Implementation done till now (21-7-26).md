# How We Built and Tested the Database — Schema, Data Loading, and the 3-Customer Verification


## 1. 

This project builds a pretend bank's database: customers, accounts, cards, loans, payments, and mutual fund investments. Everything about the customers is made up (safe to share, no real people). The one exception is mutual fund data — the list of funds and their daily prices are pulled from a real, public source (`api.mfapi.in`), so a made-up customer buys a real fund at its real price.

Building this happened in five stages, each one depending on the one before it:

1. **Build the empty database structure** (tables, columns, rules) — nothing in it yet.
2. **Load the real mutual fund data** from the internet.
3. **Build the "only legal doorway" for moving money** — a set of guarded functions that are the one and only way any balance is ever allowed to change.
4. **Generate the made-up customers and their transaction history**, run entirely through that doorway from step 3.
5. **Verify it** — check that every one of a small set of test customers really does have activity in every part of the bank (deposits, cards, loans, payments, mutual funds).

We currently have step 4 and 5 done at a small, deliberately tiny scale: **3 customers**, so that every single row could be checked by hand rather than trusted on faith. That's what "as of now with just three customers" refers to. The same code, unchanged, can generate the full-size dataset (tens of thousands of customers) later — it's a configuration switch, not new work.

---

## 2. Why it's built this way

A bank's data has one hard requirement that a normal spreadsheet doesn't: **money can never appear or vanish, and two things happening at the same instant can never both succeed if only one of them should.** For example, if an account has $100 and two withdrawal requests for $80 arrive in the same instant, only one is allowed to succeed — the database has to actually *stop* the second one, not just hope it doesn't happen.

Because of this, we didn't just build 17 tables and start inserting rows into them by hand. We built:
- A real transactional database (Postgres) that can "lock" a single row while it's being changed, so nothing else can touch it at the same moment.
- A small number of guarded functions that are the *only* way any code — including our own data generator — is allowed to change a balance.

This means the test dataset described below isn't clean because whoever wrote the data generator was careful. It's clean because the database itself refuses to accept anything that breaks a rule (a withdrawal bigger than the balance, a purchase over the credit limit, and so on).

---

## 3. Stage 1 — Building the empty database (the schema)

**File used:** `sql/01_oltp_core_schema.sql`

"Schema" just means the blueprint: table names, columns, and the rules attached to them — before any actual data goes in. T
This one file creates 14 tables inside a folder (in database terms, a "schema") called `core` — the live, real-time side of the system. It includes the customer list, the account list, deposit accounts and their transactions, cards and their transactions, payments, loans and their transactions, and mutual fund folios and their transactions.

Every rule is written directly into the table when it's created, for example:
- A deposit or withdrawal amount can never be zero or negative.
- A credit card's bill can never be allowed to exceed its credit limit.
- A loan's remaining balance can never go negative or exceed what was originally borrowed.
- If an account is marked "closed," it must have a close date recorded.

**How we know it worked:** we ran this file against a completely empty database, then deliberately tried a few things that should be rejected — like inserting a negative payment amount — and confirmed the database refused them, before wrapping the whole test in a "pretend this never happened" block so no test data was left behind.

A second, related file (`sql/02_lakehouse_delta_schema.sql`) describes the equivalent structure for the *analytics/reporting* side of the system (a separate "Lakehouse" platform used for dashboards and reporting rather than live transactions). We don't have access to a real Lakehouse environment in this practice project, so we didn't run that file — instead we simulated the same idea inside the same Postgres database using two more files, `sql/03_lakehouse_standin_schema.sql` and `sql/05_lakehouse_standin_gold_schema.sql`, which create the same kind of "folders" (`bronze`, `silver`, `gold`) for reporting-style tables like fund price history and monthly wealth summaries.

---

## 4. Stage 2 — Loading the one real thing: mutual fund data

**Files used:** everything inside `scripts/mf_loader/`

This is the only piece of real (non-invented) data in the whole project. It comes from `api.mfapi.in`, a genuine public website that publishes India's actual mutual fund prices.


| Step | File | What it does |
|---|---|---|
| 1 | `api_client.py` | Calls the real website and asks for the list of funds, then each fund's price history. Automatically retries if a call fails. |
| 2 | `bronze.py` | Saves whatever came back, completely unedited, into a "raw" table (`bronze.mf_api_raw`). This is our safety copy — if a later cleanup step turns out to be buggy, we still have the original untouched data to reprocess. |
| 3 | `silver.py` | Cleans the raw data up: turns price text into real numbers, date text into real dates, and — instead of quietly throwing away anything broken — sets it aside in a separate "quarantine" table with a note explaining why. The clean rows go into `silver.dim_mf_scheme` (the fund catalog) and `silver.fact_mf_nav` (the daily prices). |
| 4 | `sync_core.py` | Copies the clean prices one step further, into a small mirror table inside the live `core` side of the database (`core.mf_nav_ref`). This is needed because when a customer later "buys" a fund, the check that the price is real has to happen instantly, inside the same live transaction — it can't reach out to a separate reporting system to check. |
| 5 | `full_load.py` | The script you actually run once, top to bottom, to do the first full load — it's the one that calls steps 1–4 in order. |
| 6 | `daily_refresh.py` | The script meant to run every day going forward, to pick up new prices without re-downloading everything from scratch. |

**A checkpoint before moving on:** `sql/04_phase2_exit_gate_checks.sql` runs four read-only checks — no duplicate prices, no negative/zero prices, every fund has at least one price on file, and the live mirror roughly matches the reporting copy. Nothing is allowed to invent a fake fund purchase until these checks pass.

---

## 5. Stage 3 — Building the "only legal doorway" for moving money

**File used:** `sql/06_phase4_transaction_functions.sql`

This file creates five small programs that live inside the database itself, called `core.post_deposit_txn`, `core.post_card_txn`, `core.post_payment`, `core.post_loan_txn`, and `core.post_mf_transaction`. These are the **only** way any balance in the whole system is ever allowed to change — not our data generator, not a person running a command by hand, nothing else.

Every one of these five functions does the same four things, every single time, no exceptions:

1. **Check if this exact request already happened before.** If so, just return the same result again instead of doing it twice (this protects against something like a network hiccup causing the same request to be sent twice).
2. **Lock the one row about to be changed** (the account, the card, the loan, the fund folio) so nothing else can sneak in and change it at the exact same instant.
3. **Check the rules** — is there enough balance? Is the account actually open? Is the price/date valid? If anything fails, the attempt is refused and a note is written to a dedicated "rejected transactions" table (`txn_rejection_log`) explaining exactly why — it's never silently dropped.
4. **If everything passes, save the transaction and update the balance together, as one indivisible step** — either both happen, or if anything goes wrong, neither does.

---

## 6. Stage 4 — Generating the made-up customers and their history

**Files used:** everything inside `scripts/synth_gen/`, run via `scripts/synth_gen/main.py`

This is the actual data-filling step. It creates pretend customers and, for each one, years of realistic transaction history — but it does this by calling the five guarded functions from Stage 3 over and over, never by inserting rows directly. 

The generator is split into one file per topic, and `main.py` runs them in a strict order, because later steps genuinely need earlier ones to exist first (you can't generate a card transaction before the card exists, for instance):

| Order | File | What it creates |
|---|---|---|
| 1 | `spine.py` | The customers themselves, and their accounts (tagged as deposit / card / loan). Everything else depends on this existing first. |
| 2 | `deposits.py` | Savings/checking/term-deposit/overdraft accounts, then years of deposit and withdrawal history (via `post_deposit_txn`). |
| 3 | `cards.py` | Debit/credit/prepaid cards, then purchase/refund/ATM history (via `post_card_txn`). |
| 4 | `payments.py` | Outgoing bill payments and transfers, pulling money out of the deposit/card accounts created above (via `post_payment`). |
| 5 | `credit_bureau.py` | Pretend credit-score check history — runs *before* loans on purpose, so each loan can point back at a real, already-existing score. |
| 6 | `loans.py` | Loan accounts, then EMI (monthly repayment) and prepayment history (via `post_loan_txn`). |
| 7 | `loan_delinquency.py` | Works out, from the loan history above, how many days late each loan is, month by month. (This is an analysis step — it doesn't call one of the five money-moving functions, since it's not money moving.) |
| 8 | `mf_ownership.py` | Mutual fund "folios" (a customer's account with a fund company) and buy/sell/SIP history (via `post_mf_transaction`) — **but only using real fund names and real prices already loaded in Stage 2.** This step refuses to run at all if that real data isn't there. |
| 9 | `mf_holding_snapshot.py` | Works out, from the fund transactions above, what each customer owned and what it was worth at each month-end. |
| 10 | `wealth.py` | Runs last — adds each customer's deposit balances and fund holdings together into a monthly "total wealth" figure. |

A few supporting files back all of this: `config.py` (settings — how many customers, a fixed "random seed" so the same fake data can be produced again on a rerun), `db.py` (opens/closes the database connection), `fake.py` (generates realistic-*looking* but entirely invented ID numbers, card numbers, names), and `txncall.py` (the single point every module goes through to actually call one of the five Stage-3 functions).

**The "start over" button:** `sql/07_reset_phase3_data.sql` empties every table this stage fills in — without touching the table structure or the real mutual fund data from Stage 2 — so the whole thing can be regenerated cleanly. This was used repeatedly while bugs were being found and fixed during development.

### The 3-customer "micro" test

The generator has a special small-scale mode, turned on with `SYNTH_MICRO=1`. Instead of generating tens of thousands of customers, it generates a tiny, fixed number — **3** — and deliberately sizes everything else so that *every single one* of those 3 customers is guaranteed to have at least one account, one card, one loan, and one fund folio. This isn't left to chance (with only 3 customers, a purely random assignment could easily leave one of them with zero cards, for example) — the generator explicitly guarantees a full pass over every customer before filling in anything extra randomly.

This is what was actually run and checked, using this command:

```
SYNTH_MICRO=1 .venv/Scripts/python.exe -m scripts.synth_gen.main
```

It finished in about 1 second and produced:

| Table | Rows created |
|---|---|
| Customers | 3 |
| Accounts (all types) | 15 |
| Deposit accounts | 6 |
| Deposit transactions | 70 |
| Cards | 6 |
| Card transactions | 61 |
| Payments | 35 |
| Credit score checks | 8 |
| Loans | 3 |
| Loan transactions | 24 |
| Loan lateness records | 21 |
| Mutual fund folios | 3 |
| Mutual fund transactions | 77 |
| Current fund holdings | 6 |
| Monthly fund holding history | 26 |
| Monthly wealth snapshots | 9 |
| Rejected transactions | 0 (expected — at this tiny scale, the small deliberate chance of a rejection almost never fires; rejections were separately proven to work during Stage 3's own testing) |

---

## 7. Stage 5 — Proving each of the 3 customers is genuinely present everywhere

**File used:** `sql/08_verify_micro_dataset.sql`

This is a read-only script (it only reads data, never changes anything) with two parts:

1. It counts how many rows landed in every table (the table above).
2. It builds one row per customer, with one column for each domain of the bank — accounts, deposit transactions, cards, card transactions, payments, credit checks, loans, loan transactions, lateness records, fund folios, fund transactions, fund holding records, and wealth snapshots — so you can see at a glance whether every customer really has activity everywhere, with no gaps.

This is the actual result, read live from the database:

| Customer | Accounts | Deposit accts | Deposit txns | Cards | Card txns | Payments | Bureau pulls | Loans | Loan txns | Delinquency rows | MF folios | MF txns | MF holding rows | Wealth rows |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Indira Padmanabhan | 5 | 2 | 19 | 2 | 25 | 12 | 2 | 1 | 11 | 10 | 1 | 37 | 15 | 3 |
| Gaurangi Menon | 6 | 2 | 27 | 3 | 20 | 14 | 3 | 1 | 10 | 9 | 1 | 33 | 6 | 3 |
| Frado Som | 4 | 2 | 24 | 1 | 16 | 9 | 3 | 1 | 3 | 2 | 1 | 7 | 5 | 3 |

**Every single cell is greater than zero, for all 3 customers, across all 14 columns.** That's the proof: it's not just true "on average across the group," it's true for each individual customer, checked one at a time.

A few concrete, real examples pulled from the live data, to make it tangible:
- **Indira Padmanabhan** has a savings account worth ₹307,366, a credit card with a ₹292,119 limit, an auto loan with ₹213,701 still owed, and a mutual fund SIP (recurring investment) installment of ₹9,915.
- **Gaurangi Menon** has a *closed* savings account that still correctly keeps its full transaction history, a mortgage, and a mutual fund folio she has since redeemed money out of.
- **Frado Som** has a term deposit, a prepaid card, a personal loan that was later written off, and a mutual fund "switch" (moving money from one fund to another).

These small, realistic details (a closed account still keeping history, a written-off loan, a fund switch) aren't accidents — they're evidence that the five guarded functions from Stage 3 are producing data that behaves the way a real bank's data actually behaves, not just data that looks plausible at a glance.

---

## 8. The whole thing, start to finish, in order

```
1. sql/01_oltp_core_schema.sql            → builds the empty "core" tables + rules
2. sql/03_lakehouse_standin_schema.sql    → builds the empty reporting-side tables for fund data
3. scripts/mf_loader/full_load.py         → pulls real fund data from api.mfapi.in, cleans it, loads it
4. sql/04_phase2_exit_gate_checks.sql     → confirms the real fund data loaded cleanly
5. sql/05_lakehouse_standin_gold_schema.sql → builds the remaining empty reporting-side tables
6. sql/06_phase4_transaction_functions.sql → builds the 5 guarded "only legal doorway" functions
7. SYNTH_MICRO=1 python -m scripts.synth_gen.main → generates 3 customers and their full history
8. sql/08_verify_micro_dataset.sql        → confirms every customer has activity in every domain
```

The same code used for this 3-customer test can generate a much larger dataset (tens of thousands of customers, ~3.6 million rows total) later — it's a settings change (`SYNTH_MICRO` turned off, a scale percentage set instead), not new code. Running the small version first, and checking it by hand, was a deliberate choice: it's far easier to catch a bug looking at 3 customers' worth of data than 50,000 customers' worth.
