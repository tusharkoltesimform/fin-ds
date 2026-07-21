# Banking Data Model POC — Final Decided Plan

*Story, full column-level data model, ACID constraints, and build plan — in one file for review.*

---

## 1. What this is

A 17-table banking data-model proof-of-concept covering six domains — shared spine, retail deposits, cards, lending, payments, mutual funds, and finance & risk — built medallion-style (bronze → silver → gold). It is a data-engineering practice project, not a production system, but the money-movement tables are built to a real ACID contract rather than being "demo-clean" by convention.

**The one thing that makes this non-trivial:** it mixes real and synthetic data. The mutual-fund catalog and price history are **real**, pulled live from `api.mfapi.in`. Everything else — customers, accounts, transactions, MF ownership — is **synthetic**, generated to look and behave like production data.

---

## 2. The Story

One customer (`dim_customer`) sits at the center. Customers open accounts (`dim_account`), each one of three kinds: deposit, card, or loan. Every money movement is a **fact** row — a deposit transaction, a card swipe, a loan repayment, a payment, a fund purchase. Periodically, **snapshot** tables take a photo of state: an account balance, a loan's lateness, a fund holding's value, a customer's total wealth.

Mutual funds are the odd one out: fund folios link straight to the customer, not through `dim_account` (a fund folio isn't a bank account), and the fund catalog + NAV history are real API data, not generated.

| | Mutual Funds | Everything else |
|---|---|---|
| The "things" (funds / customers) | Funds are **real** (`dim_mf_scheme`) | Customers, accounts are **synthetic** |
| The prices / history | NAV history is **real** (`fact_mf_nav`) | — |
| The transactions / ownership | **Synthetic** (generated customers buy real funds) | **Synthetic** |

A generated customer buys a real fund, at its real price, on a real date. That's the whole trick of the dataset.

---

## 3. Architecture Decision — Two-Platform Write Path

**Decision:** A Postgres OLTP database (`core` schema) is the system of record for every write to the 5 money-movement fact tables and the balance-bearing rows they touch. Fabric Lakehouse remains the analytical medallion layer (bronze/silver/gold), kept current via CDC/batch sync from Postgres.

**Why:** Delta Lake has no native row locking, `SELECT … FOR UPDATE`, or multi-table `SERIALIZABLE` transactions. "Two concurrent writes can never both overdraw the same account" cannot be honestly guaranteed on Spark/Delta alone — it needs a real transactional engine underneath.

**Isolation strategy:** pessimistic row locking (`FOR UPDATE` on the account/card/loan/folio row), not the engine's optimistic serializable-snapshot retry mode — simpler to reason about and demo for a POC. Compare-and-swap is the documented fallback if pessimistic locking isn't available.

**What stays Lakehouse-native** (no OLTP involvement, no concurrent-write risk): the MF API ingestion (`dim_mf_scheme`, `fact_mf_nav` — single-writer batch job), and the derived/batch tables `loan_delinquency`, `credit_bureau_score`, `customer_wealth_snapshot`.

---

## 4. The Data Model — 17 Tables, Column by Column

Legend: **Role** (Dimension = list of "things" · Fact = list of events, grows fast · Snapshot = periodic photo of state · Reference = static lookup) · **Layer** = medallion layer the clean version lives in · **Source** = Synthetic / Real API / Derived.

### Group 1 — Shared spine

**1. `dim_customer`** — *Dimension · Grain: one row per customer · ~50,000 rows · Synthetic · silver · OLTP-native*
| Column | Meaning |
|---|---|
| `customer_id` (PK) | unique customer identifier |
| `customer_type` | `individual` / `sme` |
| `full_name` | synthetic, format-valid name |
| `pan` | synthetic but format-valid PAN (tax ID) |
| `aadhaar_token` | tokenized synthetic Aadhaar — never a real one |
| `date_of_birth` | DOB |
| `kyc_status` | `verified` / `pending` / `failed` |
| `risk_category` | `low` / `medium` / `high` |
| `segment` | `retail` / `hni` / `sme` |
| `income_band` | coarse income bracket |
| `home_branch_city/state/ifsc` | home branch, denormalized (no separate branch table) |
| `relationship_manager` | assigned RM name |
| `customer_since` | relationship start date |
| `status` | `active` / `dormant` / `closed` |

**2. `dim_account`** — *Dimension (the spine) · Grain: one row per account · ~110,000 rows · Synthetic · silver · OLTP-native*
| Column | Meaning |
|---|---|
| `account_id` (PK) | unique account identifier |
| `customer_id` (FK → dim_customer) | owning customer |
| `account_type` | `deposit` / `card` / `loan` — the flag every downstream join branches on |
| `product_name` | product label (e.g. "Premium Savings") |
| `currency` | default `INR` |
| `open_date` | account opening date |
| `status` | `active` / `closed` / `written_off` — checked before every money-movement write |
| `close_date` | required if status = closed |

### Group 2 — Real MF data (loaded in Phase 2, independent of Group 1)

**3. `dim_mf_scheme`** — *Dimension · Grain: one row per fund scheme · ~500 curated rows · Real API · bronze→silver · Lakehouse-native*
| Column | Meaning |
|---|---|
| `scheme_code` | AMFI scheme code (from `api.mfapi.in`) |
| `scheme_name` | fund name |
| `amc_name` | fund house (AMC) |
| `scheme_category` | e.g. large-cap, debt, hybrid |
| `scheme_type` | open-ended / close-ended etc. |
| `plan` | direct / regular |
| `option` | growth / dividend |
| `isin` | ISIN identifier |

**4. `fact_mf_nav`** — *Fact (time series) · Grain: one row per fund per date · ~1,200,000 rows (largest table) · Real API · bronze→silver · partitioned by `nav_date` · Lakehouse-native*
| Column | Meaning |
|---|---|
| `nav_id` | surrogate row id |
| `scheme_code` | which fund |
| `nav_date` | the date this NAV applies to |
| `nav_value` | net asset value on that date, must be `> 0` |

### Group 3 — Deposit + card detail

**5. `deposit_account`** — *Dimension (account subtype) · Grain: one row per deposit account · ~70,000 rows · Synthetic · silver · OLTP-native*
| Column | Meaning |
|---|---|
| `account_id` (PK/FK → dim_account) | shared key with the spine |
| `deposit_type` | `savings` / `checking` / `term` / `overdraft` |
| `current_balance` | live balance — the value every deposit transaction locks and mutates |
| `interest_rate` | applicable rate |
| `min_balance` | floor balance (non-overdraft accounts) |
| `term_months` | tenure, for term deposits |
| `maturity_date` | maturity, for term deposits |
| `overdraft_limit` | max negative balance allowed, for overdraft accounts |

**6. `fact_deposit_txn`** — *Fact · Grain: one row per deposit money movement · ~500,000 rows · Synthetic · silver→gold · partitioned by `txn_date` (month) · OLTP-native origin, CDC'd*
| Column | Meaning |
|---|---|
| `txn_id` (PK) | unique transaction id |
| `account_id` (FK) | which deposit account |
| `txn_datetime` / `txn_date` | when it happened |
| `amount` | must be `> 0` |
| `dr_cr` | `debit` / `credit` |
| `running_balance` | balance immediately after this txn — trigger-enforced to equal prior row ± amount |
| `channel` | `branch` / `atm` / `netbanking` / `mobile` / `upi` |
| `narration` | free-text description |
| `idempotency_key` | unique client-supplied key, prevents double-posting on retry |

**7. `card_master`** — *Dimension · Grain: one row per card · ~55,000 rows · Synthetic · silver · OLTP-native*
| Column | Meaning |
|---|---|
| `card_id` (PK) | unique card identifier |
| `account_id` (FK) | linked account |
| `customer_id` (FK) | cardholder |
| `card_type` | `credit` / `debit` / `prepaid` |
| `network` | `visa` / `mastercard` / `rupay` |
| `card_token` | tokenized card number |
| `issue_date` / `expiry_date` | card lifecycle dates |
| `status` | `active` / `blocked` / `expired` |
| `credit_limit` | max allowed statement balance (credit cards) |
| `current_statement_balance` | live balance — locked and mutated by every purchase |
| `payment_due_date` | next due date |

**8. `fact_card_txn`** — *Fact · Grain: one row per card transaction · ~300,000 rows · Synthetic · silver→gold · partitioned by `txn_date` (month) · OLTP-native origin, CDC'd*
| Column | Meaning |
|---|---|
| `card_txn_id` (PK) | unique transaction id |
| `card_id` (FK) | which card |
| `merchant_name` / `mcc` / `merchant_category` / `merchant_city` | merchant details, denormalized |
| `txn_datetime` / `txn_date` | when it happened |
| `amount` | must be `> 0` |
| `txn_type` | `purchase` / `atm` / `refund` / `reversal` |
| `entry_mode` | `pos` / `ecom` / `contactless` / `atm` |
| `status` | `approved` / `declined` / `reversed` |
| `interchange_fee` | fee earned on the txn |
| `reward_points` | points accrued |
| `dispute_flag` | customer disputed this txn |
| `idempotency_key` | dedupe key for retries |

### Group 4 — Domain facts

**9. `fact_payment`** — *Fact · Grain: one row per payment · ~200,000 rows · Synthetic · silver→gold · partitioned by `payment_date` (month) · OLTP-native origin, CDC'd*
| Column | Meaning |
|---|---|
| `payment_id` (PK) | unique payment id |
| `from_account_id` (FK → dim_account) | paying account (type resolved at write time) |
| `beneficiary_name` / `beneficiary_account_or_vpa` / `beneficiary_type` | who's being paid |
| `rail` | `upi` / `neft` / `rtgs` / `imps` / `nach` / `bbps` / `swift` |
| `amount` | must be `> 0` |
| `payment_datetime` / `payment_date` | when |
| `status` | `initiated` / `settled` / `failed` / `returned` |
| `reference_no` | rail reference number |
| `payer_vpa` / `payee_vpa` | for UPI |
| `biller_category` | for BBPS bill payments |
| `idempotency_key` | dedupe key for retries |

### Group 5 — Lending chain

**10. `loan_account`** — *Dimension (account subtype) · Grain: one row per live loan · ~18,000 rows · Synthetic · silver · OLTP-native*
| Column | Meaning |
|---|---|
| `loan_id` (PK) | unique loan id |
| `account_id` (FK) | linked account |
| `customer_id` (FK) | borrower |
| `loan_type` | `personal` / `auto` / `mortgage` / `sme` |
| `requested_amount` | amount applied for |
| `cibil_at_application` | credit score at application time (300–900), sampled from a real prior `credit_bureau_score` pull |
| `application_date` | application date |
| `principal` | sanctioned principal |
| `interest_rate` / `tenure_months` / `emi_amount` | loan terms |
| `disbursal_date` | disbursal date |
| `outstanding_principal` | live balance — locked and mutated by every loan txn, `0 ≤ outstanding ≤ principal` |
| `status` | `active` / `closed` / `written_off` |
| `collection_status` | `in_collections` / `promise_to_pay` / null |

**11. `fact_loan_txn`** — *Fact · Grain: one row per loan money movement · ~120,000 rows · Synthetic · silver→gold · partitioned by `txn_date` (month) · OLTP-native origin, CDC'd*
| Column | Meaning |
|---|---|
| `loan_txn_id` (PK) | unique txn id |
| `loan_id` (FK) | which loan |
| `txn_date` | when |
| `amount` | must be `> 0` |
| `txn_type` | `disbursal` / `emi` / `prepayment` / `charge` |
| `principal_paid` / `interest_paid` | split of the payment (must sum ≤ `amount`) |
| `idempotency_key` | dedupe key for retries |

**12. `loan_delinquency`** — *Snapshot · Grain: one row per loan per month · ~120,000 rows · Derived · silver→gold · partitioned by `snapshot_date` (month) · Lakehouse-native batch*
| Column | Meaning |
|---|---|
| `snapshot_id` | surrogate row id |
| `loan_id` | which loan |
| `snapshot_date` | month-end snapshot date |
| `days_past_due` | DPD count, `≥ 0` |
| `dpd_bucket` | bucketed DPD (e.g. 0-30/31-60...) |
| `overdue_amount` | amount overdue |
| `risk_stage` | `1` / `2` / `3` — must stay consistent with `days_past_due` (pipeline-checked, not a single-column CHECK) |
| `provision_amount` | provisioned loss amount |

### Group 6 — MF ownership (hard-gated on Group 2 / Phase 2)

**13. `mf_folio`** — *Dimension · Grain: one row per customer per fund house · ~40,000 rows · Synthetic · silver · OLTP-native*
| Column | Meaning |
|---|---|
| `folio_id` (PK) | unique folio id |
| `customer_id` (FK) | folio owner |
| `amc_name` | fund house — drawn from AMCs already present in `dim_mf_scheme` |
| `folio_number` | folio number |
| `settlement_account_id` (FK → dim_account) | bank account funds settle to/from |
| `open_date` | folio open date |
| `status` | `active` / `closed` — closed blocks new transactions |

**14. `fact_mf_transaction`** — *Fact · Grain: one row per fund buy/sell event · ~350,000 rows · Synthetic, references real NAV · silver→gold · partitioned by `txn_date` (month) · OLTP-native origin, CDC'd*
| Column | Meaning |
|---|---|
| `mf_txn_id` (PK) | unique txn id |
| `folio_id` (FK) | which folio |
| `scheme_code` | which fund (must exist in `dim_mf_scheme`) |
| `txn_type` | `purchase` / `redemption` / `sip` / `switch_in` / `switch_out` / `dividend` |
| `txn_date` / `nav_date` | transaction date and the NAV date it priced against |
| `nav_value` | NAV used for this txn — must match a real, already-loaded NAV row |
| `amount` | must be `> 0` |
| `units` | must equal `amount / nav_value` within tolerance (0.00005) |
| `is_sip` | flag for systematic investment plan installments |
| `idempotency_key` | dedupe key for retries |

**15. `mf_holding_snapshot`** — *Snapshot · Grain: one row per folio per fund per month · ~250,000 rows · Derived (units × real NAV) · silver→gold · partitioned by `snapshot_date` (month) · hybrid table, two landing cadences*
| Column | Meaning |
|---|---|
| `folio_id` / `scheme_code` | which holding |
| `as_of_date` (OLTP) / `snapshot_date` (gold) | current date, or the monthly snapshot date |
| `units_held` | live unit balance, `≥ 0` |
| `invested_amount` | cumulative amount invested |
| `nav_value` | NAV used for this valuation |
| `market_value` | must equal `units_held × nav_value` |
| `unrealised_gain` | must equal `market_value − invested_amount` |

> Not a new table — one table, two landing cadences. `core.mf_holding_current` (OLTP) is upserted on every buy/sell and is what the redemption check reads. `gold.mf_holding_snapshot` is the original monthly-grain history, frozen from the current row at each month-end.

### Group 7 — Risk + derived

**16. `credit_bureau_score`** — *Fact (time series) · Grain: one row per customer per score pull · ~80,000 rows · Synthetic · silver · partitioned by `pull_date` (month) · Lakehouse-native batch*
| Column | Meaning |
|---|---|
| `score_id` | surrogate row id |
| `customer_id` | which customer |
| `bureau` | scoring bureau name |
| `score` | `300–900` |
| `band` | score band label |
| `pull_date` | when the score was pulled — generated *before* any `loan_account` row that samples from it |

**17. `customer_wealth_snapshot`** — *Snapshot · Grain: one row per customer per month · ~150,000 rows · Derived (deposits + MF holdings) · gold · partitioned by `snapshot_date` (month) · Lakehouse-native batch*
| Column | Meaning |
|---|---|
| `wealth_id` | surrogate row id |
| `customer_id` | which customer |
| `snapshot_date` | month-end date |
| `deposit_balance` | sum of that customer's deposit balances |
| `mf_aum` | sum of that customer's `mf_holding_snapshot.market_value` — cross-table check |
| `total_relationship_value` | must equal `deposit_balance + mf_aum` |
| `wealth_segment` | derived tier label |

### Operational tables (alongside the 17, not counted in it)

| Table | Grain | Purpose |
|---|---|---|
| `dim_date` | one row per calendar date | standard date dimension |
| `txn_rejection_log` | one row per rejected money-movement attempt | `rejection_id`, `source_table`, `attempted_key`, `account_or_folio_id`, `amount`, `reason_code` (`INSUFFICIENT_BALANCE`/`LIMIT_EXCEEDED`/`ACCOUNT_CLOSED`/`NAV_NOT_FOUND`/`UNITS_EXCEEDED`/`FK_NOT_FOUND`), `rejected_at`, `idempotency_key` — every rejected write is logged, never silently dropped |
| `dq_quarantine` | one row per bad ingested row | `quarantine_id`, `source_table`, `raw_payload`, `reason`, `quarantined_at` — bad API rows parked, not discarded |
| `dq_correction_log` | one row per correction applied | `correction_id`, `quarantine_id`, `field_corrected`, `old_value`, `new_value`, `corrected_at`, `corrected_by` — audit trail of fixes |
| `core.mf_nav_ref` | mirror of `fact_mf_nav` inside OLTP | `scheme_code`, `nav_date`, `nav_value` only — CDC'd Lakehouse→OLTP (the one reverse-direction flow) so `fact_mf_transaction`'s composite FK can be enforced declaratively at write time |

**Total volume: ~3.6M rows**, concentrated in the transaction and NAV fact tables.

---

## 5. The ACID Contract (the core engineering requirement)

Applies to the 5 money-movement tables: `fact_deposit_txn`, `fact_card_txn`, `fact_payment`, `fact_loan_txn`, `fact_mf_transaction`.

| Property | How it's guaranteed |
|---|---|
| **Atomicity** | Every write that touches more than one row/table is one `BEGIN…COMMIT` unit — lock parent row → check → insert fact → update balance, or roll back everything. |
| **Consistency** | Universal rule: *the transaction amount must never exceed the available balance/limit at the moment of write.* Declarative `CHECK`/FK where the DB engine can express it; application-layer pre-check for anything that needs the incoming amount or crosses tables (e.g. balance-vs-amount, `mf_aum` cross-table sum). |
| **Isolation** | `SERIALIZABLE`-equivalent via pessimistic row locking (`FOR UPDATE`) on exactly one row per write — the account/card/loan/`(folio_id, scheme_code)` row. A second concurrent write blocks until the first commits, then re-reads current state — this is what makes a lost-update double-overdraw impossible. |
| **Durability** | WAL-flushed commits (`synchronous_commit = on`); every fact table carries a `UNIQUE idempotency_key` so a retried write returns the original result instead of double-posting; every write follows **pre-check → write → post-verify**, and every rejected transaction is logged to `txn_rejection_log`, never silently dropped. |

**Per-table rejection rule:**

| Table | Rejects when |
|---|---|
| `fact_deposit_txn` | debit `>` current balance (respecting `min_balance`); balance may go negative only if `deposit_type = overdraft` and stays within `-overdraft_limit` |
| `fact_card_txn` | `current_statement_balance + amount > credit_limit` |
| `fact_payment` | `amount >` balance of `from_account_id` |
| `fact_loan_txn` | prepayment `amount > outstanding_principal` |
| `fact_mf_transaction` | redemption units requested `>` units held on that folio/scheme |

**Declarative vs. application-enforced, in one line:** the database enforces structure (PKs, FKs, enums, single-table `CHECK`s); application code enforces anything that requires reading the incoming transaction amount or comparing across tables.

---

## 6. Build Plan — Phases 0 through 5

| Phase | Goal | Exit criterion |
|---|---|---|
| **0 — Environment** | Provision Postgres (`core` schema) + Fabric Lakehouse (bronze/silver/gold); land all secrets (`MFAPI_BASE_URL`, connection strings, seed). No table data yet. | Both platforms reachable and empty; all secrets resolvable. |
| **1 — Schema** | Create all 17 tables + operational tables, fully constrained, in dependency order. No data loaded. | Every DDL applies cleanly; a smoke-test bad insert (negative NAV, over-limit purchase, closed-account payment) is rejected; a valid insert succeeds and rolls back. |
| **2 — Real MF data** *(hard gate)* | Load `dim_mf_scheme` + `fact_mf_nav` from `api.mfapi.in`; build the daily refresh job. **Phase 3 may not touch MF ownership tables until this passes.** | No duplicate `(scheme_code, nav_date)`; no invalid NAVs; every scheme has ≥1 NAV row (or is logged as excluded); OLTP mirror in sync. |
| **3 — Synthetic data generation** | Generate all synthetic rows, same dependency order as the table groups above, writing **through** the Phase 4 functions — never direct bulk INSERT into the 5 fact tables. | Row counts roughly match the ~3.6M volume plan; `txn_rejection_log` contains only expected/deliberate rejections; every table has data. |
| **4 — Transaction logic** | Build the 5 `core.post_*_txn` functions (deposit, card, payment, loan, MF) — idempotency check → row lock → consistency check → atomic write → post-verify. Built *before* Phase 3 runs, even though it's numbered after. | Unit tests pass: valid write commits; over-limit write is rejected + logged; two concurrent same-key writes correctly serialize; a repeated idempotency key returns the original result, not a duplicate. |
| **5 — Validation & fault-injection readiness** | Re-verify every invariant against the loaded gold dataset; confirm the fault-injection demo has a safe place to run. | All re-verification queries (running-balance, units-math, market-value, wealth-total, orphan-FK) return zero rows on the real dataset; each fault category dry-run once against a disposable silver-layer fork, confirmed catchable, then discarded. |

**Critical build-order notes:**
- Phase 4's functions exist *before* Phase 3's generation loop runs, even though Phase 3 is numbered first for "when data appears."
- Phase 2 is a hard gate on Group 6 (MF ownership) — the synthetic generator's scheme/date pool is *sampled from* real NAV data, never invented.
- `credit_bureau_score` pulls are generated before the `loan_account` rows that reference them, despite `loan_account` appearing earlier in the table list.

---

## 7. Fault Injection — Kept Out of Scope for `core`

The original POC purpose (data-quality fault injection) still applies, but never against the OLTP core or by disabling a Phase 1 constraint. Faults are injected only into a **disposable fork** of the bronze/silver Lakehouse layer:

| Fault category | Where injected | Caught by |
|---|---|---|
| Bad references (orphaned FK, missing NAV date) | Silver-layer fork | Anti-join checks / `dq_quarantine` |
| Broken math (`units ≠ amount/nav`, `market_value ≠ units×nav`) | Silver-layer fork | Tolerance `CHECK`s / Phase 5 re-verification |
| Bad values (score out of range, negative NAV) | Bronze re-processing path | `dq_quarantine` |
| Duplicates / gaps | Bronze re-processing path | Phase 2 exit-gate duplicate check |
| Inconsistent state (`risk_stage` vs DPD, wealth mismatch) | Silver-layer fork | Phase 5 queries |

This keeps "the schema prevents bad data" true for the real write path while still giving the DQ demo somewhere to inject and catch faults.

---

## 8. Volume Plan

| Table | ~Rows | Notes |
|---|---|---|
| dim_customer | 50,000 | |
| dim_account | 110,000 | |
| deposit_account | 70,000 | |
| **fact_deposit_txn** | 500,000 | partitioned |
| card_master | 55,000 | |
| **fact_card_txn** | 300,000 | partitioned |
| loan_account | 18,000 | |
| fact_loan_txn | 120,000 | |
| loan_delinquency | 120,000 | |
| **fact_payment** | 200,000 | partitioned |
| dim_mf_scheme | 500 | real |
| **fact_mf_nav** | 1,200,000 | real, biggest table |
| mf_folio | 40,000 | |
| **fact_mf_transaction** | 350,000 | partitioned |
| mf_holding_snapshot | 250,000 | partitioned |
| credit_bureau_score | 80,000 | |
| customer_wealth_snapshot | 150,000 | partitioned |
| **Total** | **~3.6M** | |
