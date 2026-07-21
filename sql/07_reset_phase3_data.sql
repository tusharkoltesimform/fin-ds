-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Resets every table Phase 3's generator writes to, WITHOUT touching Phase 1's
-- schema or Phase 2's real MF data (silver.dim_mf_scheme, silver.fact_mf_nav,
-- core.mf_nav_ref, dq_quarantine/dq_correction_log). Safe to rerun the
-- generator from a clean slate (smoke test, then full run).
--
-- A single TRUNCATE statement lists every dependent table together so
-- Postgres doesn't need CASCADE or a specific order.
-- =============================================================================

TRUNCATE TABLE
  core.txn_rejection_log,
  core.fact_mf_transaction,
  core.mf_holding_current,
  core.mf_folio,
  core.fact_loan_txn,
  core.loan_account,
  core.fact_payment,
  core.fact_card_txn,
  core.card_master,
  core.fact_deposit_txn,
  core.deposit_account,
  core.dim_account,
  core.dim_customer,
  silver.credit_bureau_score,
  gold.loan_delinquency,
  gold.mf_holding_snapshot,
  gold.customer_wealth_snapshot;
