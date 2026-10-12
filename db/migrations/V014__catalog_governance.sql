-- Catalog governance: version identity on source cells, observation dates, supplier reference
-- rows, requirement units and whitespace-tolerant norm name matching.

-- 1. A norm version is identified by the extracted source cells, not by parser output.
--    A parser change alone must not look like a norm change. The parser that produced a
--    version's rows is recorded next to it.
ALTER TABLE spec.norm_version ADD COLUMN cells_sha256 text, ADD COLUMN parser_version text;
COMMENT ON COLUMN spec.norm_version.cells_sha256 IS 'spec.cells_digest() of the ordered raw cell objects of all rows of this version';
COMMENT ON COLUMN spec.norm_version.parser_version IS 'ingest/p4_designation.py PARSER_VERSION that produced the normalized columns and flags';
COMMENT ON COLUMN spec.norm_version.content_sha256 IS 'Hash of the parser output (informational since V014; versions are compared on cells_sha256)';

CREATE FUNCTION spec.cells_digest(cells jsonb) RETURNS text
LANGUAGE sql IMMUTABLE STRICT AS $$ SELECT encode(sha256(convert_to(cells::text, 'UTF8')), 'hex') $$;

-- One-off backfill for versions loaded before V014 (first parser). The history guard only
-- allows closing a version, so it is bypassed for this statement; the audit trigger stays on.
ALTER TABLE spec.norm_version DISABLE TRIGGER version_history_guard;
UPDATE spec.norm_version v
   SET cells_sha256 = spec.cells_digest(coalesce(
         (SELECT jsonb_agg(ct.raw_cells ORDER BY ct.row_no) FROM spec.coating_type ct WHERE ct.version_id = v.version_id),
         '[]'::jsonb)),
       parser_version = '1'
 WHERE v.cells_sha256 IS NULL;
ALTER TABLE spec.norm_version ENABLE TRIGGER version_history_guard;

-- 2. Observation dates. A version that is not marked as a historical assumption cannot
--    take effect before the day it was observed, and observations of one page only move forward.
ALTER TABLE spec.norm_version ADD CONSTRAINT norm_version_backdate_flagged
  CHECK (historical_assumption OR valid_from >= (observed_at AT TIME ZONE 'UTC')::date);

CREATE FUNCTION spec.guard_version_insert() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM spec.norm_version v
              WHERE v.norm_id = NEW.norm_id
                AND (v.observed_at >= NEW.observed_at OR v.valid_from >= NEW.valid_from)) THEN
    RAISE EXCEPTION 'A new norm version must be observed after and take effect after every earlier version of the same norm';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_insert_guard BEFORE INSERT ON spec.norm_version
  FOR EACH ROW EXECUTE FUNCTION spec.guard_version_insert();

-- 3. Requirements and supplier references.
--    The comparison operator of a lower bound is stored, not implied by free text.
ALTER TABLE spec.requirement ADD COLUMN lower_operator text NOT NULL DEFAULT '>='
  CHECK (lower_operator IN ('>=', '>'));
UPDATE spec.requirement SET lower_operator = '>'
 WHERE kind = 'reference' AND context LIKE '%strictly >%';

-- Units of the numeric lot characteristics (trace.measurement.characteristic).
CREATE TABLE spec.characteristic_unit (
  characteristic text PRIMARY KEY,
  unit           text NOT NULL
);
INSERT INTO spec.characteristic_unit VALUES
  ('thickness_um', 'µm'), ('ni_pct', '%'), ('nss_h_white', 'h'), ('nss_h_red', 'h'), ('friction_coeff', '1');
CREATE TRIGGER characteristic_unit_audit AFTER INSERT OR UPDATE OR DELETE ON spec.characteristic_unit
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('characteristic');

CREATE FUNCTION spec.guard_requirement() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.kind IS DISTINCT FROM OLD.kind THEN
    RAISE EXCEPTION 'A supplier reference cannot become a release requirement (or back); insert a new row with its own citation';
  END IF;
  IF NEW.kind = 'requirement' AND NOT EXISTS (
       SELECT 1 FROM spec.characteristic_unit u WHERE u.characteristic = NEW.characteristic AND u.unit = NEW.unit) THEN
    RAISE EXCEPTION 'Requirement % in unit % does not match a measured characteristic and its unit', NEW.characteristic, NEW.unit;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER requirement_guard BEFORE INSERT OR UPDATE ON spec.requirement
  FOR EACH ROW EXECUTE FUNCTION spec.guard_requirement();

-- 4. Matching ignores repeated or surrounding whitespace in norm names and ordered designations
--    (one published title contains a double space). Stored values keep the source spelling.
CREATE OR REPLACE VIEW spec.v_link_candidates AS
SELECT DISTINCT l.lot_id, k.coating_type_id
FROM trace.lot l JOIN trace.customer_order o USING(order_id)
JOIN (SELECT ct.coating_type_id, v.valid_during,
             regexp_replace(btrim(n.norm_code), '\s+', ' ', 'g') || ' ' || code.type_code AS spec_key
        FROM spec.coating_type ct JOIN spec.norm_version v USING(version_id) JOIN spec.norm n USING(norm_id)
        CROSS JOIN LATERAL (VALUES (ct.type_code), (regexp_replace(btrim(ct.type_code_raw), '\s+', ' ', 'g'))) code(type_code)
       WHERE ct.type_code <> '' OFFSET 0) k  -- OFFSET 0: normalize each catalog key once, then hash-join
  ON k.spec_key = regexp_replace(btrim(l.spec_code), '\s+', ' ', 'g') AND k.valid_during @> o.received_on;
