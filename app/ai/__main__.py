"""Explicit live extraction smoke test; no database writes and no secret output."""

import argparse
import json
import sys

from app.ai.client import AIError, OpenRouterClient
from app.ai.extraction import Extractor
from app.config import Settings
from app.ingestion.errors import InputError
from app.ingestion.service import prepare_ingestion
from app.schemas.ingest import IngestRequest, SourceConfig


SMOKE_TEXT = (
    "Inventory: device ID CMDB-SMOKE-17; hostname smoke-laptop; "
    "operating system macOS; status active."
)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Test OpenRouter extraction on synthetic data without writing to the CMDB.")
    parser.add_argument("--live-check", action="store_true", help="Make a small paid model request using configured credentials.")
    args = parser.parse_args(argv)
    if not args.live_check:
        parser.print_help()
        return 0
    settings = Settings.from_env()
    source = SourceConfig(organization_id="smoke-check", source_id="phase4-smoke", adapter="auto")
    request = IngestRequest(organization_id=source.organization_id, source_id=source.source_id,
                            format="text", content=SMOKE_TEXT)
    try:
        result = prepare_ingestion(request, [source], extractor=Extractor(OpenRouterClient(settings)))
        if not any(row.ci_type == "device" and row.attributes.hostname == "smoke-laptop"
                   and any(i.value == "CMDB-SMOKE-17" for i in row.identifiers) for row in result.batch.records):
            raise AIError("Live extraction did not return the expected synthetic device.")
    except (AIError, InputError) as exc:
        print(json.dumps(dict(status="failed", code=exc.code, message=str(exc))), file=sys.stderr)
        return 1
    print(json.dumps(dict(status="ok", model=settings.openrouter_model,
                         records=len(result.batch.records), origin=result.origin)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
