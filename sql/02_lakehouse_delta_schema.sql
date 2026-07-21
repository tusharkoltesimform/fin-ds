-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Phase 1 DDL: Fabric Lakehouse schema (Delta / Spark SQL, bronze/silver/gold)
--
-- Source of truth: Banking_Data_Model_Build_Spec_v2_ACID.md, Part 1 + Phase 1
-- (.claude/specs/01-acid-transaction-integrity.md, .claude/specs/phase1-schema-ddl.md)
--
-- Scope: tables with no concurrent-write race to defend against — real MF
-- catalog/NAV data (single-writer batch loader, Phase 2), and pure
-- derived/batch snapshots computed from the OLTP core after CDC sync
-- (Phase 3 onward). Delta has no native PK/FK/UNIQUE enforcement; uniqueness
-- and referential integrity here are enforced by the pipeline (MERGE upsert
-- keys, pre-merge anti-joins) rather than declaratively — called out per table.
--
-- OLTP system-of-record tables (the 5 money-movement facts and the balance-
-- bearing parents they touch) live in 01_oltp_core_schema.sql.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;

-- -----------------------------------------------------------------------------
-- dim_date — standard calendar dimension. Not one of the 17 tables (see spec
-- note under "How to read each table spec"); generate/join the usual way.
-- -----------------------------------------------------------------------------

CREATE TABLE silver.dim_date (
  date_key       INT NOT NULL,          -- YYYYMMDD
  calendar_date  DATE NOT NULL,
  day_of_week    INT NOT NULL,
  day_name       STRING NOT NULL,
  day_of_month   INT NOT NULL,
  day_of_year    INT NOT NULL,
  week_of_year   INT NOT NULL,
  month_number   INT NOT NULL,
  month_name     STRING NOT NULL,
  quarter        INT NOT NULL,
  year           INT NOT NULL,
  is_weekend     BOOLEAN NOT NULL,
  is_month_end   BOOLEAN NOT NULL
) USING DELTA;

-- -----------------------------------------------------------------------------
-- Group 2 — Real MF data (independent of Group 1; Lakehouse-native, no OLTP)
-- -----------------------------------------------------------------------------

-- 3. dim_mf_scheme
-- Role: Dimension · Grain: one row per fund scheme · ~Volume: ~500 (curated)
-- Source: Real API (api.mfapi.in) · Layer: bronze -> silver
-- Depends on: Phase 2 API loader (/mf + /mf/{code}).
CREATE TABLE silver.dim_mf_scheme (
  scheme_code      STRING NOT NULL,
  scheme_name      STRING NOT NULL,
  amc_name         STRING NOT NULL,
  scheme_category  STRING,
  scheme_type      STRING,
  plan             STRING,
  option           STRING,
  isin             STRING
) USING DELTA;
ALTER TABLE silver.dim_mf_scheme ADD CONSTRAINT pk_scheme_code CHECK (scheme_code IS NOT NULL);
-- Delta has no native PK/UNIQUE enforcement pre-3.x uniform metastore support;
-- uniqueness on scheme_code is enforced by the MERGE upsert key in the Phase 2 loader (app-layer).

-- 4. fact_mf_nav
-- Role: Fact (time series) · Grain: one row per fund per date · ~Volume: ~1,200,000
-- Source: Real API · Layer: bronze -> silver · Partition: nav_date
-- (scheme_code, nav_date) uniqueness enforced by MERGE ... ON (scheme_code, nav_date)
-- WHEN NOT MATCHED THEN INSERT (app-layer). scheme_code FK to dim_mf_scheme
-- validated by pre-merge anti-join (app-layer). Rows failing nav_value > 0 or
-- an unparseable nav_date are quarantined (silver.dq_quarantine), not inserted here.
-- Depends on: Phase 2 API loader.
CREATE TABLE silver.fact_mf_nav (
  nav_id      STRING NOT NULL,
  scheme_code STRING NOT NULL,
  nav_date    DATE NOT NULL,
  nav_value   DECIMAL(14,5) NOT NULL
) USING DELTA
PARTITIONED BY (nav_date);

ALTER TABLE silver.fact_mf_nav ADD CONSTRAINT chk_nav_positive CHECK (nav_value > 0);

-- -----------------------------------------------------------------------------
-- Group 5 (cont.) — Lending chain, derived
-- -----------------------------------------------------------------------------

-- 12. loan_delinquency
-- Role: Snapshot · Grain: one row per loan per month · ~Volume: 120,000
-- Source: Synthetic (derived) · Layer: silver -> gold · Partition: snapshot_date
-- risk_stage vs. days_past_due cross-column consistency is NOT a single-column
-- CHECK -- enforced as a pipeline validation rule at generation time (Phase 3)
-- and re-checked in Phase 5.
-- Depends on: loan_account (OLTP; needs to exist and be CDC'd into silver first).
CREATE TABLE gold.loan_delinquency (
  snapshot_id     STRING NOT NULL,
  loan_id         STRING NOT NULL,
  snapshot_date   DATE NOT NULL,
  days_past_due   INT NOT NULL,
  dpd_bucket      STRING NOT NULL,
  overdue_amount  DECIMAL(18,2) NOT NULL,
  risk_stage      INT NOT NULL,
  provision_amount DECIMAL(18,2)
) USING DELTA PARTITIONED BY (snapshot_date);

ALTER TABLE gold.loan_delinquency ADD CONSTRAINT chk_dpd_nonneg CHECK (days_past_due >= 0);
ALTER TABLE gold.loan_delinquency ADD CONSTRAINT chk_risk_stage CHECK (risk_stage IN (1,2,3));

-- -----------------------------------------------------------------------------
-- Group 6 (cont.) — MF ownership, monthly history
-- -----------------------------------------------------------------------------

-- 15. mf_holding_snapshot (gold) — the original v1 monthly-grain table,
-- appended at each month-end by copying core.mf_holding_current as of that
-- date via the CDC/batch sync. Same table conceptually as OLTP's
-- core.mf_holding_current (01_oltp_core_schema.sql), two landing cadences,
-- not an 18th table.
-- Depends on: mf_folio, fact_mf_transaction (OLTP), and transitively Phase 2.
CREATE TABLE gold.mf_holding_snapshot (
  holding_id       STRING NOT NULL,
  folio_id         STRING NOT NULL,
  scheme_code      STRING NOT NULL,
  snapshot_date    DATE NOT NULL,
  units_held       DECIMAL(18,5) NOT NULL,
  invested_amount  DECIMAL(18,2) NOT NULL,
  nav_value        DECIMAL(14,5) NOT NULL,
  market_value     DECIMAL(18,2) NOT NULL,
  unrealised_gain  DECIMAL(18,2) NOT NULL
) USING DELTA PARTITIONED BY (snapshot_date);

-- -----------------------------------------------------------------------------
-- Group 7 — Risk + derived (Lakehouse-native batch; no OLTP, no concurrent-write risk)
-- -----------------------------------------------------------------------------

-- 16. credit_bureau_score
-- Role: Fact (time series) · Grain: one row per customer per score pull · ~Volume: ~80,000
-- Layer: silver · Partition: pull_date
-- Depends on: dim_customer (OLTP; CDC'd into silver so the FK-equivalent
-- anti-join can validate customer_id).
-- Build-order note: generate these pulls BEFORE the loan_account rows whose
-- cibil_at_application they back (Phase 3 sequencing, not a schema concern).
CREATE TABLE silver.credit_bureau_score (
  score_id     STRING NOT NULL,
  customer_id  STRING NOT NULL,
  bureau       STRING NOT NULL,
  score        INT NOT NULL,
  band         STRING NOT NULL,
  pull_date    DATE NOT NULL
) USING DELTA PARTITIONED BY (pull_date);

ALTER TABLE silver.credit_bureau_score ADD CONSTRAINT chk_score_range CHECK (score BETWEEN 300 AND 900);

-- 17. customer_wealth_snapshot
-- Role: Snapshot · Grain: one row per customer per month · ~Volume: ~150,000
-- Source: Derived (deposits + MF holdings) · Layer: gold · Partition: snapshot_date
-- total_relationship_value = deposit_balance + mf_aum is a declarative CHECK;
-- mf_aum must equal SUM(mf_holding_snapshot.market_value) for that customer --
-- a cross-table sum, application/pipeline-enforced, re-verified in Phase 5.
-- Depends on: deposit_account (OLTP, CDC'd balances) and mf_holding_snapshot
-- (both must be current for the snapshot month).
CREATE TABLE gold.customer_wealth_snapshot (
  wealth_id                 STRING NOT NULL,
  customer_id               STRING NOT NULL,
  snapshot_date              DATE NOT NULL,
  deposit_balance            DECIMAL(18,2) NOT NULL,
  mf_aum                     DECIMAL(18,2) NOT NULL,
  total_relationship_value   DECIMAL(18,2) NOT NULL,
  wealth_segment              STRING NOT NULL
) USING DELTA PARTITIONED BY (snapshot_date);

ALTER TABLE gold.customer_wealth_snapshot
  ADD CONSTRAINT chk_wealth_total CHECK (ABS(total_relationship_value - (deposit_balance + mf_aum)) <= 0.01);

-- -----------------------------------------------------------------------------
-- Operational tables (alongside the 17, same status as dim_date)
-- -----------------------------------------------------------------------------

-- dq_quarantine / dq_correction_log — built here in Phase 1 so Phase 2's
-- silver quarantine path (§2.2) has somewhere to write from day one.
-- Populated starting in Phase 2.
CREATE TABLE silver.dq_quarantine (
  quarantine_id  STRING NOT NULL,
  source_table   STRING NOT NULL,
  raw_payload    STRING NOT NULL,     -- the offending row, as received
  reason         STRING NOT NULL,
  quarantined_at TIMESTAMP NOT NULL
) USING DELTA;

CREATE TABLE silver.dq_correction_log (
  correction_id   STRING NOT NULL,
  quarantine_id   STRING NOT NULL,
  field_corrected STRING NOT NULL,
  old_value       STRING,
  new_value       STRING,
  corrected_at    TIMESTAMP NOT NULL,
  corrected_by    STRING NOT NULL
) USING DELTA;

-- =============================================================================
-- Exit criterion (Phase 1): every DDL block above applies successfully against
-- empty bronze/silver/gold containers; smoke-test insert with intentionally
-- bad data (negative NAV) is rejected by the constraint layer; smoke-test
-- insert with valid data succeeds and rolls back.
--
-- Hard gate (Phase 2, §2.3): mf_folio / fact_mf_transaction / mf_holding_snapshot
-- may not be generated (see 01_oltp_core_schema.sql) until dim_mf_scheme and
-- fact_mf_nav are loaded and pass the Phase 2 exit validation queries.
-- =============================================================================
