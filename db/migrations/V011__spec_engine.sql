-- Full-page immutable revisions. Effective dates are observations, not OEM issue dates.
CREATE TABLE spec.norm (
 norm_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 norm_code text NOT NULL UNIQUE, page_url text NOT NULL UNIQUE
);
CREATE TABLE spec.norm_version (
 version_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 norm_id int NOT NULL REFERENCES spec.norm,
 valid_from date NOT NULL, valid_to date,
 valid_during daterange GENERATED ALWAYS AS (daterange(valid_from,valid_to,'[)')) STORED,
 observed_at timestamptz NOT NULL, page_modified timestamptz,
 raw_sha256 text NOT NULL, content_sha256 text NOT NULL,
 historical_assumption boolean NOT NULL DEFAULT false,
 CHECK (valid_to IS NULL OR valid_to > valid_from),
 EXCLUDE USING gist (norm_id WITH =, valid_during WITH &&)
);
CREATE TABLE spec.coating_type (
 coating_type_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 version_id int NOT NULL REFERENCES spec.norm_version,
 row_no int NOT NULL CHECK(row_no>0),
 type_code_raw text NOT NULL, type_code text NOT NULL, description_de text NOT NULL,
 thickness_min_um numeric, thickness_max_um numeric,
 barrel_ok boolean, rack_ok boolean,
 coating_system text CHECK(coating_system IN ('Zn','ZnNi','ZnFe')),
 passivation text CHECK(passivation IN ('transparent','thick_layer','black','iridescent')),
 sealed boolean, raw_cells jsonb NOT NULL, quality_flags text[] NOT NULL,
 UNIQUE(version_id,row_no),
 CHECK(thickness_min_um >= 0), CHECK(thickness_max_um >= thickness_min_um)
);
CREATE INDEX coating_type_code_idx ON spec.coating_type(version_id,type_code);
CREATE TABLE spec.requirement (
 requirement_id int GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 coating_type_id int NOT NULL REFERENCES spec.coating_type,
 characteristic text NOT NULL, lsl numeric, usl numeric, unit text NOT NULL,
 kind text NOT NULL CHECK(kind IN ('requirement','reference')),
 source text NOT NULL CHECK(length(source)>0), context text NOT NULL,
 UNIQUE(coating_type_id,characteristic,kind),
 CHECK(lsl IS NOT NULL OR usl IS NOT NULL), CHECK(lsl IS NULL OR usl IS NULL OR lsl<=usl)
);
CREATE TRIGGER coating_type_audit AFTER INSERT OR UPDATE OR DELETE ON spec.coating_type
 FOR EACH ROW EXECUTE FUNCTION audit.log_change('coating_type_id');
CREATE TRIGGER norm_version_audit AFTER INSERT OR UPDATE OR DELETE ON spec.norm_version
 FOR EACH ROW EXECUTE FUNCTION audit.log_change('version_id');
CREATE TRIGGER requirement_audit AFTER INSERT OR UPDATE OR DELETE ON spec.requirement
 FOR EACH ROW EXECUTE FUNCTION audit.log_change('requirement_id');
ALTER TABLE trace.lot ADD COLUMN coating_type_id int REFERENCES spec.coating_type;

CREATE VIEW spec.v_link_candidates AS
SELECT l.lot_id, ct.coating_type_id
FROM trace.lot l JOIN trace.customer_order o USING(order_id)
JOIN spec.norm_version v ON v.valid_during @> o.received_on
JOIN spec.norm n USING(norm_id) JOIN spec.coating_type ct USING(version_id)
WHERE l.spec_code IN (n.norm_code || ' ' || ct.type_code,n.norm_code || ' ' || ct.type_code_raw)
AND ct.type_code<>'';

CREATE VIEW spec.v_lot_link_status AS
SELECT l.lot_id,l.lot_no,l.spec_code,l.coating_type_id,count(c.coating_type_id) AS candidate_count,
CASE WHEN count(c.coating_type_id)>1 THEN 'ambiguous_type_code'
     WHEN count(c.coating_type_id)=0 THEN 'no_matching_version_on_order_date'
     WHEN l.coating_type_id IS DISTINCT FROM min(c.coating_type_id) THEN 'link_missing_or_stale'
     ELSE 'linked' END AS reason
FROM trace.lot l LEFT JOIN spec.v_link_candidates c USING(lot_id)
GROUP BY l.lot_id;

CREATE FUNCTION spec.release_check(p_lot_id bigint,p_min_samples int DEFAULT 5)
RETURNS TABLE(check_name text,required text,observed text,verdict text)
LANGUAGE plpgsql STABLE AS $$
DECLARE lot trace.lot; ct spec.coating_type; ver spec.norm_version; n int; missing int;
 low numeric; high numeric; oos int; steps int; gaps int; flags text[]; candidate int; candidates int;
BEGIN
 IF p_min_samples IS NULL OR p_min_samples<1 THEN RAISE EXCEPTION 'Minimum samples must be positive'; END IF;
 SELECT * INTO lot FROM trace.lot WHERE lot_id=p_lot_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'Unknown lot %',p_lot_id; END IF;
 SELECT count(*),min(coating_type_id) INTO candidates,candidate FROM spec.v_link_candidates WHERE lot_id=p_lot_id;
 IF candidates<>1 OR lot.coating_type_id IS DISTINCT FROM candidate THEN
  RETURN QUERY SELECT 'Beschichtungszuordnung','Eindeutige Version am Auftragsdatum',candidates::text || ' Kandidaten','FAIL';
  RETURN;
 END IF;
 SELECT * INTO ct FROM spec.coating_type WHERE coating_type_id=candidate;
 SELECT * INTO ver FROM spec.norm_version WHERE version_id=ct.version_id;
 RETURN QUERY SELECT 'Quellenstand','Zeitlich belegte Spezifikation',ver.observed_at::date::text,
  CASE WHEN ver.historical_assumption AND (SELECT received_on FROM trace.customer_order WHERE order_id=lot.order_id)<ver.observed_at::date THEN 'WARN' ELSE 'PASS' END;
 SELECT array_agg(f) INTO flags FROM unnest(ct.quality_flags) f WHERE f NOT IN ('slash_fixed','whitespace_trimmed','whitespace_removed');
 RETURN QUERY SELECT 'Quelldaten','Keine offenen Unklarheiten',coalesce(array_to_string(flags,', '),'Keine'),CASE WHEN cardinality(flags)>0 THEN 'INCOMPLETE' ELSE 'PASS' END;
 SELECT count(DISTINCT sample_no) FILTER(WHERE value IS NOT NULL),min(value),max(value),count(*) FILTER(WHERE value IS NULL)
 INTO n,low,high,missing FROM trace.measurement WHERE lot_id=p_lot_id AND characteristic='thickness_um';
 RETURN QUERY SELECT 'Schichtdicke: Stichprobe','>= '||p_min_samples||' Teile (Demoannahme)',n::text,
  CASE WHEN n>=p_min_samples AND missing=0 THEN 'PASS' ELSE 'INCOMPLETE' END;
 RETURN QUERY SELECT 'Schichtdicke: Minimum',coalesce('>= '||ct.thickness_min_um||' µm','Unbekannt'),coalesce(round(low,2)::text||' µm','Nicht gemessen'),
  CASE WHEN low IS NULL OR ct.thickness_min_um IS NULL THEN 'INCOMPLETE' WHEN low<ct.thickness_min_um THEN 'FAIL' ELSE 'PASS' END;
 IF ct.thickness_max_um IS NOT NULL THEN
  RETURN QUERY SELECT 'Schichtdicke: Maximum','<= '||ct.thickness_max_um||' µm',coalesce(round(high,2)::text||' µm','Nicht gemessen'),
   CASE WHEN high IS NULL THEN 'INCOMPLETE' WHEN high>ct.thickness_max_um THEN 'FAIL' ELSE 'PASS' END;
 END IF;
 RETURN QUERY SELECT 'Anforderung: '||r.characteristic,concat_ws(' / ','>= '||r.lsl,'<= '||r.usl)||' '||r.unit,
  coalesce(min(m.value)::text||' - '||max(m.value)::text,'Nicht gemessen'),
  CASE WHEN count(m.value)=0 OR count(*) FILTER(WHERE m.measurement_id IS NOT NULL AND m.value IS NULL)>0 THEN 'INCOMPLETE'
       WHEN (r.lsl IS NOT NULL AND min(m.value)<r.lsl) OR (r.usl IS NOT NULL AND max(m.value)>r.usl) THEN 'FAIL' ELSE 'PASS' END
 FROM spec.requirement r LEFT JOIN trace.measurement m ON m.lot_id=p_lot_id AND m.characteristic=r.characteristic
 WHERE r.coating_type_id=ct.coating_type_id AND r.kind='requirement'
 GROUP BY r.requirement_id;
 -- Only direct lot measurements; ancestors contribute process exposure, never final inspection samples.
 WITH exposures AS (
  SELECT DISTINCT st.step_id,st.in_station,t.tank_id,t.process_code
  FROM trace.lot_family(p_lot_id) f JOIN trace.load_lot ll ON ll.lot_id=f.lot_id
  JOIN trace.load_step st USING(load_id) JOIN ref.station s USING(station_id) JOIN ref.tank t USING(tank_id)
  WHERE f.relation IN ('self','ancestor') AND t.function='plate'
 ), coverage AS (
  SELECT e.*,coalesce((SELECT range_agg(a.valid_during) @> e.in_station FROM lab.analysis a
    WHERE a.tank_id=e.tank_id AND a.valid_during && e.in_station
    AND EXISTS(SELECT 1 FROM lab.parameter_limit l WHERE l.process_code=e.process_code AND l.valid_during @> (a.sampled_at AT TIME ZONE 'Europe/Berlin')::date)
    AND NOT EXISTS(SELECT 1 FROM lab.parameter_limit l WHERE l.process_code=e.process_code
      AND l.valid_during @> (a.sampled_at AT TIME ZONE 'Europe/Berlin')::date
      AND NOT EXISTS(SELECT 1 FROM lab.analysis_result r WHERE r.analysis_id=a.analysis_id AND r.parameter_code=l.parameter_code))),false) AS complete
  FROM exposures e
 ) SELECT count(*),count(*) FILTER(WHERE NOT complete) INTO steps,gaps FROM coverage;
 SELECT count(DISTINCT o.analysis_id) INTO oos
 FROM trace.lot_family(p_lot_id) f JOIN trace.load_lot ll ON ll.lot_id=f.lot_id
 JOIN trace.load_step st USING(load_id) JOIN ref.station s USING(station_id)
 JOIN ref.tank t USING(tank_id) JOIN lab.v_out_of_spec_period o ON o.tank_id=t.tank_id AND o.valid_during && st.in_station
 WHERE f.relation IN ('self','ancestor') AND t.function='plate';
 RETURN QUERY SELECT 'Badanalysen: Abdeckung','Alle Beschichtungsschritte',steps||' Schritte / '||gaps||' Lücken',
  CASE WHEN steps=0 OR gaps>0 THEN 'INCOMPLETE' ELSE 'PASS' END;
 RETURN QUERY SELECT 'Bad im Prozessfenster','0 abweichende Analysen',oos::text,CASE WHEN oos>0 THEN 'WARN' ELSE 'PASS' END;
 RETURN QUERY SELECT 'Anlagenart','Trommel / Gestell zugelassen',string_agg(DISTINCT ln.handling,', '),
  CASE WHEN count(*)=0 THEN 'INCOMPLETE'
       WHEN bool_or(CASE ln.handling WHEN 'barrel' THEN ct.barrel_ok IS FALSE ELSE ct.rack_ok IS FALSE END) THEN 'FAIL'
       WHEN bool_or(CASE ln.handling WHEN 'barrel' THEN ct.barrel_ok IS NULL ELSE ct.rack_ok IS NULL END) THEN 'INCOMPLETE' ELSE 'PASS' END
 FROM trace.load_lot ll JOIN trace.load ld USING(load_id) JOIN ref.line ln USING(line_id) WHERE ll.lot_id=p_lot_id;
 RETURN QUERY SELECT 'Losstatus','Nicht gesperrt / verschrottet',lot.status,CASE WHEN lot.status IN ('blocked','scrapped') THEN 'FAIL' ELSE 'PASS' END;
END $$;
CREATE FUNCTION spec.release_verdict(p_lot_id bigint,p_min_samples int DEFAULT 5) RETURNS text
LANGUAGE sql STABLE AS $$
 SELECT CASE WHEN bool_or(verdict='FAIL') THEN 'FAIL' WHEN bool_or(verdict='INCOMPLETE') THEN 'INCOMPLETE'
 WHEN bool_or(verdict='WARN') THEN 'WARN' ELSE 'PASS' END FROM spec.release_check(p_lot_id,p_min_samples)
$$;

CREATE VIEW spec.v_norm_matrix AS
SELECT n.norm_code,ct.* FROM spec.coating_type ct JOIN spec.norm_version v USING(version_id)
JOIN spec.norm n USING(norm_id) WHERE v.valid_to IS NULL;
