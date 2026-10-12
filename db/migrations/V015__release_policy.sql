-- Release check: analysis age limit, analysis entry time, coating system vs. plating process,
-- non-releasable lot states, inspection readings taken after plating, thickness plausibility on
-- minimum-only specifications, a technical verdict without the source-date check, and checks
-- that name their evidence.

-- Demonstration policy. scripts/load_policy.py keeps this row equal to config/release_policy.yaml.
CREATE TABLE spec.release_policy (
  policy_id                      boolean PRIMARY KEY DEFAULT true CHECK (policy_id),
  min_distinct_thickness_samples int      NOT NULL CHECK (min_distinct_thickness_samples > 0),
  max_analysis_age               interval NOT NULL CHECK (max_analysis_age > interval '0'),
  max_thickness_factor           numeric  NOT NULL CHECK (max_thickness_factor > 1),
  source                         text     NOT NULL
);
INSERT INTO spec.release_policy VALUES (true, 5, interval '4 days', 3, 'V015 default; synchronized from config/release_policy.yaml');
CREATE TRIGGER release_policy_audit AFTER INSERT OR UPDATE OR DELETE ON spec.release_policy
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('policy_id');

-- Which plating processes can produce a coating system. Unknown systems stay INCOMPLETE.
CREATE TABLE spec.coating_system_process (
  coating_system text NOT NULL CHECK (coating_system IN ('Zn','ZnNi','ZnFe')),
  process_code   text NOT NULL,
  PRIMARY KEY (coating_system, process_code)
);
INSERT INTO spec.coating_system_process VALUES ('ZnNi','ZNNI_ALK'), ('Zn','ZN_ALK');
CREATE TRIGGER coating_system_process_audit AFTER INSERT OR UPDATE OR DELETE ON spec.coating_system_process
  FOR EACH ROW EXECUTE FUNCTION audit.log_change('coating_system', 'process_code');

-- When an analysis was entered. An analysis entered after a plating step ended was not available
-- when the parts ran; it may be a legitimate late entry or a gap being covered afterwards, so the
-- release check asks for a manual review (WARN). The time is set by the database, not the client.
-- Analyses that already exist were loaded in bulk (simulation or earlier imports) without an entry
-- time; they are taken as entered at their sampling time. That is an assumption, not a record.
ALTER TABLE lab.analysis ADD COLUMN entered_at timestamptz;
UPDATE lab.analysis SET entered_at = sampled_at WHERE entered_at IS NULL;
ALTER TABLE lab.analysis ALTER COLUMN entered_at SET DEFAULT now(), ALTER COLUMN entered_at SET NOT NULL;
CREATE FUNCTION lab.stamp_analysis_entry() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.entered_at := now();
  RETURN NEW;
END $$;
CREATE TRIGGER analysis_entered_at BEFORE INSERT ON lab.analysis
  FOR EACH ROW EXECUTE FUNCTION lab.stamp_analysis_entry();

-- Display helpers for the German report.
CREATE FUNCTION spec.de_num(x numeric) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT replace(trim_scale(x)::text, '.', ',') $$;
CREATE FUNCTION spec.de_date(d date) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS
$$ SELECT to_char(d, 'DD.MM.YYYY') $$;
CREATE FUNCTION spec.status_label(s text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE s WHEN 'received' THEN 'eingegangen' WHEN 'in_process' THEN 'in Bearbeitung'
   WHEN 'blocked' THEN 'gesperrt' WHEN 'reworked' THEN 'nachgearbeitet' WHEN 'released' THEN 'freigegeben'
   WHEN 'shipped' THEN 'ausgeliefert' WHEN 'scrapped' THEN 'verschrottet' ELSE s END $$;
CREATE FUNCTION spec.parameter_label(code text) RETURNS text LANGUAGE sql IMMUTABLE AS $$
 SELECT CASE code WHEN 'NI' THEN 'Ni' WHEN 'ZN' THEN 'Zn' WHEN 'NAOH' THEN 'NaOH'
   WHEN 'NA2CO3' THEN 'Na2CO3' WHEN 'COD' THEN 'CSB' ELSE code END $$;
CREATE FUNCTION spec.plural(n bigint, one text, many text) RETURNS text LANGUAGE sql IMMUTABLE AS
$$ SELECT n || ' ' || CASE WHEN n = 1 THEN one ELSE many END $$;
CREATE FUNCTION spec.age_label(i interval) RETURNS text LANGUAGE sql IMMUTABLE STRICT AS $$
 SELECT CASE WHEN i = make_interval(days => extract(day FROM i)::int)
             THEN extract(day FROM i)::int || CASE WHEN extract(day FROM i) = 1 THEN ' Tag' ELSE ' Tage' END
             ELSE i::text END $$;

-- Plating dwells that count for a lot: its own and its ancestors' (rework parents).
CREATE FUNCTION spec.plating_exposure(p_lot_id bigint)
RETURNS TABLE(step_id bigint, relation text, lot_no text, load_id bigint, carrier_no text,
              tank_id int, tank_code text, process_code text, in_station tstzrange)
LANGUAGE sql STABLE AS $$
  SELECT DISTINCT ON (st.step_id) st.step_id, f.relation, f.lot_no, st.load_id, ld.carrier_no,
         t.tank_id, t.code, t.process_code, st.in_station
    FROM trace.lot_family(p_lot_id) f
    JOIN trace.load_lot ll ON ll.lot_id = f.lot_id
    JOIN trace.load_step st ON st.load_id = ll.load_id
    JOIN trace.load ld ON ld.load_id = st.load_id
    JOIN ref.station s ON s.station_id = st.station_id
    JOIN ref.tank t ON t.tank_id = s.tank_id
   WHERE f.relation IN ('self', 'ancestor') AND t.function = 'plate'
   ORDER BY st.step_id, f.depth DESC
$$;

-- Every analysis overlapping each plating dwell. counted_during is the part of the dwell the
-- analysis may stand for: its validity, cut at sampled_at + max_age (calendar arithmetic in Berlin
-- local time, so a DST change does not shift the cut). parameters_complete says whether the
-- analysis reports every parameter that has a limit for the process on its sampling day.
CREATE FUNCTION spec.plating_coverage(p_lot_id bigint, p_max_age interval)
RETURNS TABLE(step_id bigint, relation text, lot_no text, carrier_no text, tank_code text, process_code text,
              in_station tstzrange, analysis_id bigint, sampled_at timestamptz, entered_at timestamptz,
              valid_during tstzrange, counted_during tstzrange, parameters_complete boolean)
LANGUAGE sql STABLE AS $$
  SELECT e.step_id, e.relation, e.lot_no, e.carrier_no, e.tank_code, e.process_code, e.in_station,
         a.analysis_id, a.sampled_at, a.entered_at, a.valid_during,
         a.valid_during * tstzrange(a.sampled_at,
           ((a.sampled_at AT TIME ZONE 'Europe/Berlin') + p_max_age) AT TIME ZONE 'Europe/Berlin', '[)') * e.in_station,
         CASE WHEN a.analysis_id IS NULL THEN NULL ELSE
           EXISTS (SELECT 1 FROM lab.parameter_limit l WHERE l.process_code = e.process_code
                     AND l.valid_during @> (a.sampled_at AT TIME ZONE 'Europe/Berlin')::date)
           AND NOT EXISTS (SELECT 1 FROM lab.parameter_limit l WHERE l.process_code = e.process_code
                     AND l.valid_during @> (a.sampled_at AT TIME ZONE 'Europe/Berlin')::date
                     AND NOT EXISTS (SELECT 1 FROM lab.analysis_result r
                                      WHERE r.analysis_id = a.analysis_id AND r.parameter_code = l.parameter_code)) END
    FROM spec.plating_exposure(p_lot_id) e
    LEFT JOIN lab.analysis a ON a.tank_id = e.tank_id AND a.valid_during && e.in_station
$$;

-- A lot's measurements, marking readings taken before their plating ended. A reading names its
-- load when known; otherwise the end of the lot's last own plating step is used.
CREATE FUNCTION spec.lot_measurements(p_lot_id bigint)
RETURNS TABLE(measurement_id bigint, characteristic text, sample_no int, value numeric, measured_at timestamptz,
              plating_end timestamptz, before_plating_end boolean)
LANGUAGE sql STABLE AS $$
  SELECT m.measurement_id, m.characteristic, m.sample_no, m.value, m.measured_at, pe.plating_end,
         coalesce(m.measured_at < pe.plating_end, false)
    FROM trace.measurement m
    LEFT JOIN LATERAL (
      SELECT max(upper(st.in_station)) AS plating_end
        FROM trace.load_lot ll
        JOIN trace.load_step st ON st.load_id = ll.load_id
        JOIN ref.station s ON s.station_id = st.station_id
        JOIN ref.tank t ON t.tank_id = s.tank_id
       WHERE ll.lot_id = p_lot_id AND t.function = 'plate' AND (m.load_id IS NULL OR st.load_id = m.load_id)) pe ON true
   WHERE m.lot_id = p_lot_id
$$;

CREATE OR REPLACE FUNCTION spec.release_check(p_lot_id bigint, p_min_samples int DEFAULT NULL)
RETURNS TABLE(check_name text, required text, observed text, verdict text)
LANGUAGE plpgsql STABLE AS $$
DECLARE lot trace.lot; ct spec.coating_type; ver spec.norm_version; nxt spec.norm_version; pol spec.release_policy;
 linked boolean := true; v_norm text; ordered date; seen date; min_samples int; age text; cap numeric;
 n int; missing int; low numeric; high numeric; candidates int; candidate int; flags text[]; iso_note boolean;
 steps int; gaps int; stale int; inherited int; parents text; late text; oos int; oos_text text; early int;
 procs text[]; allowed text[]; foreign_procs text[];
BEGIN
 SELECT * INTO pol FROM spec.release_policy WHERE policy_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'No release policy configured'; END IF;
 min_samples := coalesce(p_min_samples, pol.min_distinct_thickness_samples);
 IF min_samples < 1 THEN RAISE EXCEPTION 'Minimum samples must be positive'; END IF;
 age := spec.age_label(pol.max_analysis_age);
 SELECT * INTO lot FROM trace.lot WHERE lot_id = p_lot_id;
 IF NOT FOUND THEN RAISE EXCEPTION 'Unknown lot %', p_lot_id; END IF;
 SELECT o.received_on INTO ordered FROM trace.customer_order o WHERE o.order_id = lot.order_id;

 -- Catalog link. A missing, ambiguous or stale link is missing evidence, not a measured deviation;
 -- checks that do not need the catalog row still run so that a real FAIL is not hidden.
 SELECT count(*), min(c.coating_type_id) INTO candidates, candidate FROM spec.v_link_candidates c WHERE c.lot_id = p_lot_id;
 IF candidates <> 1 OR lot.coating_type_id IS DISTINCT FROM candidate THEN
  linked := false;
  RETURN QUERY SELECT 'Beschichtungszuordnung', 'Eindeutige Katalogzeile am Auftragsdatum',
   CASE WHEN candidates = 0 THEN 'Keine Katalogzeile am ' || spec.de_date(ordered)
        WHEN candidates > 1 THEN 'Mehrdeutig: ' || candidates || ' Katalogzeilen'
        WHEN lot.coating_type_id IS NULL THEN 'Nicht verknüpft (1 Kandidat)'
        ELSE 'Verknüpfung veraltet; neu verknüpfen (1 Kandidat)' END,
   'INCOMPLETE';
 END IF;

 IF linked THEN
  SELECT * INTO ct FROM spec.coating_type WHERE coating_type_id = candidate;
  SELECT * INTO ver FROM spec.norm_version WHERE version_id = ct.version_id;
  SELECT nn.norm_code INTO v_norm FROM spec.norm nn WHERE nn.norm_id = ver.norm_id;
  seen := (ver.observed_at AT TIME ZONE 'UTC')::date;
  SELECT * INTO nxt FROM spec.norm_version WHERE norm_id = ver.norm_id AND valid_from = ver.valid_to;
  RETURN QUERY SELECT 'Quellenstand', 'Webstand zum Auftragsdatum belegt',
   'Beobachtet ' || spec.de_date(seen)
   || coalesce(', Seite geändert ' || spec.de_date((ver.page_modified AT TIME ZONE 'UTC')::date), '')
   || CASE WHEN ver.historical_assumption AND ordered < seen
             THEN '; rückwirkend angenommen für Auftrag vom ' || spec.de_date(ordered)
           WHEN ver.valid_to IS NOT NULL AND ordered > seen
                AND NOT coalesce((nxt.page_modified AT TIME ZONE 'UTC')::date > ordered, false)
             THEN '; Auftrag vom ' || spec.de_date(ordered) || ' liegt zwischen dieser und der nächsten Beobachtung ('
                  || coalesce(spec.de_date((nxt.observed_at AT TIME ZONE 'UTC')::date), '?') || '), Änderungsdatum offen'
           ELSE '' END,
   CASE WHEN ver.historical_assumption AND ordered < seen THEN 'WARN'
        WHEN ver.valid_to IS NOT NULL AND ordered > seen
             AND NOT coalesce((nxt.page_modified AT TIME ZONE 'UTC')::date > ordered, false) THEN 'WARN'
        ELSE 'PASS' END;

  SELECT array_agg(f ORDER BY f) INTO flags FROM unnest(ct.quality_flags) f
   WHERE f NOT IN ('slash_fixed', 'whitespace_trimmed', 'whitespace_removed', 'iso19598_single_value_as_minimum');
  iso_note := regexp_replace(btrim(v_norm), '\s+', ' ', 'g') = 'DIN EN ISO 19598'
              AND ct.thickness_min_um IS NOT NULL AND ct.thickness_max_um IS NULL;
  RETURN QUERY SELECT 'Quelldaten', 'Keine offenen Unklarheiten',
   coalesce(array_to_string(flags, ', '), 'Keine')
   || CASE WHEN iso_note THEN '; Auslegung: Einzelwert als Mindestschichtdicke (ISO 19598) gelesen' ELSE '' END,
   CASE WHEN cardinality(flags) > 0 THEN 'INCOMPLETE' ELSE 'PASS' END;

  SELECT array_agg(DISTINCT e.process_code ORDER BY e.process_code) INTO procs FROM spec.plating_exposure(p_lot_id) e;
  SELECT array_agg(m.process_code ORDER BY m.process_code) INTO allowed FROM spec.coating_system_process m
   WHERE m.coating_system = ct.coating_system;
  foreign_procs := ARRAY(SELECT unnest(procs) EXCEPT SELECT unnest(allowed));
  RETURN QUERY SELECT 'Beschichtungssystem / Bad',
   coalesce(ct.coating_system, 'Nicht auswertbar') || ': ' || coalesce(array_to_string(allowed, ', '), 'kein Prozess hinterlegt'),
   coalesce(array_to_string(procs, ', '), 'Keine Beschichtung erfasst'),
   CASE WHEN procs IS NOT NULL AND allowed IS NOT NULL AND cardinality(foreign_procs) > 0 THEN 'FAIL'
        WHEN ct.coating_system IS NULL OR allowed IS NULL OR procs IS NULL THEN 'INCOMPLETE'
        ELSE 'PASS' END;
 END IF;

 -- Final inspection evidence: only the lot's own measurements, never an ancestor's, and only
 -- readings taken after the plating they measure had ended.
 SELECT count(DISTINCT m.sample_no) FILTER (WHERE m.value IS NOT NULL AND NOT m.before_plating_end),
        min(m.value) FILTER (WHERE NOT m.before_plating_end), max(m.value) FILTER (WHERE NOT m.before_plating_end),
        count(*) FILTER (WHERE m.value IS NULL), count(*) FILTER (WHERE m.before_plating_end)
   INTO n, low, high, missing, early FROM spec.lot_measurements(p_lot_id) m WHERE m.characteristic = 'thickness_um';
 RETURN QUERY SELECT 'Schichtdicke: Stichprobe', '≥ ' || min_samples || ' Teile nach Beschichtung (Demoannahme)',
  n::text || CASE WHEN early > 0 THEN '; ' || spec.plural(early, 'Messung', 'Messungen') || ' vor Beschichtungsende nicht gewertet' ELSE '' END,
  CASE WHEN n >= min_samples AND missing = 0 AND early = 0 THEN 'PASS' ELSE 'INCOMPLETE' END;

 IF linked THEN
  RETURN QUERY SELECT 'Schichtdicke: Minimum', coalesce('≥ ' || spec.de_num(ct.thickness_min_um) || ' µm', 'Unbekannt'),
   coalesce(spec.de_num(round(low, 2)) || ' µm', 'Nicht gemessen'),
   CASE WHEN low IS NULL OR ct.thickness_min_um IS NULL THEN 'INCOMPLETE' WHEN low < ct.thickness_min_um THEN 'FAIL' ELSE 'PASS' END;
  IF ct.thickness_max_um IS NOT NULL THEN
   RETURN QUERY SELECT 'Schichtdicke: Maximum', '≤ ' || spec.de_num(ct.thickness_max_um) || ' µm',
    coalesce(spec.de_num(round(high, 2)) || ' µm', 'Nicht gemessen'),
    CASE WHEN high IS NULL THEN 'INCOMPLETE' WHEN high > ct.thickness_max_um THEN 'FAIL' ELSE 'PASS' END;
  ELSIF ct.thickness_min_um IS NOT NULL THEN
   -- A minimum-only specification has no published upper bound; implausible readings
   -- (unit or decimal slips) need review instead of a clean pass.
   cap := ct.thickness_min_um * pol.max_thickness_factor;
   RETURN QUERY SELECT 'Schichtdicke: Plausibilität',
    '≤ ' || spec.de_num(cap) || ' µm (' || spec.de_num(pol.max_thickness_factor) || ' × Minimum, Demoannahme)',
    coalesce(spec.de_num(round(high, 2)) || ' µm', 'Nicht gemessen'),
    CASE WHEN high IS NULL OR high > cap THEN 'INCOMPLETE' ELSE 'PASS' END;
  END IF;
  RETURN QUERY SELECT 'Anforderung: ' || r.characteristic,
   concat_ws(' / ', CASE r.lower_operator WHEN '>' THEN '> ' ELSE '≥ ' END || spec.de_num(r.lsl), '≤ ' || spec.de_num(r.usl)) || ' ' || r.unit,
   coalesce(spec.de_num(min(m.value)) || ' – ' || spec.de_num(max(m.value)), 'Nicht gemessen'),
   CASE WHEN count(m.value) = 0 OR count(*) FILTER (WHERE m.measurement_id IS NOT NULL AND m.value IS NULL) > 0 THEN 'INCOMPLETE'
        WHEN (r.lsl IS NOT NULL AND (min(m.value) < r.lsl OR (r.lower_operator = '>' AND min(m.value) = r.lsl)))
          OR (r.usl IS NOT NULL AND max(m.value) > r.usl) THEN 'FAIL' ELSE 'PASS' END
  FROM spec.requirement r
  LEFT JOIN spec.lot_measurements(p_lot_id) m ON m.characteristic = r.characteristic AND NOT m.before_plating_end
  WHERE r.coating_type_id = ct.coating_type_id AND r.kind = 'requirement'
  GROUP BY r.requirement_id;
 END IF;

 -- Bath evidence. Every plating dwell (own and rework ancestors') must be covered by analyses
 -- that report all limited parameters and are not older than the policy's maximum age. A lot
 -- with no plating of its own (a rework child) only inherits its parent's exposure, and an
 -- analysis entered after the step it covers ended was added afterwards: both need review.
 WITH per_step AS (
   SELECT c.step_id, c.relation, c.lot_no,
          coalesce(range_agg(c.counted_during) FILTER (WHERE c.parameters_complete) @> c.in_station, false) AS complete,
          coalesce(range_agg(c.valid_during) FILTER (WHERE c.parameters_complete) @> c.in_station, false) AS complete_without_age
     FROM spec.plating_coverage(p_lot_id, pol.max_analysis_age) c
    GROUP BY c.step_id, c.relation, c.lot_no, c.in_station)
 SELECT count(*), count(*) FILTER (WHERE NOT complete), count(*) FILTER (WHERE NOT complete AND complete_without_age),
        count(*) FILTER (WHERE relation = 'ancestor'),
        string_agg(DISTINCT lot_no, ', ') FILTER (WHERE relation = 'ancestor')
   INTO steps, gaps, stale, inherited, parents FROM per_step;
 SELECT string_agg(DISTINCT 'Analyse ' || c.analysis_id || ' nachgetragen (erfasst '
          || to_char(c.entered_at AT TIME ZONE 'Europe/Berlin', 'DD.MM.YYYY HH24:MI') || ')', '; ')
   INTO late FROM spec.plating_coverage(p_lot_id, pol.max_analysis_age) c
  WHERE c.parameters_complete AND NOT isempty(c.counted_during) AND c.entered_at > upper(c.in_station);
 RETURN QUERY SELECT 'Badanalysen: Abdeckung', 'Alle Beschichtungsschritte, Analyse höchstens ' || age || ' alt, vorher erfasst',
  spec.plural(steps, 'Schritt', 'Schritte') || ' / ' || spec.plural(gaps, 'Lücke', 'Lücken')
  || CASE WHEN steps > 0 AND inherited = steps THEN '; Beschichtung nur vom Elternlos ' || parents
          WHEN inherited > 0 THEN ' (davon ' || inherited || ' vom Elternlos ' || parents || ')' ELSE '' END
  || CASE WHEN stale > 0 THEN '; ' || spec.plural(stale, 'Schritt', 'Schritte') || ' nur durch ältere Analysen gedeckt' ELSE '' END
  || coalesce('; ' || late, ''),
  CASE WHEN steps = 0 OR gaps > 0 THEN 'INCOMPLETE'
       WHEN inherited = steps OR late IS NOT NULL THEN 'WARN' ELSE 'PASS' END;

 -- At most three findings are spelled out so that the report cell stays readable; the JSON
 -- snapshot lists every result.
 SELECT count(DISTINCT x.analysis_id),
        string_agg('Analyse ' || x.analysis_id || ': ' || spec.parameter_label(x.parameter_code) || ' ' || spec.de_num(x.value)
                   || CASE x.status WHEN 'LOW' THEN ' < ' || spec.de_num(x.lsl) ELSE ' > ' || spec.de_num(x.usl) END
                   || ' ' || x.unit, '; ' ORDER BY x.analysis_id, x.parameter_code) FILTER (WHERE x.rn <= 3)
        || CASE WHEN count(*) > 3 THEN ' und ' || (count(*) - 3) || ' weitere' ELSE '' END
   INTO oos, oos_text
   FROM (SELECT y.*, row_number() OVER (ORDER BY y.analysis_id, y.parameter_code) AS rn
           FROM (SELECT DISTINCT r.analysis_id, r.parameter_code, r.value, r.lsl, r.usl, r.status, p.unit
                   FROM (SELECT DISTINCT e.tank_id, e.in_station FROM spec.plating_exposure(p_lot_id) e) e
                   JOIN lab.v_result_status r ON r.tank_id = e.tank_id AND r.valid_during && e.in_station
                   JOIN lab.parameter p ON p.parameter_code = r.parameter_code
                  WHERE r.status IN ('LOW', 'HIGH')) y) x;
 RETURN QUERY SELECT 'Bad im Prozessfenster', 'Keine Analyse außerhalb des Fensters', coalesce(oos_text, 'Keine'),
  CASE WHEN oos > 0 THEN 'WARN' ELSE 'PASS' END;

 IF linked THEN
  RETURN QUERY SELECT 'Anlagenart', 'Trommel / Gestell zugelassen',
   coalesce(string_agg(DISTINCT CASE ln.handling WHEN 'barrel' THEN 'Trommel' WHEN 'rack' THEN 'Gestell' ELSE ln.handling END, ', '), 'Keine eigene Beschichtung erfasst'),
   CASE WHEN count(*) = 0 THEN 'INCOMPLETE'
        WHEN bool_or(CASE ln.handling WHEN 'barrel' THEN ct.barrel_ok IS FALSE ELSE ct.rack_ok IS FALSE END) THEN 'FAIL'
        WHEN bool_or(CASE ln.handling WHEN 'barrel' THEN ct.barrel_ok IS NULL ELSE ct.rack_ok IS NULL END) THEN 'INCOMPLETE' ELSE 'PASS' END
  FROM trace.load_lot ll JOIN trace.load ld ON ld.load_id = ll.load_id JOIN ref.line ln ON ln.line_id = ld.line_id
  WHERE ll.lot_id = p_lot_id
    -- only carriers that actually went through a plating tank count for the handling check
    AND EXISTS (SELECT 1 FROM trace.load_step st JOIN ref.station s ON s.station_id = st.station_id
                  JOIN ref.tank t ON t.tank_id = s.tank_id WHERE st.load_id = ld.load_id AND t.function = 'plate');
 END IF;

 -- Only inspected finished output is releasable. A reworked parent's pieces have moved to its
 -- rework child, so the parent is a closed non-release like blocked/scrapped (FAIL); a lot that is
 -- still received or in process has no final evidence yet (INCOMPLETE).
 RETURN QUERY SELECT 'Losstatus', 'Geprüfte Fertigware (freigegeben / ausgeliefert)', spec.status_label(lot.status),
  CASE WHEN lot.status IN ('blocked', 'scrapped', 'reworked') THEN 'FAIL'
       WHEN lot.status IN ('released', 'shipped') THEN 'PASS' ELSE 'INCOMPLETE' END;
END $$;

-- Overall verdict, and the technical verdict without the source-date check (Quellenstand), so that
-- historical demo lots can still be told apart. The overall verdict stays the conservative one.
CREATE FUNCTION spec.release_verdicts(p_lot_id bigint, p_min_samples int DEFAULT NULL)
RETURNS TABLE(verdict text, technical_verdict text) LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN bool_or(c.verdict = 'FAIL') THEN 'FAIL' WHEN bool_or(c.verdict = 'INCOMPLETE') THEN 'INCOMPLETE'
              WHEN bool_or(c.verdict = 'WARN') THEN 'WARN' ELSE 'PASS' END,
         CASE WHEN bool_or(c.verdict = 'FAIL') FILTER (WHERE c.check_name <> 'Quellenstand') THEN 'FAIL'
              WHEN bool_or(c.verdict = 'INCOMPLETE') FILTER (WHERE c.check_name <> 'Quellenstand') THEN 'INCOMPLETE'
              WHEN bool_or(c.verdict = 'WARN') FILTER (WHERE c.check_name <> 'Quellenstand') THEN 'WARN' ELSE 'PASS' END
    FROM spec.release_check(p_lot_id, p_min_samples) c
$$;

CREATE OR REPLACE FUNCTION spec.release_verdict(p_lot_id bigint, p_min_samples int DEFAULT NULL) RETURNS text
LANGUAGE sql STABLE AS $$ SELECT verdict FROM spec.release_verdicts(p_lot_id, p_min_samples) $$;
