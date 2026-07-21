-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Phase 3 prerequisite DDL: Postgres stand-in for the remaining Lakehouse
-- silver/gold derived tables that Phase 3 needs to write into.
--
-- Source of truth: .claude/specs/phase1-schema-ddl.md, .claude/specs/appendix.md
--
-- Why this file exists: same reasoning as sql/03_lakehouse_standin_schema.sql
-- (no Spark/Fabric environment in this workspace) extended to the tables
-- Phase 1 left as DDL-only in 02_lakehouse_delta_schema.sql that Phase 3 is
-- now responsible for populating: silver.credit_bureau_score,
-- gold.loan_delinquency, gold.mf_holding_snapshot, gold.customer_wealth_snapshot.
-- Shapes match 02_lakehouse_delta_schema.sql exactly (STRING -> TEXT,
-- USING DELTA / PARTITIONED BY dropped). No declarative FK to core.* tables,
-- matching Delta's lack of native FK — Phase 3's generator validates parent
-- existence itself (it's the only writer), the same way the Phase 2 loader
-- does its own pre-merge anti-join instead of relying on a declared FK.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

-- 16. credit_bureau_score (silver) — one row per customer per score pull.
-- Generated BEFORE loan_account per Phase 1's build-order note: a loan's
-- cibil_at_application samples from a real prior pull for that customer.
CREATE TABLE silver.credit_bureau_score (
  score_id     TEXT NOT NULL PRIMARY KEY,
  customer_id  TEXT NOT NULL,
  bureau       TEXT NOT NULL,
  score        INT NOT NULL,
  band         TEXT NOT NULL,
  pull_date    DATE NOT NULL,
  CONSTRAINT chk_score_range CHECK (score BETWEEN 300 AND 900)
);

CREATE INDEX idx_credit_bureau_score_customer ON silver.credit_bureau_score (customer_id, pull_date);

-- 12. loan_delinquency (gold) — one row per loan per month.
CREATE TABLE gold.loan_delinquency (
  snapshot_id      TEXT NOT NULL PRIMARY KEY,
  loan_id          TEXT NOT NULL,
  snapshot_date    DATE NOT NULL,
  days_past_due    INT NOT NULL,
  dpd_bucket       TEXT NOT NULL,
  overdue_amount   DECIMAL(18,2) NOT NULL,
  risk_stage       INT NOT NULL,
  provision_amount DECIMAL(18,2),
  CONSTRAINT chk_dpd_nonneg CHECK (days_past_due >= 0),
  CONSTRAINT chk_risk_stage CHECK (risk_stage IN (1,2,3))
);

CREATE INDEX idx_loan_delinquency_loan ON gold.loan_delinquency (loan_id, snapshot_date);

-- 15. mf_holding_snapshot (gold) — monthly-grain history, frozen from
-- core.mf_holding_current at each month-end (Phase 3 reconstructs this
-- retroactively from fact_mf_transaction history for the backfill).
CREATE TABLE gold.mf_holding_snapshot (
  holding_id       TEXT NOT NULL PRIMARY KEY,
  folio_id         TEXT NOT NULL,
  scheme_code      TEXT NOT NULL,
  snapshot_date    DATE NOT NULL,
  units_held       DECIMAL(18,5) NOT NULL,
  invested_amount  DECIMAL(18,2) NOT NULL,
  nav_value        DECIMAL(14,5) NOT NULL,
  market_value     DECIMAL(18,2) NOT NULL,
  unrealised_gain  DECIMAL(18,2) NOT NULL,
  CONSTRAINT chk_holding_market_value CHECK (ABS(market_value - units_held * nav_value) <= 0.01),
  CONSTRAINT uq_holding_folio_scheme_month UNIQUE (folio_id, scheme_code, snapshot_date)
);

CREATE INDEX idx_mf_holding_snapshot_folio ON gold.mf_holding_snapshot (folio_id, snapshot_date);

-- 17. customer_wealth_snapshot (gold) — one row per customer per month.
CREATE TABLE gold.customer_wealth_snapshot (
  wealth_id                TEXT NOT NULL PRIMARY KEY,
  customer_id              TEXT NOT NULL,
  snapshot_date            DATE NOT NULL,
  deposit_balance          DECIMAL(18,2) NOT NULL,
  mf_aum                   DECIMAL(18,2) NOT NULL,
  total_relationship_value DECIMAL(18,2) NOT NULL,
  wealth_segment           TEXT NOT NULL,
  CONSTRAINT chk_wealth_total CHECK (ABS(total_relationship_value - (deposit_balance + mf_aum)) <= 0.01),
  CONSTRAINT uq_wealth_customer_month UNIQUE (customer_id, snapshot_date)
);

CREATE INDEX idx_customer_wealth_snapshot_customer ON gold.customer_wealth_snapshot (customer_id, snapshot_date);

-- =============================================================================
-- Exit criterion: applies cleanly against forpocdb (silver/gold schemas
-- already exist from Phase 2 for silver; gold is new here). Phase 3 is the
-- sole writer of these four tables in this workspace.
-- =============================================================================
