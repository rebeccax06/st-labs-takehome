# Manual ingestion test files

All data is synthetic. Existing samples are unchanged. Fixtures intentionally include invalid files.

Upload via `/docs` → `POST /ingest`, selecting the source below and format `auto`. Use named sources for deterministic expectations; `uploaded-data` can route unfamiliar layouts to AI. No database writes or model calls were made when verifying these fixtures.

Example from the project root:

```sh
curl -X POST http://127.0.0.1:8000/ingest -F 'source_id=hardware-json' -F 'file=@input_data/test_files/hardware_valid.json'
```

`manifest.json` records observed preparation results, normalized counts, warning codes, and rejection messages. Accepted preparation does not guarantee persistence success: inspect identity conflicts and unresolved references after ingestion. Invalid uploads should leave no partial writes. AI results depend on configured credentials/model.

For replay testing, upload the same file twice with the same source and format; the second upload should return the same run ID. For a linked scenario upload applications_valid.json, okta_valid.json, then hardware_nested.yaml; the user shares a trusted email, while the flat hardware fixture uses a name-only assignee. Use a clean test database for repeatable results.

| File | Source | Expected | Coverage |
| --- | --- | --- | --- |
| [hardware_valid.json](hardware_valid.json) | `hardware-json` | accepted (2 records, 0 warnings) | IPv4/IPv6, MAC normalization, Unicode, numeric ID, timezone, explicit false |
| [hardware_missing_optional.json](hardware_missing_optional.json) | `hardware-json` | accepted (2 records, 0 warnings) | Absent, null, and blank optional fields |
| [hardware_unknown_values.json](hardware_unknown_values.json) | `hardware-json` | accepted (1 records, 2 warnings) | Unknown OS/status/encryption produce preserved unknowns and warnings |
| [hardware_duplicate_identifiers.json](hardware_duplicate_identifiers.json) | `hardware-json` | accepted (2 records, 0 warnings) | Same identity with contradictory attributes; inspect persisted conflict evidence |
| [hardware_same_hostname.json](hardware_same_hostname.json) | `hardware-json` | accepted (2 records, 0 warnings) | Distinct IDs must not merge solely on hostname |
| [hardware_unresolved_assignee.json](hardware_unresolved_assignee.json) | `hardware-json` | accepted (1 records, 0 warnings) | Name-only assignee may remain unresolved |
| [hardware_invalid_ip.json](hardware_invalid_ip.json) | `hardware-json` | rejected | invalid_ip |
| [hardware_invalid_mac.json](hardware_invalid_mac.json) | `hardware-json` | rejected | invalid_mac |
| [hardware_timestamp_no_timezone.json](hardware_timestamp_no_timezone.json) | `hardware-json` | rejected | timestamp_no_timezone |
| [hardware_invalid_timestamp.json](hardware_invalid_timestamp.json) | `hardware-json` | rejected | invalid_timestamp |
| [hardware_numeric_boolean.json](hardware_numeric_boolean.json) | `hardware-json` | rejected | numeric_boolean |
| [hardware_wrong_hostname_type.json](hardware_wrong_hostname_type.json) | `hardware-json` | rejected | wrong_hostname_type |
| [hardware_boolean_id.json](hardware_boolean_id.json) | `hardware-json` | rejected | boolean_id |
| [hardware_wrong_root.json](hardware_wrong_root.json) | `hardware-json` | rejected | Object instead of required array |
| [hardware_nonobject_row.json](hardware_nonobject_row.json) | `hardware-json` | rejected | Invalid row after valid row; upload should fail atomically |
| [hardware_empty.json](hardware_empty.json) | `hardware-json` | rejected | Empty array rejected by normalized batch validation |
| [okta_valid.json](okta_valid.json) | `okta-export` | accepted (3 records, 0 warnings) | False/true/unknown MFA, suspended users, Unicode, empty lists, unresolved app |
| [okta_duplicate_lists.json](okta_duplicate_lists.json) | `okta-export` | accepted (1 records, 0 warnings) | Duplicate group/access assertions |
| [okta_groups_scalar.json](okta_groups_scalar.json) | `okta-export` | rejected | groups_scalar |
| [okta_blank_app.json](okta_blank_app.json) | `okta-export` | rejected | blank_app |
| [okta_numeric_mfa.json](okta_numeric_mfa.json) | `okta-export` | rejected | numeric_mfa |
| [okta_invalid_login.json](okta_invalid_login.json) | `okta-export` | rejected | invalid_login |
| [applications_valid.json](applications_valid.json) | `application-inventory` | accepted (2 records, 0 warnings) | Zero users, boolean false, integration references and trusted snapshot |
| [applications_warnings.json](applications_warnings.json) | `application-inventory` | accepted (1 records, 4 warnings) | Count mismatch and unknown enum/boolean warnings |
| [applications_negative_count.json](applications_negative_count.json) | `application-inventory` | rejected | Reject invalid user count |
| [applications_fractional_count.json](applications_fractional_count.json) | `application-inventory` | rejected | Reject invalid user count |
| [applications_string_count.json](applications_string_count.json) | `application-inventory` | rejected | Reject invalid user count |
| [applications_boolean_count.json](applications_boolean_count.json) | `application-inventory` | rejected | Reject invalid user count |
| [hardware_nested.yaml](hardware_nested.yaml) | `hardware-yaml` | accepted (2 records, 0 warnings) | Nested device plus embedded user; serial/email identity and snapshot |
| [yaml_duplicate_key.yaml](yaml_duplicate_key.yaml) | `hardware-yaml` | rejected | Duplicate YAML keys |
| [yaml_alias.yaml](yaml_alias.yaml) | `hardware-yaml` | rejected | Reject YAML aliases |
| [yaml_unsafe_tag.yaml](yaml_unsafe_tag.yaml) | `hardware-yaml` | rejected | Reject non-safe YAML tags |
| [json_duplicate_key.json](json_duplicate_key.json) | `hardware-json` | rejected | Duplicate JSON keys |
| [json_truncated.json](json_truncated.json) | `hardware-json` | rejected | Malformed/truncated JSON |
| [json_nonfinite.json](json_nonfinite.json) | `hardware-json` | rejected | Nonfinite JSON number |
| [json_deep_nesting.json](json_deep_nesting.json) | `hardware-json` | rejected | Nesting over depth limit |
| [empty_file.json](empty_file.json) | `hardware-json` | rejected | Empty upload |
| [whitespace_only.json](whitespace_only.json) | `hardware-json` | rejected | Whitespace-only upload |
| [hardware_quoted_bom.csv](hardware_quoted_bom.csv) | `hardware-csv` | accepted (1 records, 0 warnings) | UTF-8 BOM, CRLF, escaped quotes, comma and multiline field |
| [hardware_headers_only.csv](hardware_headers_only.csv) | `hardware-csv` | rejected | Header-only CSV rejected by normalized batch validation |
| [csv_duplicate_headers.csv](csv_duplicate_headers.csv) | `hardware-csv` | rejected | duplicate_headers |
| [csv_blank_header.csv](csv_blank_header.csv) | `hardware-csv` | rejected | blank_header |
| [csv_short_row.csv](csv_short_row.csv) | `hardware-csv` | rejected | short_row |
| [csv_long_row.csv](csv_long_row.csv) | `hardware-csv` | rejected | long_row |
| [csv_unterminated_quote.csv](csv_unterminated_quote.csv) | `hardware-csv` | rejected | unterminated_quote |
| [csv_missing_required_header.csv](csv_missing_required_header.csv) | `hardware-csv` | rejected | missing_required_header |
| [hardware_1000_records.json](hardware_1000_records.json) | `hardware-json` | accepted (1000 records, 0 warnings) | 1000 records: boundary of normalized record limit |
| [hardware_1001_records.json](hardware_1001_records.json) | `hardware-json` | rejected | 1001 records: boundary of normalized record limit |
| [okta_5000_relationships.json](okta_5000_relationships.json) | `okta-export` | accepted (1 records, 0 warnings) | 5000 relationships: boundary of relationship limit |
| [okta_5001_relationships.json](okta_5001_relationships.json) | `okta-export` | rejected | 5001 relationships: boundary of relationship limit |
| [upload_over_1mib.json](upload_over_1mib.json) | `hardware-json` | rejected | Upload exceeds 1 MiB |
| [ai_inventory_notes.txt](ai_inventory_notes.txt) | `uploaded-data` | Requires configured AI extraction; model-dependent results | Unstructured prose containing device, user, app, and relationships |
