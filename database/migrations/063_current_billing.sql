-- Current billing is independent of historical test grants and deferred 059-061.
-- Only service-role RPCs may mutate these tables. Runtime activation is opt-in.
BEGIN;
CREATE TABLE redirx_billing_checkouts (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 version text NOT NULL DEFAULT 'redirx_2026_10' CHECK(version='redirx_2026_10'),
 sku text NOT NULL CHECK(sku IN('payg','studio','agency')), migration_id uuid REFERENCES migration_records(id) ON DELETE CASCADE,
 old_inventory_id uuid REFERENCES inventory_snapshots(id) ON DELETE CASCADE, new_inventory_id uuid REFERENCES inventory_snapshots(id) ON DELETE CASCADE,
 quantity integer NOT NULL CHECK(quantity>0), amount_cents integer NOT NULL CHECK(amount_cents>0),
 stripe_price_id text NOT NULL, livemode boolean NOT NULL, mode text GENERATED ALWAYS AS (CASE WHEN livemode THEN 'live' ELSE 'test' END) STORED,
 status text NOT NULL DEFAULT 'reserved' CHECK(status IN('reserved','open','paid','expired','revoked')),
 idempotency_key text NOT NULL, request_hash text NOT NULL, recurring_consent boolean NOT NULL,
 stripe_session_id text UNIQUE, checkout_url text, stripe_customer_id text, stripe_subscription_id text UNIQUE,
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '23 hours',
 paid_at timestamptz, revoke_reason text CHECK(revoke_reason IN('revoked','payment_failed','canceled','refunded','disputed')),
 UNIQUE(user_id,livemode,idempotency_key),
 CHECK((sku='payg' AND quantity>=500 AND amount_cents=ceil(quantity::numeric/50)::integer*100) OR (sku='studio' AND quantity=1 AND amount_cents=7900) OR (sku='agency' AND quantity=1 AND amount_cents=19900)),
 CHECK((sku='payg' AND migration_id IS NOT NULL AND old_inventory_id IS NOT NULL AND new_inventory_id IS NOT NULL)
    OR (sku<>'payg' AND migration_id IS NULL AND old_inventory_id IS NULL AND new_inventory_id IS NULL AND recurring_consent))
);
CREATE TABLE redirx_billing_periods (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), checkout_id uuid NOT NULL REFERENCES redirx_billing_checkouts(id) ON DELETE CASCADE,
 user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE, stripe_subscription_id text NOT NULL, stripe_customer_id text NOT NULL,
 stripe_price_id text NOT NULL, sku text NOT NULL CHECK(sku IN('studio','agency')), livemode boolean NOT NULL,
 stripe_invoice_id text NOT NULL UNIQUE, period_start timestamptz NOT NULL, period_end timestamptz NOT NULL,
 amount_cents integer NOT NULL, included_old_urls integer NOT NULL CHECK(included_old_urls IN(5000,15000)),
 used_old_urls integer NOT NULL DEFAULT 0 CHECK(used_old_urls>=0), extra_old_urls integer NOT NULL DEFAULT 0 CHECK(extra_old_urls>=0),
 extra_amount_cents integer NOT NULL DEFAULT 0 CHECK(extra_amount_cents>=0),
 status text NOT NULL DEFAULT 'active' CHECK(status IN('active','suspended','revoked')),
 closed_at timestamptz, usage_invoice_id text UNIQUE, stripe_invoice_item_id text UNIQUE,
 usage_settlement text CHECK(usage_settlement IN('cycle','terminal')),
 CHECK((usage_settlement IS NULL)=(closed_at IS NULL)),
 UNIQUE(stripe_subscription_id,period_start), CHECK(period_end>period_start),
 CHECK(extra_old_urls=greatest(0,used_old_urls-included_old_urls)), CHECK(extra_amount_cents=ceil(extra_old_urls::numeric/50)::integer*100),
 CHECK((sku='studio' AND included_old_urls=5000 AND amount_cents=7900) OR (sku='agency' AND included_old_urls=15000 AND amount_cents=19900))
);
CREATE TABLE redirx_billing_events (
 event_id text PRIMARY KEY CHECK(length(event_id)>0), kind text NOT NULL, binding jsonb NOT NULL,
 checkout_id uuid NOT NULL REFERENCES redirx_billing_checkouts(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX redirx_terminal_event_checkout ON redirx_billing_events ((binding->>'checkout_id')) WHERE kind='terminal_usage';
-- The trial is account-owned; deleting a migration must not replenish it.
CREATE TABLE redirx_billing_trials (
 user_id uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE redirx_billing_usage (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(), user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
 migration_id uuid NOT NULL REFERENCES migration_records(id) ON DELETE CASCADE, old_inventory_id uuid NOT NULL REFERENCES inventory_snapshots(id) ON DELETE CASCADE,
 new_inventory_id uuid NOT NULL REFERENCES inventory_snapshots(id) ON DELETE CASCADE, checkout_id uuid REFERENCES redirx_billing_checkouts(id) ON DELETE CASCADE,
 period_id uuid REFERENCES redirx_billing_periods(id) ON DELETE CASCADE, run_id uuid NOT NULL UNIQUE,
 sku text NOT NULL CHECK(sku IN('trial','payg','studio','agency')), livemode boolean NOT NULL,
 old_urls integer NOT NULL CHECK(old_urls>0), extra_urls integer NOT NULL DEFAULT 0 CHECK(extra_urls>=0),
 approve_extra_urls boolean NOT NULL DEFAULT false, max_extra_amount_cents integer NOT NULL DEFAULT 0 CHECK(max_extra_amount_cents>=0),
 created_at timestamptz NOT NULL DEFAULT now(), expires_at timestamptz NOT NULL DEFAULT now()+interval '30 days',
 CHECK((sku='trial' AND checkout_id IS NULL AND period_id IS NULL) OR (sku='payg' AND checkout_id IS NOT NULL AND period_id IS NULL)
  OR (sku IN('studio','agency') AND checkout_id IS NOT NULL AND period_id IS NOT NULL))
);
CREATE UNIQUE INDEX redirx_one_trial ON redirx_billing_usage(user_id) WHERE sku='trial';
CREATE UNIQUE INDEX redirx_one_payg_run ON redirx_billing_usage(checkout_id) WHERE sku='payg';

CREATE FUNCTION current_billing_event(p_event_id text,p_kind text,p_binding jsonb) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
BEGIN
 IF p_event_id IS NULL OR length(p_event_id) NOT BETWEEN 1 AND 255 THEN RAISE EXCEPTION 'invalid_input'; END IF;
 INSERT INTO redirx_billing_events(event_id,kind,binding,checkout_id) VALUES(p_event_id,p_kind,p_binding,
  (CASE WHEN jsonb_typeof(p_binding)='array' THEN p_binding->>0 ELSE p_binding->>'checkout_id' END)::uuid) ON CONFLICT DO NOTHING;
 IF NOT EXISTS(SELECT 1 FROM redirx_billing_events WHERE event_id=p_event_id AND kind=p_kind AND binding=p_binding) THEN
  RAISE EXCEPTION 'operation_conflict'; END IF;
END $$;

CREATE FUNCTION current_inventory_counts(p_user_id uuid,p_migration_id uuid,p_old_inventory_id uuid,p_new_inventory_id uuid)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE o integer; n integer; b bigint;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM migration_records WHERE id=p_migration_id AND user_id=p_user_id) THEN RAISE EXCEPTION 'not_found'; END IF;
 PERFORM 1 FROM inventory_snapshots WHERE id=p_old_inventory_id AND migration_id=p_migration_id AND user_id=p_user_id AND side='old' AND status='complete' FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'inventory_incomplete'; END IF;
 PERFORM 1 FROM inventory_snapshots WHERE id=p_new_inventory_id AND migration_id=p_migration_id AND user_id=p_user_id AND side='new' AND status='complete' FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'inventory_incomplete'; END IF;
 SELECT count(*),coalesce(sum(octet_length(url)),0) INTO o,b FROM session_discovered_urls WHERE inventory_id=p_old_inventory_id;
 SELECT count(*),b+coalesce(sum(octet_length(url)),0) INTO n,b FROM session_discovered_urls WHERE inventory_id=p_new_inventory_id;
 IF o NOT BETWEEN 1 AND 15000 OR n NOT BETWEEN 1 AND 20000 OR b>16777216 THEN RAISE EXCEPTION 'capacity_exceeded'; END IF;
 RETURN jsonb_build_object('old',o,'new',n,'bytes',b);
END $$;

CREATE FUNCTION reserve_current_checkout(p_user_id uuid,p_sku text,p_migration_id uuid,p_old_inventory_id uuid,p_new_inventory_id uuid,
 p_key text,p_recurring_consent boolean,p_price_id text,p_livemode boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE; h text; counts jsonb; qty integer; amount integer;
BEGIN
 IF p_user_id IS NULL OR p_sku IS NULL OR p_sku NOT IN('payg','studio','agency') OR p_key IS NULL OR length(btrim(p_key)) NOT BETWEEN 1 AND 200
  OR p_price_id IS NULL OR p_price_id !~ '^price_' OR p_livemode IS NULL OR p_recurring_consent IS NULL THEN RAISE EXCEPTION 'invalid_input'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'not_ready'; END IF;
 PERFORM pg_advisory_xact_lock(630001,hashtext(p_user_id::text));
 h:=md5(jsonb_build_array(p_sku,p_migration_id,p_old_inventory_id,p_new_inventory_id,p_recurring_consent,p_price_id,p_livemode)::text);
 SELECT * INTO c FROM redirx_billing_checkouts WHERE user_id=p_user_id AND livemode=p_livemode AND idempotency_key=p_key FOR UPDATE;
 IF FOUND THEN
  IF c.request_hash<>h THEN RAISE EXCEPTION 'operation_conflict'; END IF;
  IF c.status IN('reserved','open') AND c.expires_at<=clock_timestamp() THEN
   UPDATE redirx_billing_checkouts SET status='expired' WHERE id=c.id RETURNING * INTO c;
  END IF;
  RETURN to_jsonb(c);
 END IF;
 UPDATE redirx_billing_checkouts SET status='expired' WHERE user_id=p_user_id AND livemode=p_livemode AND status IN('reserved','open') AND expires_at<=clock_timestamp();
 IF p_sku='payg' THEN
  counts:=current_inventory_counts(p_user_id,p_migration_id,p_old_inventory_id,p_new_inventory_id);
  qty:=greatest(500,(counts->>'old')::integer); amount:=greatest(1000,ceil((counts->>'old')::numeric/50)::integer*100);
  IF EXISTS(SELECT 1 FROM redirx_billing_checkouts WHERE user_id=p_user_id AND livemode=p_livemode AND sku='payg'
    AND old_inventory_id=p_old_inventory_id AND new_inventory_id=p_new_inventory_id AND status IN('reserved','open','paid')) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 ELSE
  IF NOT p_recurring_consent OR p_migration_id IS NOT NULL OR p_old_inventory_id IS NOT NULL OR p_new_inventory_id IS NOT NULL THEN RAISE EXCEPTION 'invalid_input'; END IF;
  IF EXISTS(SELECT 1 FROM redirx_billing_checkouts WHERE user_id=p_user_id AND livemode=p_livemode AND sku<>'payg'
    AND (status IN('reserved','open','paid') OR (status='revoked' AND revoke_reason='payment_failed'))) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
  qty:=1; amount:=CASE p_sku WHEN 'studio' THEN 7900 ELSE 19900 END;
 END IF;
 INSERT INTO redirx_billing_checkouts(user_id,sku,migration_id,old_inventory_id,new_inventory_id,quantity,amount_cents,stripe_price_id,livemode,idempotency_key,request_hash,recurring_consent)
 VALUES(p_user_id,p_sku,p_migration_id,p_old_inventory_id,p_new_inventory_id,qty,amount,p_price_id,p_livemode,p_key,h,p_recurring_consent) RETURNING * INTO c;
 RETURN to_jsonb(c);
END $$;
CREATE FUNCTION attach_current_checkout(p_checkout_id uuid,p_session_id text,p_url text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE;
BEGIN
 SELECT * INTO c FROM redirx_billing_checkouts WHERE id=p_checkout_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 IF p_session_id IS NULL OR p_session_id !~ '^cs_' OR p_url IS NULL OR p_url !~ '^https://checkout[.]stripe[.]com/' THEN RAISE EXCEPTION 'invalid_input'; END IF;
 IF c.stripe_session_id IS NOT NULL THEN
  IF c.stripe_session_id<>p_session_id OR (c.checkout_url IS NOT NULL AND c.checkout_url<>p_url) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
  UPDATE redirx_billing_checkouts SET checkout_url=coalesce(checkout_url,p_url) WHERE id=c.id RETURNING * INTO c;
  RETURN to_jsonb(c);
 END IF;
 IF c.status<>'reserved' OR c.expires_at<=clock_timestamp() THEN RAISE EXCEPTION 'checkout_expired'; END IF;
 UPDATE redirx_billing_checkouts SET stripe_session_id=p_session_id,checkout_url=p_url,status='open' WHERE id=c.id RETURNING * INTO c;
 RETURN to_jsonb(c);
END $$;
CREATE FUNCTION get_current_checkout(p_user_id uuid,p_checkout_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE;
BEGIN
 SELECT * INTO c FROM redirx_billing_checkouts WHERE id=p_checkout_id AND user_id=p_user_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF; RETURN to_jsonb(c);
END $$;
CREATE FUNCTION apply_current_checkout_payment(p_checkout_id uuid,p_session_id text,p_customer_id text,p_subscription_id text,p_amount_cents integer,p_currency text,p_event_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(630001,hashtext(user_id::text)) FROM redirx_billing_checkouts WHERE id=p_checkout_id;
 SELECT * INTO c FROM redirx_billing_checkouts WHERE id=p_checkout_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 IF (c.stripe_session_id IS NOT NULL AND c.stripe_session_id<>p_session_id) OR p_session_id IS NULL OR p_session_id !~ '^cs_' OR p_customer_id IS NULL OR p_customer_id !~ '^cus_'
 OR p_currency IS DISTINCT FROM 'usd' OR p_amount_cents IS DISTINCT FROM c.amount_cents
 OR (c.sku='payg' AND p_subscription_id IS NOT NULL) OR (c.sku<>'payg' AND (p_subscription_id IS NULL OR p_subscription_id !~ '^sub_'))
 OR (c.stripe_customer_id IS NOT NULL AND c.stripe_customer_id<>p_customer_id)
 OR (c.stripe_subscription_id IS NOT NULL AND c.stripe_subscription_id IS DISTINCT FROM p_subscription_id) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 PERFORM current_billing_event(p_event_id,'checkout_paid',jsonb_build_array(p_checkout_id,p_session_id,p_customer_id,p_subscription_id,p_amount_cents,p_currency));
 IF c.status='revoked' THEN RETURN to_jsonb(c); END IF;
 IF c.sku<>'payg' AND EXISTS(SELECT 1 FROM redirx_billing_checkouts other WHERE other.user_id=c.user_id AND other.livemode=c.livemode
   AND other.id<>c.id AND other.sku<>'payg' AND (other.status='paid' OR (other.status IN('reserved','open') AND other.expires_at>clock_timestamp()))) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 IF c.status NOT IN('reserved','open','paid','expired') THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 UPDATE redirx_billing_checkouts SET stripe_session_id=p_session_id,status='paid',paid_at=coalesce(paid_at,now()),stripe_customer_id=p_customer_id,stripe_subscription_id=p_subscription_id WHERE id=c.id RETURNING * INTO c;
 RETURN to_jsonb(c);
END $$;
CREATE FUNCTION record_current_subscription_period(p_checkout_id uuid,p_subscription_id text,p_customer_id text,p_invoice_id text,p_period_start timestamptz,p_period_end timestamptz,p_amount_cents integer,p_event_id text)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE; b redirx_billing_periods%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(630001,hashtext(user_id::text)) FROM redirx_billing_checkouts WHERE id=p_checkout_id;
 SELECT * INTO c FROM redirx_billing_checkouts WHERE id=p_checkout_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 IF c.sku='payg' OR c.stripe_subscription_id IS DISTINCT FROM p_subscription_id OR p_subscription_id IS NULL
 OR c.stripe_customer_id IS DISTINCT FROM p_customer_id OR p_invoice_id IS NULL OR p_invoice_id !~ '^in_'
 OR p_period_start IS NULL OR p_period_end IS NULL OR p_period_end<=p_period_start OR p_amount_cents IS DISTINCT FROM c.amount_cents THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 PERFORM current_billing_event(p_event_id,'invoice_paid',jsonb_build_array(p_checkout_id,p_subscription_id,p_customer_id,p_invoice_id,p_period_start,p_period_end,p_amount_cents));
 SELECT * INTO b FROM redirx_billing_periods WHERE stripe_invoice_id=p_invoice_id;
 IF FOUND THEN
  IF b.checkout_id<>c.id OR b.period_start<>p_period_start OR b.period_end<>p_period_end OR b.amount_cents<>p_amount_cents THEN RAISE EXCEPTION 'operation_conflict'; END IF;
  RETURN to_jsonb(b); -- A suspended/terminal invoice is never regranted by replay.
 END IF;
 IF c.revoke_reason IN('revoked','refunded','disputed') OR c.status NOT IN('paid','revoked') THEN RAISE EXCEPTION 'payment_required'; END IF;
 IF EXISTS(SELECT 1 FROM redirx_billing_periods WHERE stripe_subscription_id=p_subscription_id
   AND tstzrange(period_start,period_end,'[)') && tstzrange(p_period_start,p_period_end,'[)')) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 IF c.sku<>'payg' AND EXISTS(SELECT 1 FROM redirx_billing_checkouts other WHERE other.user_id=c.user_id AND other.livemode=c.livemode
   AND other.id<>c.id AND other.sku<>'payg' AND (other.status='paid' OR (other.status IN('reserved','open') AND other.expires_at>clock_timestamp()))) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 INSERT INTO redirx_billing_periods(checkout_id,user_id,stripe_subscription_id,stripe_customer_id,stripe_price_id,sku,livemode,stripe_invoice_id,period_start,period_end,amount_cents,included_old_urls)
 VALUES(c.id,c.user_id,p_subscription_id,p_customer_id,c.stripe_price_id,c.sku,c.livemode,p_invoice_id,p_period_start,p_period_end,p_amount_cents,CASE c.sku WHEN 'studio' THEN 5000 ELSE 15000 END) RETURNING * INTO b;
 -- A verified terminal cancellation is final for this Stripe subscription.
 -- Late paid invoices may be recorded for audit, but cannot reopen admission.
 IF NOT EXISTS(SELECT 1 FROM redirx_billing_events WHERE kind='terminal_usage' AND binding->>'checkout_id'=c.id::text) THEN
  UPDATE redirx_billing_checkouts SET status='paid',revoke_reason=NULL WHERE id=c.id;
 END IF;
 RETURN to_jsonb(b);
END $$;
CREATE FUNCTION revoke_current_billing(p_checkout_id uuid,p_subscription_id text,p_event_id text,p_reason text DEFAULT 'revoked',p_invoice_id text DEFAULT NULL) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE; replay boolean;
BEGIN
 IF p_reason IS NULL OR p_reason NOT IN('revoked','payment_failed','canceled','refunded','disputed') OR (p_checkout_id IS NULL AND p_subscription_id IS NULL) THEN RAISE EXCEPTION 'invalid_input'; END IF;
 SELECT * INTO c FROM redirx_billing_checkouts WHERE (p_checkout_id IS NULL OR id=p_checkout_id) AND (p_subscription_id IS NULL OR stripe_subscription_id=p_subscription_id) FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 replay:=EXISTS(SELECT 1 FROM redirx_billing_events WHERE event_id=p_event_id);
 PERFORM current_billing_event(p_event_id,'revoke',jsonb_build_array(c.id,p_subscription_id,p_reason,p_invoice_id));
 IF replay THEN RETURN to_jsonb(c); END IF;
 IF c.sku<>'payg' AND p_reason IN('refunded','disputed','revoked') THEN
  IF p_invoice_id IS NULL OR NOT EXISTS(SELECT 1 FROM redirx_billing_periods WHERE checkout_id=c.id AND stripe_invoice_id=p_invoice_id) THEN RAISE EXCEPTION 'not_found'; END IF;
  UPDATE redirx_billing_periods SET status='revoked' WHERE checkout_id=c.id AND stripe_invoice_id=p_invoice_id;
  RETURN to_jsonb(c);
 END IF;
 -- Ordinary lapse prevents new admissions, preserving previously purchased work.
 UPDATE redirx_billing_checkouts SET status='revoked',revoke_reason=CASE WHEN revoke_reason IN('revoked','refunded','disputed') THEN revoke_reason ELSE p_reason END WHERE id=c.id RETURNING * INTO c;
 RETURN to_jsonb(c);
END $$;
CREATE FUNCTION close_current_usage(p_subscription_id text,p_period_end timestamptz,p_invoice_id text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE b redirx_billing_periods%ROWTYPE;
BEGIN
 IF p_invoice_id IS NULL OR p_invoice_id !~ '^in_' THEN RAISE EXCEPTION 'invalid_input'; END IF;
 SELECT * INTO b FROM redirx_billing_periods WHERE stripe_subscription_id=p_subscription_id AND period_end=p_period_end FOR UPDATE;
 IF NOT FOUND THEN
  IF p_period_end IS NULL OR p_period_end>clock_timestamp() OR NOT EXISTS(SELECT 1 FROM redirx_billing_checkouts WHERE stripe_subscription_id=p_subscription_id) THEN RAISE EXCEPTION 'not_found'; END IF;
  RETURN jsonb_build_object('extra_old_urls',0,'extra_amount_cents',0,'missing_period',true);
 END IF;
 IF b.usage_settlement='terminal' THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 IF b.period_end>clock_timestamp() THEN RAISE EXCEPTION 'not_ready'; END IF;
 IF b.usage_invoice_id IS NOT NULL AND b.usage_invoice_id<>p_invoice_id THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 UPDATE redirx_billing_periods SET usage_settlement='cycle',closed_at=coalesce(closed_at,now()),usage_invoice_id=p_invoice_id WHERE id=b.id RETURNING * INTO b;
 RETURN to_jsonb(b);
END $$;
-- Caller has independently verified that this exact provider subscription is canceled.
-- Freeze first, then create a provider invoice using the immutable period UUID.
CREATE FUNCTION reserve_current_terminal_usage(p_subscription_id text,p_event_id text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE c redirx_billing_checkouts%ROWTYPE; b redirx_billing_periods%ROWTYPE; prior jsonb; binding jsonb;
BEGIN
 IF p_subscription_id IS NULL OR p_subscription_id !~ '^sub_' THEN RAISE EXCEPTION 'invalid_input'; END IF;
 IF current_setting('transaction_isolation')<>'read committed' THEN RAISE EXCEPTION 'not_ready'; END IF;
 PERFORM pg_advisory_xact_lock(630001,hashtext(user_id::text)) FROM redirx_billing_checkouts WHERE stripe_subscription_id=p_subscription_id;
 SELECT * INTO c FROM redirx_billing_checkouts WHERE stripe_subscription_id=p_subscription_id AND sku IN('studio','agency') FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 -- All cancellation deliveries, even different event IDs, retain the first
 -- selected period. An invoice arriving late must not move the settlement.
 SELECT e.binding INTO prior FROM redirx_billing_events e WHERE kind='terminal_usage'
  AND e.binding->>'checkout_id'=c.id::text ORDER BY created_at,event_id LIMIT 1;
 IF FOUND THEN
  SELECT * INTO b FROM redirx_billing_periods WHERE id=(prior->>'period_id')::uuid FOR UPDATE;
 ELSE
  SELECT * INTO b FROM redirx_billing_periods WHERE checkout_id=c.id ORDER BY period_start DESC,id DESC LIMIT 1 FOR UPDATE;
 END IF;
 binding:=jsonb_build_object('checkout_id',c.id,'subscription_id',p_subscription_id,'period_id',b.id);
 PERFORM current_billing_event(p_event_id,'terminal_usage',binding);
 UPDATE redirx_billing_checkouts SET status='revoked',revoke_reason='canceled' WHERE id=c.id;
 IF b.id IS NULL THEN
  RETURN jsonb_build_object('extra_old_urls',0,'extra_amount_cents',0,'missing_period',true,'terminal_settlement',false);
 END IF;
 IF b.usage_settlement IS NULL THEN
  UPDATE redirx_billing_periods SET usage_settlement='terminal',closed_at=clock_timestamp() WHERE id=b.id RETURNING * INTO b;
 END IF;
 -- A cycle closure that won the row lock already owns delivery; never create
 -- a second invoice. Its existing invoice may still need provider settlement.
 RETURN to_jsonb(b)||jsonb_build_object('terminal_settlement',b.usage_settlement='terminal');
END $$;
CREATE FUNCTION attach_current_terminal_usage_invoice(p_period_id uuid,p_invoice_id text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE b redirx_billing_periods%ROWTYPE;
BEGIN
 SELECT * INTO b FROM redirx_billing_periods WHERE id=p_period_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 IF b.usage_settlement IS DISTINCT FROM 'terminal' OR b.closed_at IS NULL OR b.extra_amount_cents=0
  OR p_invoice_id IS NULL OR p_invoice_id !~ '^in_'
  OR (b.usage_invoice_id IS NOT NULL AND b.usage_invoice_id<>p_invoice_id) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 UPDATE redirx_billing_periods SET usage_invoice_id=p_invoice_id WHERE id=b.id RETURNING * INTO b;
 RETURN to_jsonb(b)||jsonb_build_object('terminal_settlement',true);
END $$;
CREATE FUNCTION attach_current_usage_invoice_item(p_period_id uuid,p_invoice_id text,p_item_id text) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE b redirx_billing_periods%ROWTYPE;
BEGIN
 SELECT * INTO b FROM redirx_billing_periods WHERE id=p_period_id FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION 'not_found'; END IF;
 IF b.closed_at IS NULL OR b.usage_invoice_id IS DISTINCT FROM p_invoice_id OR p_invoice_id IS NULL OR p_item_id IS NULL OR p_item_id !~ '^ii_'
 OR b.extra_amount_cents=0 OR (b.stripe_invoice_item_id IS NOT NULL AND b.stripe_invoice_item_id<>p_item_id) THEN RAISE EXCEPTION 'operation_conflict'; END IF;
 UPDATE redirx_billing_periods SET stripe_invoice_item_id=p_item_id WHERE id=b.id RETURNING * INTO b; RETURN to_jsonb(b);
END $$;
CREATE FUNCTION get_current_billing_usage(p_user_id uuid,p_livemode boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE b redirx_billing_periods%ROWTYPE; c redirx_billing_checkouts%ROWTYPE;
BEGIN
 SELECT * INTO c FROM redirx_billing_checkouts WHERE user_id=p_user_id AND livemode=p_livemode AND sku<>'payg' ORDER BY created_at DESC LIMIT 1;
 SELECT * INTO b FROM redirx_billing_periods WHERE checkout_id=c.id AND period_start<=now() AND period_end>now() ORDER BY period_start DESC LIMIT 1;
 RETURN jsonb_build_object('subscription_id',c.id,'sku',coalesce(c.sku,'free'),'included_old_urls',coalesce(b.included_old_urls,0),'used_old_urls',coalesce(b.used_old_urls,0),
 'extra_old_urls',coalesce(b.extra_old_urls,0),'extra_amount_cents',coalesce(b.extra_amount_cents,0),'period_start',b.period_start,'period_end',b.period_end,
 'stripe_customer_id',c.stripe_customer_id,'stripe_subscription_id',c.stripe_subscription_id,'portal_available',c.stripe_customer_id IS NOT NULL,'subscription_status',CASE WHEN c.status='revoked' THEN c.revoke_reason ELSE coalesce(b.status,c.status,'none') END);
END $$;
CREATE FUNCTION current_billing_quote(p_user_id uuid,p_migration_id uuid,p_old_inventory_id uuid,p_new_inventory_id uuid) RETURNS jsonb
LANGUAGE plpgsql SECURITY DEFINER SET search_path=public,pg_temp AS $$
DECLARE counts jsonb; n integer;
BEGIN
 counts:=current_inventory_counts(p_user_id,p_migration_id,p_old_inventory_id,p_new_inventory_id); n:=(counts->>'old')::integer;
 RETURN jsonb_build_object('old_urls',n,'amount_cents',greatest(1000,ceil(n::numeric/50)::integer*100),'stripe_quantity',greatest(500,n));
END $$;
DO $$ DECLARE f record; t text; BEGIN
 FOREACH t IN ARRAY ARRAY['redirx_billing_trials','redirx_billing_checkouts','redirx_billing_periods','redirx_billing_usage','redirx_billing_events'] LOOP
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('REVOKE ALL ON %I FROM PUBLIC,anon,authenticated,service_role',t);
  EXECUTE format('GRANT SELECT ON %I TO service_role',t);
 END LOOP;
 FOR f IN SELECT oid::regprocedure AS signature FROM pg_proc WHERE pronamespace='public'::regnamespace AND proname IN(
 'current_billing_quote','current_billing_event','current_inventory_counts','reserve_current_checkout','attach_current_checkout','get_current_checkout','apply_current_checkout_payment',
 'reserve_current_terminal_usage','attach_current_terminal_usage_invoice','record_current_subscription_period','revoke_current_billing','close_current_usage','attach_current_usage_invoice_item','get_current_billing_usage') LOOP
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,anon,authenticated',f.signature);
 EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO service_role',f.signature);
 END LOOP;
END $$;
COMMIT;
