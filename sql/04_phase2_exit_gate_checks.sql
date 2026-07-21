-- =============================================================================
-- Phase 2 §2.3 — exit-gate validation queries, run verbatim against forpocdb
-- after scripts/mf_loader/full_load.py completes.
-- =============================================================================

-- No duplicate (scheme_code, nav_date)
SELECT scheme_code, nav_date, COUNT(*) FROM silver.fact_mf_nav
GROUP BY scheme_code, nav_date HAVING COUNT(*) > 1;               -- must return 0 rows

-- No invalid NAVs
SELECT * FROM silver.fact_mf_nav WHERE nav_value <= 0;             -- must return 0 rows

-- Row-count sanity: every scheme in dim_mf_scheme has at least one NAV row
SELECT s.scheme_code FROM silver.dim_mf_scheme s
LEFT JOIN silver.fact_mf_nav n ON s.scheme_code = n.scheme_code
WHERE n.scheme_code IS NULL;                                       -- expect 0, or a known/logged short-list

-- OLTP mirror is in sync
SELECT COUNT(*) FROM core.mf_nav_ref;                               -- roughly matches silver.fact_mf_nav row count
SELECT COUNT(*) FROM silver.fact_mf_nav;
