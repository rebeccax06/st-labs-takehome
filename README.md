# AI-first CMDB

A Python backend that turns IT inventory exports into connected, queryable records for devices, users, and applications. It accepts JSON, CSV, YAML, and text, keeps the evidence behind each fact, and answers natural-language questions through validated, read-only query plans.

The backend only links records when their identifiers support the match. This matters when combining inventories: the same name may refer to different people, and a missing MFA value cannot tell us whether MFA is disabled. Uncertain matches and conflicting facts are kept for inspection.

## Setup

Use Python **3.12+** from the project root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.demo
```

The demo needs no API key or running server. It uploads all five supplied files, checks replay, and answers seven example questions through the API using a temporary SQLite database. It leaves your local database untouched.

**The offline demo uses prewritten query plans.** It runs the uploads and queries through the real backend, but replaces the model responses so you can try it without credentials.

To use the API yourself, start the server:

```sh
# Start the local API server; reload automatically when code changes.
python -m uvicorn app.main:app --reload
```

**Next, open [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) in your browser.** This is the interactive interface where you upload files, inspect records, and ask questions. Include `/docs`; the base address printed by Uvicorn has no homepage.

Leave the terminal running while you use the browser. Press `Ctrl+C` to stop the server. Startup creates missing tables in `sqlite:///cmdb.db` and retains existing data. It does not migrate an older schema. Activate the virtual environment again when opening a new terminal.

## Upload and inspect data in your browser

At [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs):

1. Expand **POST /ingest** and click **Try it out**.
2. Click **Choose File** and select `input_data/sample_hardware.yaml` from this project.
3. Select **hardware-yaml** for `source_id`, leave `format` as **auto**, and click **Execute**.
4. Look under **Server response → Response body** for the upload summary. `entities.created` shows how many new items were saved; uploading the same file again returns `replayed: true`.
5. Expand **GET /devices**, click **Try it out**, then **Execute** to see the saved devices. You can leave the filters at their defaults.

To inspect one device, copy its `id` from that response and paste it into **GET /ci/{ci_id}**. This shows its attributes, relationships, and supporting evidence. Use the item's `id`, not the upload's `run_id`.

You can follow the same steps with `sample_okta.json` using **okta-export**, then view **GET /users**; use `sample_app.json` with **application-inventory**, then view **GET /apps**. To paste text instead of choosing a file, use **POST /ingest/raw**.

Optional: make the same requests with curl

Keep the server running and run these commands in a second terminal from the project root:

```sh
curl http://127.0.0.1:8000/ingest \
  -F 'source_id=hardware-yaml' \
  -F 'file=@input_data/sample_hardware.yaml'

curl 'http://127.0.0.1:8000/ingest/raw?source_id=hardware-csv' \
  -H 'Content-Type: text/plain' \
  --data-binary @input_data/sample_hardware.csv

curl 'http://127.0.0.1:8000/devices?os_family=macos'
curl 'http://127.0.0.1:8000/users?mfa_enabled=false'
```

`GET /ingest/sources` lists source choices. The default `uploaded-data` source recognizes familiar layouts and uses AI for unfamiliar ones. Repeating the same upload under the same source and configuration returns the previous run without creating duplicates or calling the provider again.

An upload can succeed with warnings. For example, a device with a name-only assignee is saved, but no assignment link is created unless a stable identifier identifies the user unambiguously. The backend returns an `unresolved_reference` warning and retains the original assignment as evidence. These warnings come from validation and identity-resolution rules.

Each inventory system needs its own source configuration. An ID such as `123` in Jamf does not mean the same thing as `123` in Okta. Cross-source matching requires a shared identifier authority configured in [sample_sources.json](examples/sample_sources.json); names alone never merge records.

### Configure AI and ask a question

Known-source ingestion, inventory reads, tests, and the offline demo need no credentials. For unfamiliar extraction or `/ask` requests, stop the server with `Ctrl+C`, then configure credentials in the same terminal:

```sh
cp .env.example .env
# Edit .env: uncomment and set CMDB_OPENROUTER_API_KEY.
set -a
source .env
set +a
python -m uvicorn app.main:app --reload
```

The app does not load `.env` automatically. Load it in each new shell and restart the server after changing settings. Credentials resolve from `CMDB_OPENROUTER_API_KEY`, then `OPENROUTER_API_KEY`, then `CMDB_OPENROUTER_TOKEN_PATH` (default `token.txt`). Keep credentials private; these files are ignored by git.

The default model is `openai/gpt-4o-mini`; set `CMDB_OPENROUTER_MODEL` to change it. Database, source configuration, timeout, and output-token settings are described in [.env.example](.env.example) and [Settings](app/config.py). The normal service has no mock fallback. Invalid model output gets one corrective attempt, then a sanitized error.

Return to [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) after restarting the server:

1. If you haven't loaded users yet, upload `input_data/sample_okta.json` through **POST /ingest** with source **okta-export**.
2. Expand **POST /ask** and click **Try it out**.
3. Replace the request body with:

```json
{"question":"Which users do not have MFA?","limit":50,"offset":0}
```

1. Click **Execute** and read the answer under **Server response → Response body**.

Optional: ask from a second terminal with curl

```sh
curl http://127.0.0.1:8000/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"Which users do not have MFA?","limit":50,"offset":0}'
```

Each response shows the answer, matching records, and the query plan used to find them. Warnings explain any missing or conflicting data that could affect the result. Count questions return `total` with `data: null`. Ambiguous names return `clarification_required` with candidate IDs; unsupported questions return `unsupported`. Names are matched without regard to capitalization, and configured app aliases are supported. Typos and approximate names will not match.

With the sample files, device/MFA questions find no matches because the hardware and Okta records lack reliable shared user identifiers. An application integrating with Salesforce also does not establish a dependency on Salesforce. An empty result here means the files do not establish the relationship.

## How it works

On upload, an adapter or the AI turns the input into device, user, and application records. The backend checks those records, matches them to existing items, and handles conflicting facts before saving the changes in one transaction. For questions, the AI proposes filters and relationships to follow. The backend checks that plan, runs the query, and builds the answer from the results.

**Data model.** Every item has a UUID in `configuration_items`, with its attributes in a separate device, user, or application table. Relationships connect the items: a device can be assigned to a user, and that user can use an application. Keeping these links separate lets queries follow them without copying application lists into every user record. The database also stores external IDs, source observations, and upload history so a fact can be traced back to the file that supplied it. API responses show the saved fields and supporting evidence without returning full source records.

**Matching records and handling conflicts.** External IDs include the source authority and entity type, so an ID from one system cannot accidentally match an unrelated ID from another. If a record's identifiers point to different items, it is set aside for review. For conflicting attributes, a configured trusted source takes priority. Otherwise, the backend uses the latest observation when both timestamps are reliable and comparable. Uploading a file later does not make its values more trustworthy. If neither rule settles the conflict, the returned value is `null` and both reports are kept. A blank field in a later upload does not erase a known value.

**Database choice.** I chose SQLite so the project runs locally without a database service. A relational schema fits the identity constraints and the short queries across devices, users, and applications. SQLAlchemy handles those queries, while foreign keys and uniqueness constraints protect the stored relationships. SQLite allows one writer at a time; short `BEGIN IMMEDIATE` transactions keep identity lookup and saving together. A busier deployment would need PostgreSQL, along with migrations and tests for its locking and constraints. The current queries don't need the deeper traversal that would justify a graph database.

**Where AI helps.** Known file layouts have dedicated parsers. For unfamiliar files, OpenRouter proposes records and cites the input behind each field. The backend checks those citations before saving. When you ask a question, the model receives the question and a list of supported fields and relationships. The backend then runs the approved plan against the database and writes the answer from the results. Stored database records are never sent to the model.

The query code uses fixed tables and bound parameters, and supports up to two relationship steps. For a question about a device's owner who uses an app, both conditions must apply to the same owner. The model cannot run SQL or decide how source IDs are shared between systems.

FastAPI defines the endpoints, and Pydantic validates requests and extracted records. Synchronous services keep transactions straightforward for this prototype; larger imports would need background jobs. Validation catches malformed output, but a model can still misunderstand a question. The response includes its plan and any warnings so you can check how it reached the answer.

See [the technical design](docs/design.md) for schema details and API contracts, or [sample responses](docs/demo-output.json) for the demo output.

## Live AI verification

Optional checks below make real provider requests and may incur charges. Both demos and the unfamiliar-fixture check use temporary databases:

```sh
python -m app.ai --live-check
python -m app.demo --live --json
python -m examples.check_unfamiliar
```

This check uploads ten synthetic vendor-style files and checks selected fields, relationships, and repeat uploads. The `ai_provenance` result checks whether at least one saved observation is marked as AI-extracted. Other exports may need different mappings or prompt changes. To run one file, use `--file jamf_style.json`.

Live verification with the default model passed all ten structured extraction fixtures and six of seven example questions. The two-hop question failed plan validation in the full run but produced a valid plan after correction in a later isolated check. The prose inventory fixture repeatedly failed evidence validation. Invalid output was rejected safely. The offline suite verifies the underlying two-hop execution and text-evidence contracts, but live interpretation remains a limitation.

## API and verification

- `POST /ingest`: multipart file, direct body, or JSON envelope.
- `POST /ingest/raw`: paste JSON, CSV, YAML, or notes as plain text.
- `GET /ingest/sources`: configured sources and accepted formats.
- `GET /devices`, `/users`, `/apps`: typed filters and pagination; default 50 records, maximum 100, maximum offset 10,000.
- `GET /ci/{id}`: attributes, identifiers, conflicts, relationships, and bounded provenance.
- `POST /ask/plan`: preview a validated plan.
- `POST /ask`: plan and execute a bounded read query.

Uploads accept UTF-8 JSON, CSV, YAML, or text up to 1 MiB, with at most 1,000 normalized records and 5,000 relationship assertions. AI extraction has a smaller 64 KiB input limit. PDF, Excel, and images are unsupported. Query plans allow two relationship steps and up to 100 distinct literal references. Counts and coverage checks can still scan a large dataset.

The generated `/docs` and `/openapi.json` provide request and response schemas. Invalid input returns 422, exceeded limits 413, and unsupported binary/encoding input 415. Provider errors return 502, unavailability 503, and timeouts 504. Error responses include matching body/header request IDs without echoing source content or credentials.

The offline suite includes HTTP checks for all 51 deterministic fixtures in [the test files](input_data/test_files/README.md), including replay and checks that rejected uploads leave no data behind. It also tests all supplied layouts, transport variants, malformed and extreme input, identity conflicts, evidence policy, atomic rollback, SQLite locking, concurrent replay, provider failures, unsafe query plans, and grounded answers. Integration tests use temporary databases and block real HTTP:

```sh
python -m pytest -q
python -m pytest tests/integration -q
python -m app.ingestion --file sample_hardware.yaml  # Normalize without saving
python -m app.querying --question "Which users don't have MFA?" --plan-only
```

The query CLI uses the configured database and live planner. The offline demo does not populate that database. Dependencies have compatible version ranges rather than a complete lock; Python 3.12 is the intended minimum, while the recorded development verification used Python 3.14.2.

## Limitations and next steps

The prototype covers uploading inventory, matching records, keeping evidence, and querying relationships. A few implementation choices leave work for a fuller version:

- **Source settings need careful review.** Someone has to decide which IDs mean the same thing across systems and which source to trust when values disagree. The backend follows those settings, so a mistake there can lead to an incorrect match even if the uploaded records pass validation. I would add checks and a preview of affected matches before applying policy changes.
- **Identity review is manual.** Matching uses configured identifiers. Conflicting identities are kept for review, but there is no screen or endpoint to resolve them or merge duplicate items. I would add a review workflow with a record of who approved each change.
- **Later uploads do not remove old access.** App access, groups, and integrations accumulate as evidence. If an export stops listing an assignment, the stored link remains. Supporting removal would require each source to distinguish a complete snapshot from a partial update.
- **AI interpretation is inconsistent.** Validation rejects malformed plans and unsupported evidence, but cannot make the model understand every question or note. A valid quote or field pointer shows where a value came from; it does not prove the model assigned it to the right person, device, or application. I would compare models against repeatable extraction and question tests, especially the failing prose and two-hop cases, before expanding support.
- **Queries cover a small set of operations.** They can list or count items using supported filters and relationships. Historical questions, date comparisons, and broader reporting would need additions to the plan schema and compiler, with tests for their meaning.
- **Imports run during the request.** Synchronous services and SQLite keep setup and transactions simple, but writes are serialized and a slow provider keeps the request waiting. Larger deployments would need background imports and PostgreSQL, including migrations and concurrency tests.
- **There are no access controls.** The service assumes one organization and has no authentication or user permissions. Those would be required before exposing real inventory to multiple users.

I would address the live AI failures and identity-review workflow first. Before deployment, I would add access controls, backup and restore procedures, evidence-retention rules, and monitoring for failed imports, conflicts, database locks, and provider cost.

## AI-assisted development

Codex assisted with design, implementation, tests, and documentation, guided by me. The [coding session transcript](docs/codex-session.md) records that work. Runtime prompts are available in [extraction.md](app/ai/prompts/extraction.md) and [planning.md](app/querying/prompts/planning.md). The [assignment](STLabs_Backend_TakeHome_CMDB_Junior.md) provides the original requirements.