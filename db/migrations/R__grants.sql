GRANT USAGE ON SCHEMA ref,lab,trace,spec TO role_readonly,role_operator,role_lab,role_quality,role_ingest;
GRANT SELECT ON ALL TABLES IN SCHEMA ref,lab,trace,spec TO role_readonly,role_operator,role_lab,role_quality,role_ingest;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA ref,lab,trace,spec TO role_ingest,role_quality,role_operator,role_lab;
GRANT INSERT,UPDATE ON trace.load,trace.load_lot,trace.load_step TO role_operator;
GRANT INSERT ON trace.measurement TO role_operator;
GRANT INSERT,UPDATE ON lab.analysis,lab.analysis_result TO role_lab;
GRANT UPDATE(status,coating_type_id) ON trace.lot TO role_quality;
GRANT INSERT,UPDATE ON spec.requirement TO role_quality;
GRANT INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA ref,lab,trace,spec TO role_ingest;
REVOKE ALL ON audit.change_log FROM PUBLIC;
GRANT USAGE ON SCHEMA audit TO role_quality;
GRANT SELECT ON audit.change_log TO role_quality;
-- Evidence hardening (V013): the broad grants above must not give back timeline or limit rewrites.
-- Corrections to analysis values go through lab.analysis_result (audited); a changed limit is a new version.
REVOKE UPDATE, DELETE, TRUNCATE ON lab.analysis FROM role_lab, role_ingest, role_operator, role_quality;
REVOKE DELETE, TRUNCATE ON lab.parameter_limit FROM role_quality, role_ingest;
