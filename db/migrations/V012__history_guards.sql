-- Published coating rows cannot be edited in place, even by the ingest group.
CREATE FUNCTION spec.protect_coating_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Coating history is immutable; load a new norm version'; END $$;
CREATE TRIGGER coating_history_guard BEFORE UPDATE OR DELETE ON spec.coating_type
 FOR EACH ROW EXECUTE FUNCTION spec.protect_coating_history();
CREATE FUNCTION spec.protect_version_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Cannot delete a norm version'; END IF;
 IF OLD.valid_to IS NOT NULL OR NEW.valid_to IS NULL OR
    (to_jsonb(OLD)-'valid_to'-'valid_during') IS DISTINCT FROM (to_jsonb(NEW)-'valid_to'-'valid_during') THEN
  RAISE EXCEPTION 'Only closing an open norm version is allowed';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER version_history_guard BEFORE UPDATE OR DELETE ON spec.norm_version
 FOR EACH ROW EXECUTE FUNCTION spec.protect_version_history();
ALTER TABLE trace.measurement ADD CONSTRAINT measurement_finite CHECK(value IS NULL OR value NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric));
ALTER TABLE spec.requirement ADD CONSTRAINT requirement_finite CHECK((lsl IS NULL OR lsl NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)) AND (usl IS NULL OR usl NOT IN ('NaN'::numeric,'Infinity'::numeric,'-Infinity'::numeric)));
