"""Run with python3 -m change_intelligence ingest|prepare|submit."""

import argparse
import json
import os
import sys
from pathlib import Path

from .analysis import Analyzer, VERSION
from .change_request import assessment_path, build_payload, load_release, markdown, submit
from .common import Failure, json_text, now, required_env
from .git_source import GitSource, ZERO
from .store import GitHubStore


def store_from_env():
    return GitHubStore(required_env("GITHUB_REPOSITORY"), required_env("GITHUB_TOKEN"),
                       os.environ.get("GITHUB_API_URL", "https://api.github.com"),
                       default_branch=os.environ.get("CHANGE_DEFAULT_BRANCH", "main"))


def pipeline_url():
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    return f"{server}/{required_env('GITHUB_REPOSITORY')}/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}"


def write_output(name, value):
    directory = Path(os.environ.get("CHANGE_OUTPUT_DIR", ".change-output"))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(value, encoding="utf-8")


def summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as output:
            output.write(text + "\n")


def ingest(source, store, base, target, analyzer_factory=None):
    shas = source.transitions(base, target)
    store.ensure_branch()
    failures, results = [], []
    for sha in shas:
        previous, _ = store.read(assessment_path(sha))
        if previous and previous.get("status") == "completed":
            if (previous.get("sha") != sha or previous.get("repository") != store.repository or
                    previous.get("base_sha") != source.parent(sha) or previous.get("schema_version") != VERSION):
                raise Failure(f"Stored assessment identity/version mismatch for {sha}")
            results.append({"sha": sha, "status": "reused"})
            continue
        record = {"schema_version": VERSION, "repository": store.repository, "sha": sha,
                  "base_sha": source.parent(sha), "created_at": now(),
                  "pipeline_url": pipeline_url() if os.environ.get("GITHUB_REPOSITORY") else ""}
        try:
            evidence = source.evidence(sha)
            record.update({key: evidence[key] for key in ("title", "files")})
            record["pull_requests"] = store.pull_requests(sha)
            analyzer = analyzer_factory() if analyzer_factory else Analyzer(
                required_env("CURSOR_API_KEY"), os.environ.get("CURSOR_MODEL", "composer-2.5").strip() or "composer-2.5")
            record["assessment"] = analyzer.analyze(evidence)
            record.update(status="completed", model=analyzer.model)
        except Failure as exc:
            record.update(status="failed", error=str(exc))
        persisted = store.save(assessment_path(sha), record)
        results.append({"sha": sha, "status": persisted["status"]})
        if persisted["status"] != "completed":
            failures.append(sha)
    write_output("ingestion.json", json_text(results))
    summary("## Application change ingestion\n\n" + "\n".join(
        f"- `{item['sha']}`: {item['status']}" for item in results))
    if failures:
        raise Failure("Ingestion failed; inspect saved failure records and rerun: " + ", ".join(failures))
    print(f"Ingestion complete: {len(results)} commit assessments saved or reused")


def ingestion_range():
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    if event_name == "push":
        event = json.loads(Path(required_env("GITHUB_EVENT_PATH")).read_text())
        branch = os.environ.get("CHANGE_DEFAULT_BRANCH", "main")
        if event.get("ref") != f"refs/heads/{branch}" or event.get("deleted"):
            raise Failure("Ingestion only accepts pushes to the configured default branch")
        if event.get("forced"):
            raise Failure("Force-push ingestion needs an explicit reviewed baseline/backfill")
        return event.get("before") or ZERO, event["after"]
    return required_env("BASE_SHA"), required_env("TARGET_SHA")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("ingest", "prepare", "submit"))
    args = parser.parse_args(argv)
    try:
        if args.command == "ingest":
            base, target = ingestion_range()
            source = GitSource()
            source.require_branch(target, os.environ.get("CHANGE_DEFAULT_BRANCH", "main"))
            ingest(source, store_from_env(), base, target)
        elif args.command == "prepare":
            source, store = GitSource(), store_from_env()
            base, target = required_env("BASE_SHA"), required_env("TARGET_SHA")
            source.require_branch(target, os.environ.get("CHANGE_DEFAULT_BRANCH", "main"))
            records = load_release(source, store, base, target)
            payload = build_payload(store.repository, base, target, records,
                service=required_env("CHANGE_SERVICE"), environment=os.environ.get("CHANGE_ENVIRONMENT", ""),
                title=required_env("CHANGE_TITLE"), author=os.environ.get("GITHUB_ACTOR", ""),
                pipeline_url=pipeline_url(), sbom_url=os.environ.get("SBOM_URL", ""),
                attestation_url=os.environ.get("ATTESTATION_URL", ""), image_digest=os.environ.get("IMAGE_DIGEST", ""))
            write_output("change-request.json", json_text(payload))
            write_output("change-request.md", payload["summary"])
            summary(payload["summary"])
            print(f"Prepared Change Request from {len(records)} saved assessments")
        else:
            path = Path(os.environ.get("CHANGE_OUTPUT_DIR", ".change-output")) / "change-request.json"
            payload = json.loads(path.read_text())
            response = submit(payload, required_env("SETUOPS_API_URL"), required_env("CHANGE_WEBHOOK_SECRET"))
            write_output("setuops-response.json", json_text(response))
            summary(f"\nSetuOps Change Request: **{markdown(response['change_number'])}**\n")
            print(f"SetuOps Change Request recorded: {response['change_number']}")
    except (Failure, OSError, ValueError, KeyError) as exc:
        # Do not dump remote bodies, diffs, tokens, or tracebacks to Actions logs.
        message = str(exc) if isinstance(exc, Failure) else f"Invalid input or local data ({type(exc).__name__})"
        print(f"Error: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
