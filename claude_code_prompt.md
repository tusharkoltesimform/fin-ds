# Prompt for Claude Code (Plan Mode)

Copy everything below the line into Claude Code.

---

## Context

I have a data model spec for a banking POC database: `Entire_Story___Data_Model.md` (attached in this repo / will be provided). It defines **25 tables** across 6 domains — Customer/Account spine, Retail Deposits, Cards, Lending, Payments, Mutual Funds — plus a central `gl_entry` ledger. Read the whole file before planning anything; do not start from assumptions about table names or columns.

Build a **PostgreSQL** database (schema + transactional logic + a real-time sync job) that implements this model correctly. This is a plan-mode task — propose a full implementation plan first, and don't write code until the plan is confirmed.

## Non-negotiable requirements

### 1. ACID correctness
- Every operation that touches more than one table (e.g. a deposit withdrawal that updates `deposit_account.current_balance`, inserts into `fact_deposit_txn`, and posts to `gl_entry`) must run inside a **single database transaction** — all-or-nothing, no partial writes.
- Use proper `FOREIGN KEY`, `CHECK`, `NOT NULL`, and `UNIQUE` constraints so the schema itself rejects bad data (e.g. a `fact_mf_transaction.nav_date` that doesn't exist in `fact_mf_nav`, or a transaction on a `closed` account).
- Use row-level locking (`SELECT ... FOR UPDATE`) on the balance row before debiting it, so two concurrent transactions on the same account can never both pass a balance check and overdraw it (classic race condition). Pick an appropriate isolation level and justify it in the plan.
- Every money-movement fact (`fact_deposit_txn`, `fact_card_txn`, `fact_loan_txn`, `fact_payment`, `fact_mf_transaction`) must also produce a balanced `gl_entry` posting (debits = credits) in the **same transaction**. If the ledger write fails, the whole transaction rolls back.

### 2. Correct user / transaction flow
- Enforce the real-world sequence implied by the model: a customer must exist and be KYC-`verified` before opening an account; an account must be `active` (not `closed`/`written_off`) to transact; a loan must go through `loan_application` → approval → `loan_account` before any `fact_loan_txn`; an `mf_folio` must exist before any `fact_mf_transaction`, and that transaction's `(scheme_code, nav_date)` must already exist in `fact_mf_nav`.
- Model this as a small set of **stored procedures / functions** (not raw ad-hoc INSERTs) that encapsulate each real-world action: `withdraw_from_deposit()`, `make_card_purchase()`, `make_payment()`, `buy_mutual_fund()`, `redeem_mutual_fund()`, `disburse_loan()`, `post_emi_payment()`, etc. Each function should validate the flow preconditions above before touching any balance.

### 3. Spend-must-be-less-than-balance rule
- For every debit-type transaction (deposit withdrawal/spend, card purchase against available credit, payment, mutual fund purchase drawn from a settlement account, EMI payment), the transaction amount must be validated against the **currently available balance/limit** — not a stale or cached value — before the debit is applied.
- If `amount > available_balance` (or `> available_credit_limit` for cards), the whole transaction must be **rejected and rolled back**, with a clear error, and **no rows written anywhere** (not even the ledger).
- Available balance for deposit accounts should account for `min_balance` / `overdraft_limit` correctly (i.e. "available" isn't just `current_balance`, it's `current_balance - min_balance` or `current_balance + overdraft_limit` depending on account type — infer the right rule from `deposit_account`).
- Add regression tests that specifically try to overdraw an account (including two concurrent requests racing each other) and assert both that the second one fails and that the balance never goes negative/over-limit.

### 4. Real-time mutual fund data reflected back into the database
- Build a loader/sync component that calls the real `api.mfapi.in` API:
  - One-time backfill: full scheme list + full NAV history per scheme into `dim_amc`, `dim_mf_scheme`, `fact_mf_nav`.
  - A repeatable **daily/on-demand refresh job** that pulls the latest NAV per scheme and **upserts** it into `fact_mf_nav` (handle the API's string NAV values, `DD-MM-YYYY` dates, and duplicate/missing-day cases per the DE notes in the spec).
- After a NAV refresh, the job must **recompute dependent state in the same DB**: update `mf_holding_snapshot.nav_value` / `market_value` / `unrealised_gain` for affected folios, and roll that up into `customer_wealth_snapshot.mf_aum` and `total_relationship_value`. This must happen transactionally per scheme/date so the DB is never left with a NAV that doesn't match its snapshot.
- `fact_mf_transaction.units` must always equal `amount ÷ nav_value` at insert time, using a real NAV row that exists in `fact_mf_nav` — enforce this with a constraint or a trigger, not just application logic.

### 5. Schema fidelity to the spec
- Implement all 25 tables with the columns, types, PKs, and FKs exactly as described in `Entire_Story___Data_Model.md`. Map the doc's generic types to Postgres types (e.g. `DECIMAL(18,2)` → `numeric(18,2)`, `STRING` → `text`, `TIMESTAMP` → `timestamptz`).
- Add the invariants called out in each table's "DE note" as real constraints/triggers where practical (e.g. `repayment_schedule` principal components summing to loan principal, `market_value = units_held * nav_value`, ledger debits = credits per `source_txn_id`).
- Partition the large fact tables (`fact_deposit_txn`, `fact_card_txn`, `fact_payment`, `fact_mf_nav`, `fact_mf_transaction`, `repayment_schedule`, `gl_entry`) by their date column using native Postgres partitioning, per the spec's suggested partition columns.
- Follow the load/dependency order in the spec's "Load / generation order" section for both schema creation (FK dependency order) and any seed/synthetic data generation.

## What I want back from plan mode

1. A proposed **schema design** (table-by-table DDL plan, not full code yet) with all constraints/triggers called out.
2. A proposed set of **transactional functions/procedures** for each real-world action, with their preconditions and locking strategy.
3. A proposed design for the **MF real-time sync job** (backfill + refresh) and how it propagates into snapshots.
4. A **test plan** covering: ACID rollback on failure, concurrent overdraw attempts, MF unit/NAV consistency, and ledger debit=credit reconciliation.
5. Call out any ambiguities you find in the data model doc (e.g. exact "available balance" formula, how card available-credit is computed) and your assumed resolution, before generating code.

Wait for my confirmation of the plan before writing schema/migration code.
