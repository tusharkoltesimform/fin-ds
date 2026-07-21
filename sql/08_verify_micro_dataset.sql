-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Verification query for the SYNTH_MICRO=1 (3-customer, every-module-covered)
-- dataset: overall row counts per table, plus a per-customer coverage matrix
-- across every domain, so it's obvious at a glance that all N customers touch
-- every module (no zero cells). Rerun any time after
-- `SYNTH_MICRO=1 .venv/Scripts/python.exe -m scripts.synth_gen.main`.
-- =============================================================================

\echo '--- table row counts ---'
SELECT 'dim_customer', count(*) FROM core.dim_customer
UNION ALL SELECT 'dim_account', count(*) FROM core.dim_account
UNION ALL SELECT 'deposit_account', count(*) FROM core.deposit_account
UNION ALL SELECT 'fact_deposit_txn', count(*) FROM core.fact_deposit_txn
UNION ALL SELECT 'card_master', count(*) FROM core.card_master
UNION ALL SELECT 'fact_card_txn', count(*) FROM core.fact_card_txn
UNION ALL SELECT 'fact_payment', count(*) FROM core.fact_payment
UNION ALL SELECT 'credit_bureau_score', count(*) FROM silver.credit_bureau_score
UNION ALL SELECT 'loan_account', count(*) FROM core.loan_account
UNION ALL SELECT 'fact_loan_txn', count(*) FROM core.fact_loan_txn
UNION ALL SELECT 'loan_delinquency', count(*) FROM gold.loan_delinquency
UNION ALL SELECT 'mf_folio', count(*) FROM core.mf_folio
UNION ALL SELECT 'fact_mf_transaction', count(*) FROM core.fact_mf_transaction
UNION ALL SELECT 'mf_holding_current', count(*) FROM core.mf_holding_current
UNION ALL SELECT 'mf_holding_snapshot', count(*) FROM gold.mf_holding_snapshot
UNION ALL SELECT 'customer_wealth_snapshot', count(*) FROM gold.customer_wealth_snapshot
UNION ALL SELECT 'txn_rejection_log', count(*) FROM core.txn_rejection_log
ORDER BY 1;

\echo '--- per-customer coverage matrix ---'
SELECT
  c.customer_id,
  c.full_name,
  (SELECT count(*) FROM core.dim_account a WHERE a.customer_id = c.customer_id) AS accounts,
  (SELECT count(*) FROM core.deposit_account da JOIN core.dim_account a ON a.account_id = da.account_id WHERE a.customer_id = c.customer_id) AS deposit_accts,
  (SELECT count(*) FROM core.fact_deposit_txn dt JOIN core.deposit_account da ON da.account_id = dt.account_id
     JOIN core.dim_account a ON a.account_id = da.account_id WHERE a.customer_id = c.customer_id) AS deposit_txns,
  (SELECT count(*) FROM core.card_master cm WHERE cm.customer_id = c.customer_id) AS cards,
  (SELECT count(*) FROM core.fact_card_txn ct JOIN core.card_master cm ON cm.card_id = ct.card_id WHERE cm.customer_id = c.customer_id) AS card_txns,
  (SELECT count(*) FROM core.fact_payment p JOIN core.dim_account a ON a.account_id = p.from_account_id WHERE a.customer_id = c.customer_id) AS payments,
  (SELECT count(*) FROM silver.credit_bureau_score s WHERE s.customer_id = c.customer_id) AS bureau_pulls,
  (SELECT count(*) FROM core.loan_account l WHERE l.customer_id = c.customer_id) AS loans,
  (SELECT count(*) FROM core.fact_loan_txn lt JOIN core.loan_account l ON l.loan_id = lt.loan_id WHERE l.customer_id = c.customer_id) AS loan_txns,
  (SELECT count(*) FROM gold.loan_delinquency d JOIN core.loan_account l ON l.loan_id = d.loan_id WHERE l.customer_id = c.customer_id) AS delinquency_rows,
  (SELECT count(*) FROM core.mf_folio f WHERE f.customer_id = c.customer_id) AS mf_folios,
  (SELECT count(*) FROM core.fact_mf_transaction mt JOIN core.mf_folio f ON f.folio_id = mt.folio_id WHERE f.customer_id = c.customer_id) AS mf_txns,
  (SELECT count(*) FROM gold.mf_holding_snapshot h JOIN core.mf_folio f ON f.folio_id = h.folio_id WHERE f.customer_id = c.customer_id) AS mf_holding_rows,
  (SELECT count(*) FROM gold.customer_wealth_snapshot w WHERE w.customer_id = c.customer_id) AS wealth_rows
FROM core.dim_customer c
ORDER BY c.customer_id;
