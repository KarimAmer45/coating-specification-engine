# Open items

What I have done and what is still open for Project 4.

Done:
- Clean offline build on an empty database, reproducing 55 pages / 217 rows / 5,687 linked lots and the WARN complaint lot; the test suite runs with `python scripts/record_tests.py`.
- Release check hardened: analysis age, late analysis entries, inspection readings taken after plating, coating system vs. plating process, non-releasable lot states, inherited-only plating, thickness plausibility, technical verdict, evidence in the report pair, LF-only JSON with a matching footer hash.
- Analysis entry times before `entered_at` existed are assumed equal to the sampling time; real entry times only exist for analyses entered from now on.
- Catalog history: versions keyed on raw cells with a parser version; observation dates enforced.
- Docker / Flyway startup ran cleanly on 2026-10-12, and the first hosted CI run (built from the synthetic fixture) passed the same day.

Still open:
- Supplier PDF: the sterc.org mirror no longer resolves; on 2026-10-11 I checked Table 1 of the copy at nmfrc.org against the six transcribed values (> 120 / 720 h blue, > 200 / 1,008 h blue + sealer and black + sealer); they match. A local copy of the PDF is still not saved.
- Review the single-value ISO 19598 interpretation against a licensed copy of the standard; keep the printed interpretation note until then.
- Obtain historical norm evidence and a customer-approved sampling plan before treating any demo check as an operational release decision. The five-sample threshold, the 4-day analysis age and the 3 × plausibility factor are my assumptions.
- Requirement-change governance: requirements are audited but not versioned, so a rerun can differ from an issued report. Keep the generated PDF/JSON pairs as the record.
- Perform `pg_dump` / restore into a disposable database and compare counts, versions, references and release results; record elapsed time.
- Capture `EXPLAIN (ANALYZE, BUFFERS)` evidence for catalog matching and release checks.
- Review Partec's website terms before publishing anything derived from the pages; the row-level extracts are git-ignored.
- Optional: a read-only lot picker / live assessment page.
