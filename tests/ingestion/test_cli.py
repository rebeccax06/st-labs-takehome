"""Exercise the actual command, including JSON stdout and failure exit codes."""

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def command(*args):
    return subprocess.run([sys.executable, "-m", "app.ingestion", *args], cwd=ROOT,
                          capture_output=True, text=True, check=False)


def test_all_sample_files_print_normalized_json():
    result = command()
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["summary"] == {"files": 5, "records": 27, "relationship_assertions": 50, "warnings": 0}
    assert len(output["results"]) == 5
    for entry in output["results"]:
        assert entry["output"]["batch"]["records"]
        assert all("raw_record" not in row for row in entry["output"]["batch"]["records"])


def test_selected_file_can_include_raw_evidence():
    result = command("--file", "sample_hardware.yaml", "--include-raw")
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output["summary"]["files"] == 1
    assert output["summary"]["records"] == 6
    rows = output["results"][0]["output"]["batch"]["records"]
    device = next(row for row in rows if row["ci_type"] == "device")
    assert device["attributes"]["status"] == "inactive"
    assert device["raw_record"]["status"]["operational_status"] == "deactivated"


def test_invalid_input_fails_without_partial_output(tmp_path):
    (tmp_path / "sample_hardware.json").write_text('not json')
    result = command("--file", "sample_hardware.json", "--input-dir", str(tmp_path))
    assert result.returncode == 1
    assert result.stdout == ""
    assert "Malformed input" in result.stderr


def test_unknown_filename_reports_usage_error():
    result = command("--file", "unknown.json")
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Unknown filename" in result.stderr
