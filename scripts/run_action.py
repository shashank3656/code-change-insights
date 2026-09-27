"""Run trusted action code against the caller's checkout without importing it.

Invoked with python -I: ignore caller PYTHONPATH, site packages, and working-dir
modules. Only the downloaded action's package is added to the import path.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from change_intelligence.__main__ import main as run_command
from change_intelligence.common import Failure, required_env


def output(name, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        delimiter = "insights_" + uuid.uuid4().hex
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def run():
    original_directory = Path.cwd()
    try:
        operation = required_env("INSIGHTS_OPERATION")
        if operation not in ("ingest", "change-request"):
            raise Failure("operation must be ingest or change-request")
        submit = os.environ.get("INSIGHTS_SUBMIT", "false").lower()
        if submit not in ("true", "false"):
            raise Failure("submit must be true or false")
        if operation == "ingest" and submit == "true":
            raise Failure("Ingestion cannot submit a Change Request")
        branch = required_env("CHANGE_DEFAULT_BRANCH")
        if os.environ.get("GITHUB_REF") != f"refs/heads/{branch}":
            raise Failure("Run this action from the configured default branch")
        workspace = Path(required_env("GITHUB_WORKSPACE")).resolve()
        source = (workspace / os.environ.get("INSIGHTS_SOURCE_DIRECTORY", ".")).resolve()
        if not source.is_relative_to(workspace) or not source.is_dir():
            raise Failure("source-directory must be a directory inside the caller's workspace")
        # The caller checkout supplies data, not Python modules or output files.
        # Use a fresh runner temp directory, outside the checkout, for reports.
        directory = Path(tempfile.mkdtemp(prefix="code-change-insights-", dir=required_env("RUNNER_TEMP")))
        os.environ["CHANGE_OUTPUT_DIR"] = str(directory)
        output("artifact-directory", directory)
        os.chdir(source)
        commands = ["ingest"] if operation == "ingest" else ["prepare"]
        if operation == "change-request" and submit == "true":
            commands.append("submit")
        for command in commands:
            result = run_command([command])
            if result:
                return result
        if operation == "ingest":
            output("assessment-count", len(json.loads((directory / "ingestion.json").read_text())))
        else:
            payload = json.loads((directory / "change-request.json").read_text())
            output("risk-level", payload["risk_level"])
            if submit == "true":
                record = json.loads((directory / "setuops-response.json").read_text())
                output("change-number", record["change_number"])
                output("change-id", record["id"])
        return 0
    except (Failure, OSError, ValueError, KeyError) as exc:
        message = str(exc) if isinstance(exc, Failure) else f"Invalid action input or local data ({type(exc).__name__})"
        print(f"Error: {message}", file=sys.stderr)
        return 1
    finally:
        os.chdir(original_directory)


if __name__ == "__main__":
    sys.exit(run())
