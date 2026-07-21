-- =============================================================================
-- fin-ds — Banking Data Model POC
-- Phase 4: read-check-write-verify transaction functions (ACID enforcement in code)
--
-- Source of truth: .claude/specs/phase4-transaction-logic.md, .claude/specs/01-acid-transaction-integrity.md
--
-- Implementation note (deviation from the phase4 spec's literal pseudocode,
-- called out explicitly in the spec itself as something "whoever implements
-- this" needs to resolve): the spec's sketch uses a bare RAISE EXCEPTION for
-- a business rejection, which would also roll back the txn_rejection_log
-- insert unless given its own autonomous transaction (dblink) or a savepoint.
-- This file uses the savepoint route: every function wraps its
-- lock -> check -> insert -> update sequence in an inner
-- `BEGIN ... EXCEPTION WHEN OTHERS ... END` block. plpgsql gives that block
-- an implicit savepoint, so a RAISE EXCEPTION inside it rolls back only the
-- attempted write, not the whole function call — the txn_rejection_log
-- insert that follows, outside the block, still commits as part of the
-- caller's single-statement transaction. Business-rejection codes are raised
-- with the reason_code itself as the exception message; anything that comes
-- back as a message NOT in the known reason_code list is re-raised
-- (`RAISE;`) rather than swallowed, so a real bug surfaces as a hard error
-- instead of being silently logged as a fake business rejection — this is
-- what keeps Phase 3's exit criterion ("txn_rejection_log contains only
-- expected rejections") meaningful.
--
-- All five functions return the generated txn id on success (or the existing
-- one, unchanged, on an idempotent replay) and NULL on a logged rejection —
-- except post_card_txn, which per §1.1 always inserts a fact_card_txn row
-- (status='approved' or 'declined') and returns its id either way.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- 1. core.post_deposit_txn
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.post_deposit_txn(
  p_account_id      TEXT,
  p_amount          DECIMAL(18,2),
  p_dr_cr           TEXT,
  p_channel         TEXT,
  p_narration       TEXT,
  p_txn_datetime    TIMESTAMPTZ,
  p_idempotency_key TEXT
) RETURNS TEXT AS $$
DECLARE
  v_bal DECIMAL(18,2); v_min DECIMAL(18,2); v_od DECIMAL(18,2);
  v_type TEXT; v_status TEXT; v_new_bal DECIMAL(18,2);
  v_txn_id TEXT; v_existing TEXT;
  v_reason TEXT; v_rejected BOOLEAN := FALSE;
  v_known_codes TEXT[] := ARRAY['INSUFFICIENT_BALANCE','LIMIT_EXCEEDED','ACCOUNT_CLOSED','FK_NOT_FOUND'];
BEGIN
  SELECT txn_id INTO v_existing FROM core.fact_deposit_txn WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN RETURN v_existing; END IF;

  v_txn_id := 'DTXN_' || gen_random_uuid()::TEXT;

  BEGIN
    SELECT da.current_balance, da.min_balance, da.overdraft_limit, da.deposit_type, a.status
      INTO v_bal, v_min, v_od, v_type, v_status
      FROM core.deposit_account da JOIN core.dim_account a ON a.account_id = da.account_id
      WHERE da.account_id = p_account_id FOR UPDATE OF da;

    IF NOT FOUND THEN RAISE EXCEPTION 'FK_NOT_FOUND'; END IF;
    IF v_status <> 'active' THEN RAISE EXCEPTION 'ACCOUNT_CLOSED'; END IF;

    v_new_bal := CASE WHEN p_dr_cr = 'debit' THEN v_bal - p_amount ELSE v_bal + p_amount END;

    IF p_dr_cr = 'debit' AND v_type <> 'overdraft' AND v_new_bal < v_min THEN
      RAISE EXCEPTION 'INSUFFICIENT_BALANCE';
    END IF;
    IF p_dr_cr = 'debit' AND v_type = 'overdraft' AND v_new_bal < -COALESCE(v_od,0) THEN
      RAISE EXCEPTION 'LIMIT_EXCEEDED';
    END IF;

    INSERT INTO core.fact_deposit_txn(txn_id, account_id, txn_datetime, txn_date, amount, dr_cr,
        running_balance, channel, narration, idempotency_key)
      VALUES (v_txn_id, p_account_id, p_txn_datetime, p_txn_datetime::DATE, p_amount, p_dr_cr,
        v_new_bal, p_channel, p_narration, p_idempotency_key);

    UPDATE core.deposit_account SET current_balance = v_new_bal WHERE account_id = p_account_id;
  EXCEPTION WHEN OTHERS THEN
    v_reason := SQLERRM;
    IF NOT (v_reason = ANY(v_known_codes)) THEN RAISE; END IF;
    v_rejected := TRUE;
  END;

  IF v_rejected THEN
    INSERT INTO core.txn_rejection_log(rejection_id, source_table, attempted_key, account_or_folio_id,
        amount, reason_code, rejected_at, idempotency_key)
      VALUES ('REJ_'||gen_random_uuid()::TEXT, 'fact_deposit_txn', v_txn_id, p_account_id, p_amount,
        v_reason, p_txn_datetime, p_idempotency_key);
    RETURN NULL;
  END IF;

  PERFORM 1 FROM core.deposit_account WHERE account_id = p_account_id AND current_balance = v_new_bal;
  IF NOT FOUND THEN RAISE EXCEPTION 'post-verify failed for %', p_account_id; END IF;

  RETURN v_txn_id;
END; $$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 2. core.post_card_txn
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.post_card_txn(
  p_card_id           TEXT,
  p_amount            DECIMAL(18,2),
  p_txn_type          TEXT,
  p_merchant_name     TEXT,
  p_mcc               TEXT,
  p_merchant_category TEXT,
  p_merchant_city     TEXT,
  p_entry_mode        TEXT,
  p_interchange_fee   DECIMAL(18,2),
  p_reward_points     INT,
  p_txn_datetime      TIMESTAMPTZ,
  p_idempotency_key   TEXT
) RETURNS TEXT AS $$
DECLARE
  v_stmt_bal DECIMAL(18,2); v_limit DECIMAL(18,2); v_status TEXT;
  v_new_bal DECIMAL(18,2); v_card_txn_id TEXT; v_existing TEXT;
  v_reason TEXT; v_rejected BOOLEAN := FALSE; v_final_status TEXT;
  v_known_codes TEXT[] := ARRAY['LIMIT_EXCEEDED','ACCOUNT_CLOSED','FK_NOT_FOUND'];
BEGIN
  SELECT card_txn_id INTO v_existing FROM core.fact_card_txn WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN RETURN v_existing; END IF;

  v_card_txn_id := 'CTXN_' || gen_random_uuid()::TEXT;

  BEGIN
    SELECT current_statement_balance, credit_limit, status INTO v_stmt_bal, v_limit, v_status
      FROM core.card_master WHERE card_id = p_card_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'FK_NOT_FOUND'; END IF;
    IF v_status <> 'active' THEN RAISE EXCEPTION 'ACCOUNT_CLOSED'; END IF;

    v_new_bal := CASE WHEN p_txn_type IN ('purchase','atm') THEN v_stmt_bal + p_amount
                       ELSE v_stmt_bal - p_amount END;

    IF p_txn_type IN ('purchase','atm') AND v_new_bal > COALESCE(v_limit,0) THEN
      RAISE EXCEPTION 'LIMIT_EXCEEDED';
    END IF;

    UPDATE core.card_master SET current_statement_balance = v_new_bal WHERE card_id = p_card_id;
  EXCEPTION WHEN OTHERS THEN
    v_reason := SQLERRM;
    IF NOT (v_reason = ANY(v_known_codes)) THEN RAISE; END IF;
    v_rejected := TRUE;
  END;

  v_final_status := CASE WHEN v_rejected THEN 'declined' ELSE 'approved' END;

  -- §1.1: the declined row is still inserted, unconditionally, outside the
  -- balance-mutating block above (which has already rolled back on rejection).
  INSERT INTO core.fact_card_txn(card_txn_id, card_id, merchant_name, mcc, merchant_category,
      merchant_city, txn_datetime, txn_date, amount, txn_type, entry_mode, status,
      interchange_fee, reward_points, dispute_flag, idempotency_key)
    VALUES (v_card_txn_id, p_card_id, p_merchant_name, p_mcc, p_merchant_category, p_merchant_city,
      p_txn_datetime, p_txn_datetime::DATE, p_amount, p_txn_type, p_entry_mode, v_final_status,
      CASE WHEN v_rejected THEN NULL ELSE p_interchange_fee END,
      CASE WHEN v_rejected THEN NULL ELSE p_reward_points END,
      FALSE, p_idempotency_key);

  IF v_rejected THEN
    INSERT INTO core.txn_rejection_log(rejection_id, source_table, attempted_key, account_or_folio_id,
        amount, reason_code, rejected_at, idempotency_key)
      VALUES ('REJ_'||gen_random_uuid()::TEXT, 'fact_card_txn', v_card_txn_id, p_card_id, p_amount,
        v_reason, p_txn_datetime, p_idempotency_key);
    RETURN v_card_txn_id;
  END IF;

  PERFORM 1 FROM core.card_master WHERE card_id = p_card_id AND current_statement_balance = v_new_bal;
  IF NOT FOUND THEN RAISE EXCEPTION 'post-verify failed for %', p_card_id; END IF;

  RETURN v_card_txn_id;
END; $$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 3. core.post_payment
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.post_payment(
  p_from_account_id             TEXT,
  p_amount                      DECIMAL(18,2),
  p_rail                        TEXT,
  p_beneficiary_name            TEXT,
  p_beneficiary_account_or_vpa  TEXT,
  p_beneficiary_type            TEXT,
  p_payer_vpa                   TEXT,
  p_payee_vpa                   TEXT,
  p_biller_category             TEXT,
  p_payment_datetime            TIMESTAMPTZ,
  p_idempotency_key             TEXT
) RETURNS TEXT AS $$
DECLARE
  v_acct_type TEXT; v_acct_status TEXT;
  v_bal DECIMAL(18,2); v_min DECIMAL(18,2); v_od DECIMAL(18,2); v_dep_type TEXT;
  v_stmt_bal DECIMAL(18,2); v_limit DECIMAL(18,2);
  v_available DECIMAL(18,2);
  v_payment_id TEXT; v_existing TEXT;
  v_reason TEXT; v_rejected BOOLEAN := FALSE;
  v_known_codes TEXT[] := ARRAY['INSUFFICIENT_BALANCE','LIMIT_EXCEEDED','ACCOUNT_CLOSED','FK_NOT_FOUND'];
BEGIN
  SELECT payment_id INTO v_existing FROM core.fact_payment WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN RETURN v_existing; END IF;

  v_payment_id := 'PAY_' || gen_random_uuid()::TEXT;

  BEGIN
    SELECT account_type, status INTO v_acct_type, v_acct_status
      FROM core.dim_account WHERE account_id = p_from_account_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'FK_NOT_FOUND'; END IF;
    IF v_acct_status <> 'active' THEN RAISE EXCEPTION 'ACCOUNT_CLOSED'; END IF;

    IF v_acct_type = 'deposit' THEN
      SELECT current_balance, min_balance, overdraft_limit, deposit_type INTO v_bal, v_min, v_od, v_dep_type
        FROM core.deposit_account WHERE account_id = p_from_account_id FOR UPDATE;
      IF v_dep_type = 'overdraft' THEN
        v_available := v_bal + COALESCE(v_od,0);
      ELSE
        v_available := v_bal - v_min;
      END IF;
      IF p_amount > v_available THEN RAISE EXCEPTION 'INSUFFICIENT_BALANCE'; END IF;
      UPDATE core.deposit_account SET current_balance = current_balance - p_amount
        WHERE account_id = p_from_account_id;

    ELSIF v_acct_type = 'card' THEN
      SELECT current_statement_balance, credit_limit INTO v_stmt_bal, v_limit
        FROM core.card_master WHERE account_id = p_from_account_id FOR UPDATE;
      v_available := COALESCE(v_limit,0) - v_stmt_bal;
      IF p_amount > v_available THEN RAISE EXCEPTION 'LIMIT_EXCEEDED'; END IF;
      UPDATE core.card_master SET current_statement_balance = current_statement_balance + p_amount
        WHERE account_id = p_from_account_id;

    ELSE
      RAISE EXCEPTION 'ACCOUNT_CLOSED';  -- loan accounts aren't a valid payment source
    END IF;

    INSERT INTO core.fact_payment(payment_id, from_account_id, beneficiary_name, beneficiary_account_or_vpa,
        beneficiary_type, rail, amount, payment_datetime, payment_date, status, reference_no,
        payer_vpa, payee_vpa, biller_category, idempotency_key)
      VALUES (v_payment_id, p_from_account_id, p_beneficiary_name, p_beneficiary_account_or_vpa,
        p_beneficiary_type, p_rail, p_amount, p_payment_datetime, p_payment_datetime::DATE, 'settled',
        NULL, p_payer_vpa, p_payee_vpa, p_biller_category, p_idempotency_key);
  EXCEPTION WHEN OTHERS THEN
    v_reason := SQLERRM;
    IF NOT (v_reason = ANY(v_known_codes)) THEN RAISE; END IF;
    v_rejected := TRUE;
  END;

  IF v_rejected THEN
    INSERT INTO core.txn_rejection_log(rejection_id, source_table, attempted_key, account_or_folio_id,
        amount, reason_code, rejected_at, idempotency_key)
      VALUES ('REJ_'||gen_random_uuid()::TEXT, 'fact_payment', v_payment_id, p_from_account_id, p_amount,
        v_reason, p_payment_datetime, p_idempotency_key);
    RETURN NULL;
  END IF;

  RETURN v_payment_id;
END; $$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 4. core.post_loan_txn
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.post_loan_txn(
  p_loan_id         TEXT,
  p_amount          DECIMAL(18,2),
  p_txn_type        TEXT,
  p_principal_paid  DECIMAL(18,2),
  p_interest_paid   DECIMAL(18,2),
  p_txn_date        DATE,
  p_idempotency_key TEXT
) RETURNS TEXT AS $$
DECLARE
  v_outstanding DECIMAL(18,2); v_status TEXT; v_new_outstanding DECIMAL(18,2);
  v_loan_txn_id TEXT; v_existing TEXT;
  v_reason TEXT; v_rejected BOOLEAN := FALSE;
  v_known_codes TEXT[] := ARRAY['INSUFFICIENT_BALANCE','LIMIT_EXCEEDED','ACCOUNT_CLOSED','FK_NOT_FOUND'];
BEGIN
  SELECT loan_txn_id INTO v_existing FROM core.fact_loan_txn WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN RETURN v_existing; END IF;

  v_loan_txn_id := 'LTXN_' || gen_random_uuid()::TEXT;

  BEGIN
    SELECT outstanding_principal, status INTO v_outstanding, v_status
      FROM core.loan_account WHERE loan_id = p_loan_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'FK_NOT_FOUND'; END IF;
    IF v_status IN ('closed','written_off') THEN RAISE EXCEPTION 'ACCOUNT_CLOSED'; END IF;

    IF p_txn_type = 'prepayment' AND p_amount > v_outstanding THEN
      RAISE EXCEPTION 'INSUFFICIENT_BALANCE';
    END IF;

    v_new_outstanding := v_outstanding - p_principal_paid;
    IF v_new_outstanding < 0 THEN RAISE EXCEPTION 'LIMIT_EXCEEDED'; END IF;

    INSERT INTO core.fact_loan_txn(loan_txn_id, loan_id, txn_date, amount, txn_type,
        principal_paid, interest_paid, idempotency_key)
      VALUES (v_loan_txn_id, p_loan_id, p_txn_date, p_amount, p_txn_type,
        p_principal_paid, p_interest_paid, p_idempotency_key);

    UPDATE core.loan_account SET outstanding_principal = v_new_outstanding WHERE loan_id = p_loan_id;
  EXCEPTION WHEN OTHERS THEN
    v_reason := SQLERRM;
    IF NOT (v_reason = ANY(v_known_codes)) THEN RAISE; END IF;
    v_rejected := TRUE;
  END;

  IF v_rejected THEN
    INSERT INTO core.txn_rejection_log(rejection_id, source_table, attempted_key, account_or_folio_id,
        amount, reason_code, rejected_at, idempotency_key)
      VALUES ('REJ_'||gen_random_uuid()::TEXT, 'fact_loan_txn', v_loan_txn_id, p_loan_id, p_amount,
        v_reason, p_txn_date::TIMESTAMPTZ, p_idempotency_key);
    RETURN NULL;
  END IF;

  PERFORM 1 FROM core.loan_account WHERE loan_id = p_loan_id AND outstanding_principal = v_new_outstanding;
  IF NOT FOUND THEN RAISE EXCEPTION 'post-verify failed for %', p_loan_id; END IF;

  RETURN v_loan_txn_id;
END; $$ LANGUAGE plpgsql;

-- -----------------------------------------------------------------------------
-- 5. core.post_mf_transaction
-- -----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION core.post_mf_transaction(
  p_folio_id        TEXT,
  p_scheme_code     TEXT,
  p_txn_type        TEXT,
  p_txn_date        DATE,
  p_nav_date        DATE,
  p_amount          DECIMAL(18,2),
  p_is_sip          BOOLEAN,
  p_idempotency_key TEXT
) RETURNS TEXT AS $$
DECLARE
  v_nav_value DECIMAL(14,5);
  v_folio_status TEXT;
  v_units_held DECIMAL(18,5); v_invested DECIMAL(18,2);
  v_units DECIMAL(18,5); v_new_units DECIMAL(18,5); v_new_invested DECIMAL(18,2);
  v_market_value DECIMAL(18,2); v_unrealised DECIMAL(18,2);
  v_mf_txn_id TEXT; v_existing TEXT;
  v_reason TEXT; v_rejected BOOLEAN := FALSE;
  v_known_codes TEXT[] := ARRAY['NAV_NOT_FOUND','UNITS_EXCEEDED','ACCOUNT_CLOSED','FK_NOT_FOUND'];
BEGIN
  SELECT mf_txn_id INTO v_existing FROM core.fact_mf_transaction WHERE idempotency_key = p_idempotency_key;
  IF FOUND THEN RETURN v_existing; END IF;

  v_mf_txn_id := 'MFTXN_' || gen_random_uuid()::TEXT;

  BEGIN
    SELECT status INTO v_folio_status FROM core.mf_folio WHERE folio_id = p_folio_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'FK_NOT_FOUND'; END IF;
    IF v_folio_status <> 'active' THEN RAISE EXCEPTION 'ACCOUNT_CLOSED'; END IF;

    SELECT nav_value INTO v_nav_value FROM core.mf_nav_ref
      WHERE scheme_code = p_scheme_code AND nav_date = p_nav_date;
    IF NOT FOUND THEN RAISE EXCEPTION 'NAV_NOT_FOUND'; END IF;

    SELECT units_held, invested_amount INTO v_units_held, v_invested
      FROM core.mf_holding_current WHERE folio_id = p_folio_id AND scheme_code = p_scheme_code FOR UPDATE;
    IF NOT FOUND THEN
      v_units_held := 0; v_invested := 0;
    END IF;

    v_units := ROUND(p_amount / v_nav_value, 5);

    IF p_txn_type IN ('redemption','switch_out') THEN
      IF v_units > v_units_held THEN RAISE EXCEPTION 'UNITS_EXCEEDED'; END IF;
      v_new_units := v_units_held - v_units;
      v_new_invested := GREATEST(v_invested - p_amount, 0);
    ELSE
      v_new_units := v_units_held + v_units;
      v_new_invested := v_invested + p_amount;
    END IF;

    v_market_value := ROUND(v_new_units * v_nav_value, 2);
    v_unrealised := v_market_value - v_new_invested;

    INSERT INTO core.fact_mf_transaction(mf_txn_id, folio_id, scheme_code, txn_type, txn_date,
        nav_date, nav_value, amount, units, is_sip, idempotency_key)
      VALUES (v_mf_txn_id, p_folio_id, p_scheme_code, p_txn_type, p_txn_date,
        p_nav_date, v_nav_value, p_amount, v_units, p_is_sip, p_idempotency_key);

    INSERT INTO core.mf_holding_current(folio_id, scheme_code, as_of_date, units_held,
        invested_amount, nav_value, market_value, unrealised_gain)
      VALUES (p_folio_id, p_scheme_code, p_txn_date, v_new_units, v_new_invested, v_nav_value,
        v_market_value, v_unrealised)
    ON CONFLICT (folio_id, scheme_code) DO UPDATE SET
      as_of_date = EXCLUDED.as_of_date, units_held = EXCLUDED.units_held,
      invested_amount = EXCLUDED.invested_amount, nav_value = EXCLUDED.nav_value,
      market_value = EXCLUDED.market_value, unrealised_gain = EXCLUDED.unrealised_gain;
  EXCEPTION WHEN OTHERS THEN
    v_reason := SQLERRM;
    IF NOT (v_reason = ANY(v_known_codes)) THEN RAISE; END IF;
    v_rejected := TRUE;
  END;

  IF v_rejected THEN
    INSERT INTO core.txn_rejection_log(rejection_id, source_table, attempted_key, account_or_folio_id,
        amount, reason_code, rejected_at, idempotency_key)
      VALUES ('REJ_'||gen_random_uuid()::TEXT, 'fact_mf_transaction', v_mf_txn_id, p_folio_id, p_amount,
        v_reason, p_txn_date::TIMESTAMPTZ, p_idempotency_key);
    RETURN NULL;
  END IF;

  PERFORM 1 FROM core.mf_holding_current WHERE folio_id = p_folio_id AND scheme_code = p_scheme_code
    AND units_held = v_new_units;
  IF NOT FOUND THEN RAISE EXCEPTION 'post-verify failed for %/%', p_folio_id, p_scheme_code; END IF;

  RETURN v_mf_txn_id;
END; $$ LANGUAGE plpgsql;

-- =============================================================================
-- Exit criterion (Phase 4): see implemented/phase4-transaction-logic.md for the
-- unit-test run covering (a) valid commit, (b) rejection + correct reason_code,
-- (c) concurrent-call serialization, (d) idempotent replay.
-- =============================================================================
