# Unfamiliar source fixtures

These are small synthetic, vendor-inspired exports, not authenticated vendor exports or guarantees of compatibility with a vendor API version. SaaS fixtures include explicit application context to avoid requiring the model to invent it. Missing MFA must remain unknown.

Upload each inventory file through POST /ingest with source `uploaded-data`. All ten layouts require AI extraction. Do not upload this README or manifest as inventory. NetBox includes a CIDR address to check whether extraction can normalize it to a host IP.

Run `python -m examples.check_unfamiliar` from the project root with the project virtualenv. This makes live, potentially billable OpenRouter requests. Each case uses a separate temporary SQLite database; cmdb.db is untouched. Output includes actual ingestion, extracted inventory/detail, replay, and basic field-preservation checks. A passed check is a small smoke test, not complete semantic or vendor compatibility verification.

## Recorded live verification

The initial live run failed all ten fixtures. After structured-pointer guidance,
source-scope guidance, and precise correction hints, all ten have passed their
basic checks across incremental live reruns using `openai/gpt-4o-mini`.
Eight passed the first rerun; Salesforce and GitHub passed targeted follow-ups.
This was not a single ten-case run of the final revision. Actual baseline,
intermediate failures, successful responses, and selected-field checks are in
`docs/unfamiliar-live-results.json`. The GitHub response also contains its
explicit user-to-application `uses` relationship.

These checks verify selected expected fields, AI provenance, and replay. They do
not assert every possible entity/field was extracted or establish general vendor
compatibility. For example, the Jira response retained its application but omitted
the nested project lead, and the Salesforce response omitted the separate usage
count. Broader extraction completeness remains a model-quality limitation.

Rerun selected cases with `--file github_style.json --file salesforce_style.json`.
