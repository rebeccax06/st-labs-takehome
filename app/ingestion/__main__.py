"""Print deterministic preparation results: python -m app.ingestion."""

import argparse
import json
from pathlib import Path
import sys

from pydantic import Field, TypeAdapter, ValidationError

from app.ingestion.errors import InputError
from app.ingestion.service import ADAPTERS, prepare_ingestion
from app.schemas.ingest import IngestRequest, SourceConfig
from app.schemas.normalized import Contract

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class SampleInput(Contract):
    file: str = Field(pattern=r"^[^/\\]+\.(json|csv|yaml|yml)$")
    source: SourceConfig


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Normalize inventory files and print JSON; no database writes or AI calls."
    )
    parser.add_argument("--file", action="append", dest="files", metavar="NAME",
                        help="Process a configured filename (repeat to select several; default: all).")
    parser.add_argument("--input-dir", type=Path, default=PROJECT_ROOT / "input_data",
                        help="Input directory (default: project's input_data).")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "examples/sample_sources.json",
                        help="JSON list of file/source configurations (default: supplied sample profile).")
    parser.add_argument("--include-raw", action="store_true",
                        help="Include original parsed records alongside normalized data.")
    args = parser.parse_args(argv)
    try:
        configured = TypeAdapter(list[SampleInput]).validate_json(args.config.read_text(encoding="utf-8"))
        if not configured:
            raise ValueError("Configuration must contain at least one input file")
        names = [entry.file for entry in configured]
        if len(names) != len(set(names)):
            raise ValueError("Configuration contains duplicate filenames")
        if args.files and set(args.files) - set(names):
            parser.error("Unknown filename; select a file listed in the source configuration")
        sources = [entry.source for entry in configured]
        results = []
        records = relationships = warnings = 0
        for entry in configured:
            if args.files and entry.file not in args.files:
                continue
            adapter = ADAPTERS.get(entry.source.adapter)
            if adapter is None:
                raise ValueError(f"No deterministic adapter configured for {entry.file}")
            request = IngestRequest(
                organization_id=entry.source.organization_id, source_id=entry.source.source_id,
                format=adapter[0], content=(args.input_dir / entry.file).read_text(encoding="utf-8"),
            )
            prepared = prepare_ingestion(request, sources)
            output = prepared.model_dump(mode="json")
            if not args.include_raw:
                for record in output["batch"]["records"]:
                    record.pop("raw_record", None)
            results.append({"file": entry.file, "output": output})
            records += len(prepared.batch.records)
            relationships += sum(len(record.relationships) for record in prepared.batch.records)
            warnings += prepared.warning_count
        # Prepare everything before printing, so failures never leave partial JSON.
        print(json.dumps({"summary": {"files": len(results), "records": records,
                                     "relationship_assertions": relationships, "warnings": warnings},
                          "results": results}, indent=2, ensure_ascii=False))
        return 0
    except ValidationError:
        print("Error: invalid input/source configuration; check field types and required fields.", file=sys.stderr)
    except InputError as exc:
        print(f"Error [{exc.code}] at {exc.path}: {exc}", file=sys.stderr)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
