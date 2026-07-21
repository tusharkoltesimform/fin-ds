-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Phase 1 DDL: OLTP system-of-record schema (PostgreSQL 16, schema `core`)
--
-- Source of truth: Banking_Data_Model_Build_Spec_v2_ACID.md, Part 1 + Phase 1
-- (.claude/specs/01-acid-transaction-integrity.md, .claude/specs/phase1-schema-ddl.md)
--
-- Scope: every table whose writes must be transactionally consistent —
-- the 5 money-movement fact tables (fact_deposit_txn, fact_card_txn,
-- fact_payment, fact_loan_txn, fact_mf_transaction) and the balance-bearing
-- parent rows they touch, plus the operational txn_rejection_log.
-- Lakehouse-native tables (dim_mf_scheme, fact_mf_nav, loan_delinquency,
-- credit_bureau_score, customer_wealth_snapshot, gold.mf_holding_snapshot,
-- dq_quarantine, dq_correction_log) live in 02_lakehouse_delta_schema.sql.
--
-- Run this file once, in order, against an empty `core` schema (Phase 0
-- exit criterion). No data is loaded here (Phase 1 exit criterion: schema +
-- constraints only, validated empty).
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS core;

-- -----------------------------------------------------------------------------
-- Group 1 — Shared spine
-- -----------------------------------------------------------------------------

-- 1. dim_customer
-- Role: Dimension · Grain: one row per customer · ~Volume: 50,000
CREATE TABLE core.dim_customer (
  customer_id       TEXT PRIMARY KEY,
  customer_type     TEXT NOT NULL CHECK (customer_type IN ('individual','sme')),
  full_name         TEXT NOT NULL,
  pan               TEXT NOT NULL,
  aadhaar_token     TEXT,
  date_of_birth     DATE NOT NULL,
  kyc_status        TEXT NOT NULL CHECK (kyc_status IN ('verified','pending','failed')),
  risk_category     TEXT NOT NULL CHECK (risk_category IN ('low','medium','high')),
  segment           TEXT NOT NULL CHECK (segment IN ('retail','hni','sme')),
  income_band       TEXT,
  home_branch_city  TEXT,
  home_branch_state TEXT,
  home_branch_ifsc  TEXT,
  relationship_manager TEXT,
  customer_since    DATE NOT NULL,
  status            TEXT NOT NULL CHECK (status IN ('active','dormant','closed'))
);

-- 2. dim_account (the spine)
-- Role: Dimension · Grain: one row per account · ~Volume: 110,000
CREATE TABLE core.dim_account (
  account_id    TEXT PRIMARY KEY,
  customer_id   TEXT NOT NULL REFERENCES core.dim_customer(customer_id),
  account_type  TEXT NOT NULL CHECK (account_type IN ('deposit','card','loan')),
  product_name  TEXT NOT NULL,
  currency      TEXT NOT NULL DEFAULT 'INR',
  open_date     DATE NOT NULL,
  status        TEXT NOT NULL CHECK (status IN ('active','closed','written_off')),
  close_date    DATE,
  CHECK (status <> 'closed' OR close_date IS NOT NULL)
);

-- -----------------------------------------------------------------------------
-- Group 3 — Deposit + card detail
-- -----------------------------------------------------------------------------

-- 5. deposit_account
-- Role: Dimension (subtype of account) · Grain: one row per deposit account · ~Volume: 70,000
CREATE TABLE core.deposit_account (
  account_id       TEXT PRIMARY KEY REFERENCES core.dim_account(account_id),
  deposit_type     TEXT NOT NULL CHECK (deposit_type IN ('savings','checking','term','overdraft')),
  current_balance  DECIMAL(18,2) NOT NULL,
  interest_rate    DECIMAL(6,3),
  min_balance      DECIMAL(18,2) NOT NULL DEFAULT 0,
  term_months      INT,
  maturity_date    DATE,
  overdraft_limit  DECIMAL(18,2),
  CHECK (deposit_type = 'overdraft' OR current_balance >= min_balance OR current_balance >= 0),
  CHECK (deposit_type <> 'overdraft' OR current_balance >= -COALESCE(overdraft_limit, 0))
);

-- 6. fact_deposit_txn
-- Role: Fact · Grain: one row per money movement on a deposit account · ~Volume: 500,000
-- Full ACID contract: §1.1 atomic with deposit_account.current_balance,
-- §1.2 debit <= balance (respecting overdraft), §1.3 FOR UPDATE on deposit_account,
-- §1.4 idempotency_key + pre-check/write/post-verify.
CREATE TABLE core.fact_deposit_txn (
  txn_id           TEXT PRIMARY KEY,
  account_id       TEXT NOT NULL REFERENCES core.deposit_account(account_id),
  txn_datetime     TIMESTAMPTZ NOT NULL,
  txn_date         DATE NOT NULL,
  amount           DECIMAL(18,2) NOT NULL CHECK (amount > 0),
  dr_cr            TEXT NOT NULL CHECK (dr_cr IN ('debit','credit')),
  running_balance  DECIMAL(18,2) NOT NULL,
  channel          TEXT NOT NULL CHECK (channel IN ('branch','atm','netbanking','mobile','upi')),
  narration        TEXT,
  idempotency_key  TEXT UNIQUE
);

-- running_balance invariant: prior row's running_balance +/- amount, ordered by txn_datetime.
CREATE OR REPLACE FUNCTION core.trg_check_running_balance() RETURNS TRIGGER AS $$
DECLARE prior DECIMAL(18,2);
BEGIN
  SELECT running_balance INTO prior FROM core.fact_deposit_txn
    WHERE account_id = NEW.account_id AND txn_datetime < NEW.txn_datetime
    ORDER BY txn_datetime DESC LIMIT 1;
  IF prior IS NOT NULL THEN
    IF NEW.dr_cr = 'debit'  AND NEW.running_balance <> prior - NEW.amount THEN
      RAISE EXCEPTION 'running_balance inconsistent for account %', NEW.account_id; END IF;
    IF NEW.dr_cr = 'credit' AND NEW.running_balance <> prior + NEW.amount THEN
      RAISE EXCEPTION 'running_balance inconsistent for account %', NEW.account_id; END IF;
  END IF;
  RETURN NEW;
END; $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_deposit_running_balance
  BEFORE INSERT ON core.fact_deposit_txn
  FOR EACH ROW EXECUTE FUNCTION core.trg_check_running_balance();

-- 7. card_master
-- Role: Dimension · Grain: one row per card · ~Volume: 55,000
CREATE TABLE core.card_master (
  card_id          TEXT PRIMARY KEY,
  account_id       TEXT NOT NULL REFERENCES core.dim_account(account_id),
  customer_id      TEXT NOT NULL REFERENCES core.dim_customer(customer_id),
  card_type        TEXT NOT NULL CHECK (card_type IN ('credit','debit','prepaid')),
  network          TEXT NOT NULL CHECK (network IN ('visa','mastercard','rupay')),
  card_token       TEXT NOT NULL,
  issue_date       DATE NOT NULL,
  expiry_date      DATE NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('active','blocked','expired')),
  credit_limit     DECIMAL(18,2),
  current_statement_balance DECIMAL(18,2) NOT NULL DEFAULT 0,
  payment_due_date DATE,
  CHECK (card_type <> 'credit' OR current_statement_balance <= COALESCE(credit_limit, 0))
);

-- 8. fact_card_txn
-- Role: Fact · Grain: one row per card transaction · ~Volume: 300,000
-- Full ACID contract per §1.1/§1.2/§1.3/§1.4. Declined transactions are still
-- inserted (status='declined') outside the balance-mutating transaction (§1.1).
CREATE TABLE core.fact_card_txn (
  card_txn_id      TEXT PRIMARY KEY,
  card_id          TEXT NOT NULL REFERENCES core.card_master(card_id),
  merchant_name    TEXT,
  mcc              TEXT,
  merchant_category TEXT,
  merchant_city    TEXT,
  txn_datetime     TIMESTAMPTZ NOT NULL,
  txn_date         DATE NOT NULL,
  amount           DECIMAL(18,2) NOT NULL CHECK (amount > 0),
  txn_type         TEXT NOT NULL CHECK (txn_type IN ('purchase','atm','refund','reversal')),
  entry_mode       TEXT CHECK (entry_mode IN ('pos','ecom','contactless','atm')),
  status           TEXT NOT NULL CHECK (status IN ('approved','declined','reversed')),
  interchange_fee  DECIMAL(18,2),
  reward_points    INT,
  dispute_flag     BOOLEAN NOT NULL DEFAULT FALSE,
  idempotency_key  TEXT UNIQUE
);

-- -----------------------------------------------------------------------------
-- Group 4 — Domain facts
-- -----------------------------------------------------------------------------

-- 9. fact_payment
-- Role: Fact · Grain: one row per payment · ~Volume: 200,000
-- Full ACID contract: atomic with the balance-bearing row of from_account_id
-- (resolved via dim_account.account_type), §1.2 amount <= that balance,
-- §1.3 FOR UPDATE on the resolved row, §1.4.
CREATE TABLE core.fact_payment (
  payment_id       TEXT PRIMARY KEY,
  from_account_id  TEXT NOT NULL REFERENCES core.dim_account(account_id),
  beneficiary_name TEXT,
  beneficiary_account_or_vpa TEXT,
  beneficiary_type TEXT CHECK (beneficiary_type IN ('bank_account','upi','biller','international')),
  rail             TEXT NOT NULL CHECK (rail IN ('upi','neft','rtgs','imps','nach','bbps','swift')),
  amount           DECIMAL(18,2) NOT NULL CHECK (amount > 0),
  payment_datetime TIMESTAMPTZ NOT NULL,
  payment_date     DATE NOT NULL,
  status           TEXT NOT NULL CHECK (status IN ('initiated','settled','failed','returned')),
  reference_no     TEXT,
  payer_vpa        TEXT,
  payee_vpa        TEXT,
  biller_category  TEXT,
  idempotency_key  TEXT UNIQUE
);

-- -----------------------------------------------------------------------------
-- Group 5 — Lending chain
-- -----------------------------------------------------------------------------

-- 10. loan_account
-- Role: Dimension (subtype of account) · Grain: one row per live loan · ~Volume: 18,000
CREATE TABLE core.loan_account (
  loan_id               TEXT PRIMARY KEY,
  account_id            TEXT NOT NULL REFERENCES core.dim_account(account_id),
  customer_id           TEXT NOT NULL REFERENCES core.dim_customer(customer_id),
  loan_type             TEXT NOT NULL CHECK (loan_type IN ('personal','auto','mortgage','sme')),
  requested_amount      DECIMAL(18,2),
  cibil_at_application  INT CHECK (cibil_at_application BETWEEN 300 AND 900),
  application_date      DATE,
  principal             DECIMAL(18,2) NOT NULL,
  interest_rate         DECIMAL(6,3),
  tenure_months         INT,
  emi_amount            DECIMAL(18,2),
  disbursal_date        DATE,
  outstanding_principal DECIMAL(18,2) NOT NULL,
  status                TEXT NOT NULL CHECK (status IN ('active','closed','written_off')),
  collection_status     TEXT CHECK (collection_status IN ('in_collections','promise_to_pay') OR collection_status IS NULL),
  CHECK (outstanding_principal >= 0 AND outstanding_principal <= principal)
);

-- 11. fact_loan_txn
-- Role: Fact · Grain: one row per loan money movement · ~Volume: 120,000
-- Full ACID contract: atomic with loan_account.outstanding_principal,
-- §1.2 prepayment <= outstanding principal, §1.3 FOR UPDATE on loan_account, §1.4.
CREATE TABLE core.fact_loan_txn (
  loan_txn_id      TEXT PRIMARY KEY,
  loan_id          TEXT NOT NULL REFERENCES core.loan_account(loan_id),
  txn_date         DATE NOT NULL,
  amount           DECIMAL(18,2) NOT NULL CHECK (amount > 0),
  txn_type         TEXT NOT NULL CHECK (txn_type IN ('disbursal','emi','prepayment','charge')),
  principal_paid   DECIMAL(18,2) NOT NULL DEFAULT 0,
  interest_paid    DECIMAL(18,2) NOT NULL DEFAULT 0,
  idempotency_key  TEXT UNIQUE,
  CHECK (principal_paid + interest_paid <= amount + 0.01)
);

-- -----------------------------------------------------------------------------
-- Group 6 — MF ownership (needs real MF data from Phase 2; see mf_nav_ref below)
-- -----------------------------------------------------------------------------

-- core.mf_nav_ref — thin OLTP-side mirror of silver.fact_mf_nav (scheme_code,
-- nav_date, nav_value only), CDC'd from Lakehouse silver INTO OLTP so
-- fact_mf_transaction's composite FK can be declaratively enforced at write
-- time (§1.1 step 3 / §2.1 step 6). Populated by the Phase 2 loader, not here.
CREATE TABLE core.mf_nav_ref (
  scheme_code TEXT NOT NULL,
  nav_date    DATE NOT NULL,
  nav_value   DECIMAL(14,5) NOT NULL CHECK (nav_value > 0),
  PRIMARY KEY (scheme_code, nav_date)
);

-- 13. mf_folio
-- Role: Dimension · Grain: one row per customer per fund house · ~Volume: ~40,000
CREATE TABLE core.mf_folio (
  folio_id               TEXT PRIMARY KEY,
  customer_id            TEXT NOT NULL REFERENCES core.dim_customer(customer_id),
  amc_name               TEXT NOT NULL,
  folio_number           TEXT NOT NULL,
  settlement_account_id  TEXT REFERENCES core.dim_account(account_id),
  open_date              DATE NOT NULL,
  status                 TEXT NOT NULL CHECK (status IN ('active','closed'))
);

-- 14. fact_mf_transaction
-- Role: Fact · Grain: one row per fund buy/sell event · ~Volume: ~350,000
-- Full ACID contract: atomic with core.mf_holding_current, §1.2 redemption <=
-- units held, (scheme_code, nav_date) must pre-exist, units = amount/nav_value
-- tolerance, §1.3 FOR UPDATE on the (folio_id, scheme_code) holding row, §1.4.
CREATE TABLE core.fact_mf_transaction (
  mf_txn_id   TEXT PRIMARY KEY,
  folio_id    TEXT NOT NULL REFERENCES core.mf_folio(folio_id),
  scheme_code TEXT NOT NULL,
  txn_type    TEXT NOT NULL CHECK (txn_type IN ('purchase','redemption','sip','switch_in','switch_out','dividend')),
  txn_date    DATE NOT NULL,
  nav_date    DATE NOT NULL,
  nav_value   DECIMAL(14,5) NOT NULL,
  amount      DECIMAL(18,2) NOT NULL CHECK (amount > 0),
  units       DECIMAL(18,5) NOT NULL,
  is_sip      BOOLEAN NOT NULL DEFAULT FALSE,
  idempotency_key TEXT UNIQUE,
  CHECK (ABS(units - (amount / nav_value)) <= 0.00005),          -- §1.2 rounding tolerance
  FOREIGN KEY (scheme_code, nav_date) REFERENCES core.mf_nav_ref (scheme_code, nav_date)
);

-- 15. mf_holding_snapshot — OLTP current-state landing (one row per folio+scheme).
-- Upserted by every fact_mf_transaction (§1.1 step 5); this is the balance the
-- redemption check in §1.2 reads. The monthly-grain history lives in
-- gold.mf_holding_snapshot (02_lakehouse_delta_schema.sql), frozen from this
-- table at each month-end via CDC/batch sync — same table conceptually, two
-- landing cadences, not an 18th table.
CREATE TABLE core.mf_holding_current (
  folio_id         TEXT NOT NULL REFERENCES core.mf_folio(folio_id),
  scheme_code      TEXT NOT NULL,
  as_of_date       DATE NOT NULL,
  units_held       DECIMAL(18,5) NOT NULL CHECK (units_held >= 0),
  invested_amount  DECIMAL(18,2) NOT NULL,
  nav_value        DECIMAL(14,5) NOT NULL,
  market_value     DECIMAL(18,2) NOT NULL,
  unrealised_gain  DECIMAL(18,2) NOT NULL,
  PRIMARY KEY (folio_id, scheme_code),
  CHECK (ABS(market_value - units_held * nav_value) <= 0.01),
  CHECK (ABS(unrealised_gain - (market_value - invested_amount)) <= 0.01)
);

-- -----------------------------------------------------------------------------
-- Operational tables (alongside the 17, same status as dim_date)
-- -----------------------------------------------------------------------------

-- txn_rejection_log — every transaction that fails a §1.2 consistency check is
-- logged here, never silently dropped (§1.4). Populated starting in Phase 4.
CREATE TABLE core.txn_rejection_log (
  rejection_id        TEXT PRIMARY KEY,
  source_table        TEXT NOT NULL CHECK (source_table IN
                         ('fact_deposit_txn','fact_card_txn','fact_payment','fact_loan_txn','fact_mf_transaction')),
  attempted_key        TEXT,
  account_or_folio_id  TEXT NOT NULL,
  amount               DECIMAL(18,2) NOT NULL,
  reason_code          TEXT NOT NULL CHECK (reason_code IN
                         ('INSUFFICIENT_BALANCE','LIMIT_EXCEEDED','ACCOUNT_CLOSED',
                          'NAV_NOT_FOUND','UNITS_EXCEEDED','FK_NOT_FOUND')),
  rejected_at          TIMESTAMPTZ NOT NULL,
  idempotency_key      TEXT
);

-- =============================================================================
-- Exit criterion (Phase 1): every DDL block above applies successfully against
-- an empty `core` schema; all FKs resolve (trivially true, no rows yet); a
-- smoke-test insert with intentionally bad data (negative amount, over-limit
-- card purchase, closed-account payment) is rejected by the constraint layer,
-- and a smoke-test insert with valid data succeeds and rolls back.
-- =============================================================================
