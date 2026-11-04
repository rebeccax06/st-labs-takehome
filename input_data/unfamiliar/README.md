# Unfamiliar source fixtures

These are small synthetic, vendor-inspired exports, not authenticated vendor exports or guarantees of compatibility with a vendor API version. SaaS fixtures include explicit application context to avoid requiring the model to invent it. Missing MFA must remain unknown.

Upload each inventory file through POST /ingest with source `uploaded-data`. All ten layouts require AI extraction. Do not upload this README or manifest as inventory. NetBox includes a CIDR address to check whether extraction can normalize it to a host IP.

Run `python -m examples.check_unfamiliar` from the project root with the project virtualenv. This makes live, potentially billable OpenRouter requests. Each case uses a separate temporary SQLite database; cmdb.db is untouched. Output includes actual ingestion, extracted inventory/detail, replay, and basic field-preservation checks. A passed check is a small smoke test, not complete semantic or vendor compatibility verification.

The checks cover selected expected fields, AI provenance, and replay. They do not verify that every entity or field was extracted.

To run selected cases, add `--file github_style.json --file salesforce_style.json`.
