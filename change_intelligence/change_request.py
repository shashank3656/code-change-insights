"""Build a release assessment using saved results, then deliver it to SetuOps."""

import hashlib
import html
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .analysis import VERSION, risk_level, validate
from .common import Failure, request_json
from .git_source import ZERO


def markdown(value):
    # Model/commit text cannot create links, images, HTML, or other active markup.
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", html.escape(str(value)))


def assessment_path(sha):
    return f"assessments/{sha}.json"


def load_release(source, store, base, target):
    if base == ZERO:
        raise Failure("A Change Request requires the actual previously deployed SHA")
    shas = source.transitions(base, target)
    if not shas:
        raise Failure("No changes between the selected deployment revisions")
    records, missing = [], []
    for sha in shas:
        record, _ = store.read(assessment_path(sha))
        if not record or record.get("status") != "completed":
            missing.append(sha)
            continue
        if (record.get("repository") != store.repository or record.get("sha") != sha or
                record.get("base_sha") != source.parent(sha) or record.get("schema_version") != VERSION):
            raise Failure(f"Stored assessment identity/version mismatch for {sha}")
        validate(record["assessment"])
        if record.get("files") != source.files(source.parent(sha), sha):
            raise Failure(f"Stored assessment file coverage mismatch for {sha}")
        records.append(record)
    if missing:
        raise Failure("Missing or failed assessments; rerun/backfill ingestion for: " + ", ".join(missing))
    return records


def render_release(repository, base, target, records):
    risk = risk_level(record["assessment"]["risk_level"] for record in records)
    lines = ["# Application change assessment", "", f"Repository: {markdown(repository)}",
             f"Range: `{base}` → `{target}`", f"Overall assessed risk: **{risk.upper()}**", "",
             "AI assessment for human review. Recommended checks below are not executed test results.", ""]
    if risk == "unknown":
        lines += ["Incomplete evidence: sent to SetuOps as HIGH risk for manual review.", ""]
    for record in records:
        assessment = record["assessment"]
        lines += [f"## {markdown(record['title'])} ({record['sha'][:12]})", "",
                  markdown(assessment["summary"]), "", f"Risk: **{assessment['risk_level'].upper()}**", ""]
        for field, label in (("changes", "Changes"), ("testing", "Recommended validation"),
                             ("rollback", "Rollback considerations"), ("limitations", "Limitations")):
            if assessment[field]:
                lines += [f"### {label}", ""] + [f"- {markdown(item)}" for item in assessment[field]] + [""]
        if assessment["risk_factors"]:
            lines += ["### Risk factors", ""]
            for factor in assessment["risk_factors"]:
                paths = ", ".join(factor["files"])
                lines.append(f"- **{factor['level'].upper()}**: {markdown(factor['reason'])} ({markdown(paths)})")
            lines.append("")
        if record.get("pull_requests"):
            lines += ["PRs: " + ", ".join(f"#{pr['number']}" for pr in record["pull_requests"]), ""]
    return "\n".join(lines).rstrip() + "\n"


def build_payload(repository, base, target, records, *, service, environment, title,
                  author="", pipeline_url="", sbom_url="", attestation_url="", image_digest=""):
    if not service.strip() or not title.strip():
        raise Failure("Service and Change Request title are required")
    assessed_risk = risk_level(record["assessment"]["risk_level"] for record in records)
    summary = render_release(repository, base, target, records)
    concise = " ".join(record["assessment"]["summary"].strip() for record in records)
    setuops_summary = f"{concise} Overall assessed risk: {assessed_risk.upper()}."
    if len(setuops_summary) > 1800:
        setuops_summary = setuops_summary[:1797].rstrip() + "..."
    prs = {pr["number"]: pr for r in records for pr in r.get("pull_requests", [])}
    single_pr = next(iter(prs.values())) if len(prs) == 1 else {}
    factors = [f"[{f['level']}] {f['reason']} ({', '.join(f['files'])})"
               for r in records for f in r["assessment"]["risk_factors"]]
    limitations = [item for r in records for item in r["assessment"]["limitations"]]
    rollback = [item for r in records for item in r["assessment"]["rollback"]]
    payload = {
        "repo": repository, "sha": target, "previous_sha": base,
        "service": service.strip(), "title": title.strip(),
        "author": author, "summary": summary, "setuops_summary": setuops_summary,
        "files": sorted({path for r in records for path in r["files"]}),
        "pr_number": single_pr.get("number", 0), "pr_url": single_pr.get("url", ""),
        "pipeline_url": pipeline_url, "change_type": "standard", "watch_minutes": 120,
        "risk_level": "high" if assessed_risk == "unknown" else assessed_risk,
        "priority": "medium", "impact": "\n".join(factors + limitations),
        "backout_plan": "\n".join(rollback), "sbom_url": sbom_url,
        "attestation_url": attestation_url, "image_digest": image_digest,
    }
    if environment.strip():
        payload["environment"] = environment.strip()
    if len(json.dumps(payload).encode()) > 750_000:
        raise Failure("Release assessment is too large for a single Change Request; split the release")
    return payload


def endpoint_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or
            parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", "/api/webhooks/change")):
        raise Failure("SETUOPS_API_URL must be an HTTPS origin or the /api/webhooks/change endpoint")
    return urlunsplit((parsed.scheme, parsed.netloc, "/api/webhooks/change", "", ""))


def submit(payload, api_url, secret):
    if not secret:
        raise Failure("Configure CHANGE_WEBHOOK_SECRET before submitting")
    # Keep the artifact rich, but send only the current SetuOps webhook contract.
    delivery = {key: payload[key] for key in (
        "repo", "sha", "service", "title", "author", "pipeline_url",
        "change_type", "watch_minutes",
    )}
    delivery["summary"] = payload["setuops_summary"]
    for key in ("sbom_url", "attestation_url", "image_digest"):
        if payload.get(key):
            delivery[key] = payload[key]
    identity = "\n".join((payload["repo"], payload["sha"]))
    response = request_json("POST", endpoint_url(api_url), {
        "X-CI-Secret": secret,
        "Idempotency-Key": hashlib.sha256(identity.encode()).hexdigest(),
    }, delivery, attempts=1)
    # No automatic POST retries: the current SetuOps implementation does not
    # promise atomic idempotency for concurrent inserts. Reruns are deliberate.
    if (response.get("status") != "ok" or type(response.get("id")) is not int or response["id"] <= 0 or
            not isinstance(response.get("change_number"), str) or
            not re.fullmatch(r"CHG[0-9]+", response["change_number"])):
        raise Failure("SetuOps returned an unexpected response; check its records before retrying")
    return {key: response[key] for key in ("id", "change_number", "status")}
