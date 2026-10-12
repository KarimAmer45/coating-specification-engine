-- Parameterize lot_id in the application; this query uses the planted complaint.
SELECT * FROM spec.release_check((SELECT lot_id FROM trace.complaint ORDER BY complaint_id LIMIT 1));
SELECT reason,count(*) FROM spec.v_lot_link_status GROUP BY reason;
SELECT norm_code,row_no,type_code_raw,type_code,quality_flags FROM spec.v_norm_matrix
 WHERE cardinality(quality_flags)>0 ORDER BY norm_code,row_no;
SELECT n.norm_code,v.version_id,v.valid_from,v.valid_to,v.observed_at,v.historical_assumption
 FROM spec.norm n JOIN spec.norm_version v USING(norm_id) ORDER BY n.norm_code,v.valid_from;
