-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Phase 2 DDL: Postgres stand-in for the Fabric Lakehouse bronze/silver layers
--
-- Source of truth: .claude/specs/phase2-mf-api-ingestion.md, .claude/specs/02-mutual-fund-data-sourcing.md
--
-- Why this file exists: 02_lakehouse_delta_schema.sql is the real Delta/Spark
-- DDL for the eventual Fabric Lakehouse, but per implemented/phase1-schema-ddl.md
-- no Spark/Fabric environment exists in this workspace — that file was never
-- deployed. Phase 2 needs somewhere real to land bronze/silver so the loader
-- can actually run against api.mfapi.in and the §2.3 exit-gate SQL can actually
-- execute. This file stands the Phase-2-relevant subset of that schema up in
-- the same local Postgres instance (forpocdb) that already hosts `core`.
--
-- This is a stand-in, not a redefinition: table shapes match
-- 02_lakehouse_delta_schema.sql exactly (STRING -> TEXT, DECIMAL unchanged,
-- USING DELTA / PARTITIONED BY dropped since Postgres doesn't use that syntax).
-- Where Delta has no native PK/FK/UNIQUE and the spec calls for app-layer
-- enforcement (MERGE upsert key, pre-merge anti-join), this file deliberately
-- does NOT add a declarative FK either, so the loader still has to implement
-- and demonstrate that pattern — see scripts/mf_loader/silver.py. A UNIQUE
-- constraint is added as a safety net where Delta would rely on a MERGE key,
-- since Postgres offers it for free and it costs nothing to keep.
--
-- When a real Fabric workspace exists, swap the loader's target from this
-- schema to 02_lakehouse_delta_schema.sql's tables — the shapes already match.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;

-- -----------------------------------------------------------------------------
-- Bronze: raw API payloads landed byte-for-byte as received, no transform.
-- Replayable source of truth if silver logic ever needs to be rerun (§2.2).
-- -----------------------------------------------------------------------------

CREATE TABLE bronze.mf_api_raw (
  raw_id       BIGSERIAL PRIMARY KEY,
  endpoint     TEXT NOT NULL,              -- 'mf_list' or 'mf_scheme'
  scheme_code  TEXT,                       -- NULL for the mf_list call
  payload      JSONB NOT NULL,             -- exact API response body
  fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_bronze_mf_api_raw_scheme ON bronze.mf_api_raw (scheme_code, fetched_at);

-- -----------------------------------------------------------------------------
-- Silver: cast + deduped + quarantine-clean. Matches
-- 02_lakehouse_delta_schema.sql's silver.dim_mf_scheme / silver.fact_mf_nav.
-- -----------------------------------------------------------------------------

-- 3. dim_mf_scheme
CREATE TABLE silver.dim_mf_scheme (
  scheme_code      TEXT NOT NULL PRIMARY KEY,
  scheme_name      TEXT NOT NULL,
  amc_name         TEXT NOT NULL,
  scheme_category  TEXT,
  scheme_type      TEXT,
  plan             TEXT,
  option           TEXT,
  isin             TEXT,
  loaded_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. fact_mf_nav
-- No declarative FK to dim_mf_scheme, by design — matching Delta's lack of
-- native FK support. scheme_code existence is validated by a pre-merge
-- anti-join in the loader (§2.2), same as it would be against real Delta.
CREATE TABLE silver.fact_mf_nav (
  nav_id       TEXT NOT NULL PRIMARY KEY,
  scheme_code  TEXT NOT NULL,
  nav_date     DATE NOT NULL,
  nav_value    DECIMAL(14,5) NOT NULL,
  loaded_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT chk_nav_positive CHECK (nav_value > 0),
  CONSTRAINT uq_scheme_nav_date UNIQUE (scheme_code, nav_date)
);

CREATE INDEX idx_silver_fact_mf_nav_scheme ON silver.fact_mf_nav (scheme_code);

-- -----------------------------------------------------------------------------
-- Operational: dq_quarantine / dq_correction_log — built in Phase 1's spec but
-- never deployed for the same no-Fabric reason above. Needed from day one of
-- Phase 2's silver quarantine path (§2.2).
-- -----------------------------------------------------------------------------

CREATE TABLE silver.dq_quarantine (
  quarantine_id   TEXT NOT NULL PRIMARY KEY,
  source_table    TEXT NOT NULL,
  raw_payload     TEXT NOT NULL,   -- the offending row, as received
  reason          TEXT NOT NULL,
  quarantined_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE silver.dq_correction_log (
  correction_id    TEXT NOT NULL PRIMARY KEY,
  quarantine_id    TEXT NOT NULL,
  field_corrected  TEXT NOT NULL,
  old_value        TEXT,
  new_value        TEXT,
  corrected_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  corrected_by     TEXT NOT NULL
);

-- =============================================================================
-- Exit criterion (Phase 2, §2.3): loader populates silver.dim_mf_scheme and
-- silver.fact_mf_nav from api.mfapi.in, mirrors into core.mf_nav_ref
-- (01_oltp_core_schema.sql, already deployed empty), and all four §2.3 queries
-- pass. Run this file against an empty forpocdb before running the loader.
-- =============================================================================
