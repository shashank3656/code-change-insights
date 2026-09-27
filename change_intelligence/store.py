"""Durable JSON records on an orphan branch of the application repository."""

import base64
import json
import re
import time
from urllib.parse import quote, urlencode

from .common import Failure, HTTPFailure, json_text, request_json

BRANCH = "change-intelligence"


class GitHubStore:
    def __init__(self, repository, token, api_url="https://api.github.com", default_branch="main"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise Failure("GITHUB_REPOSITORY must be owner/repository")
        self.repository = repository
        self.default_branch = default_branch
        self.root = f"{api_url.rstrip('/')}/repos/{repository}"
        self.headers = {"Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2022-11-28"}

    def call(self, method, path, body=None):
        return request_json(method, self.root + path, self.headers, body)

    def ensure_branch(self):
        try:
            self.call("GET", f"/git/ref/heads/{BRANCH}")
            return
        except HTTPFailure as exc:
            if exc.status != 404:
                raise
        tree = self.call("POST", "/git/trees", {"tree": [{
            "path": "README.md", "mode": "100644", "type": "blob",
            "content": "# Application change intelligence\n\nGenerated assessments. Do not merge this branch into main.\n",
        }]})
        commit = self.call("POST", "/git/commits", {
            "message": "Initialize application change intelligence storage",
            "tree": tree["sha"], "parents": [],
        })
        try:
            self.call("POST", "/git/refs", {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]})
        except HTTPFailure as exc:
            if exc.status not in (409, 422):
                raise
            # Another ingestion run may have created it concurrently.
            self.call("GET", f"/git/ref/heads/{BRANCH}")

    def read(self, path):
        try:
            item = self.call("GET", f"/contents/{quote(path, safe='/')}?{urlencode({'ref': BRANCH})}")
        except HTTPFailure as exc:
            if exc.status == 404:
                return None, None
            raise
        if item.get("encoding") != "base64":
            raise Failure("Stored assessment is too large or has an unsupported encoding")
        try:
            return json.loads(base64.b64decode(item["content"])), item["sha"]
        except (ValueError, KeyError):
            raise Failure("Stored assessment is corrupt") from None

    def save(self, path, record):
        encoded = base64.b64encode(json_text(record).encode()).decode()
        if len(encoded) > 900_000:
            raise Failure("Assessment exceeds storage size limit")
        for attempt in range(5):
            previous, blob = self.read(path)
            if previous and previous.get("status") == "completed":
                # Completed assessments are immutable, including on retry races.
                return previous
            body = {"message": f"Record analysis {record['sha'][:12]}", "branch": BRANCH,
                    "content": encoded}
            if blob:
                body["sha"] = blob
            try:
                self.call("PUT", f"/contents/{quote(path, safe='/')}", body)
                return record
            except HTTPFailure as exc:
                if exc.status not in (409, 422) or attempt == 4:
                    raise
                time.sleep(attempt + 1)
        raise Failure("Concurrent storage updates could not be resolved")

    def pull_requests(self, sha):
        prs = []
        page = 1
        while True:
            batch = self.call("GET", f"/commits/{sha}/pulls?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise Failure("GitHub returned invalid pull request metadata")
            for pr in batch:
                if pr.get("merged_at") and pr.get("base", {}).get("ref") == self.default_branch:
                    prs.append({"number": pr["number"], "url": pr["html_url"], "title": pr["title"]})
            if len(batch) < 100:
                return prs
            page += 1
