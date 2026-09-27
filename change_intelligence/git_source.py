"""Read immutable Git transitions without executing application code."""

import fnmatch
import json
import re
import subprocess

from .common import Failure

ZERO = "0" * 40
SHA = re.compile(r"[0-9a-f]{40}\Z")
MAX_FILE_PATCH = 16_000
MAX_BATCH_BYTES = 48_000
MAX_FILES = 2_000
EXCLUDE_CONTENT = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "*.jks",
    "id_rsa", "id_ed25519", "*.tfstate", "*.tfstate.*", "*.kubeconfig",
)


class GitSource:
    def __init__(self, directory="."):
        self.directory = directory

    def git(self, *args, limit=None):
        command = ["git", "--no-pager", *args]
        with subprocess.Popen(command, cwd=self.directory, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL) as process:
            raw = process.stdout.read() if limit is None else process.stdout.read(limit + 1)
            truncated = limit is not None and len(raw) > limit
            if truncated:
                process.kill()
            code = process.wait()
        if code and not truncated:
            raise Failure("Git history is unavailable or invalid; use a full-history checkout")
        return (raw[:limit], truncated) if limit is not None else raw

    def commit(self, sha):
        if not SHA.fullmatch(sha):
            raise Failure("Use a full, lowercase, 40-character Git commit SHA")
        actual = self.git("rev-parse", "--verify", f"{sha}^{{commit}}").decode().strip()
        if actual != sha:
            raise Failure("Expected an immutable commit SHA")
        return sha

    def transitions(self, base, target):
        self.commit(target)
        if base != ZERO:
            self.commit(base)
        # First-parent transitions cover merge, squash, and rebase strategies.
        # Requiring this exact chain avoids dropping commits or using a merge-base
        # comparison that accidentally includes changes outside the release.
        history = self.git("rev-list", "--first-parent", target).decode().splitlines()
        if base == target:
            return []
        if base != ZERO and base not in history:
            raise Failure("Base SHA must be on the target's first-parent history; force-push/divergent ranges need a new baseline")
        selected = history if base == ZERO else history[:history.index(base)]
        if len(selected) > 1000:
            raise Failure("More than 1,000 commits in range; backfill smaller ranges first")
        return list(reversed(selected))

    def parent(self, sha):
        parts = self.git("rev-list", "--parents", "-n", "1", self.commit(sha)).decode().split()
        return parts[1] if len(parts) > 1 else ZERO

    def require_main(self, sha):
        self.require_branch(sha, "main")

    def require_branch(self, sha, branch):
        self.git("check-ref-format", f"refs/heads/{branch}")
        self.commit(sha)
        history = self.git("rev-list", "--first-parent", f"refs/remotes/origin/{branch}").decode().splitlines()
        if sha not in history:
            raise Failure("Target SHA must already be on the configured default branch's first-parent history")

    def diff_args(self, base, sha):
        if base == ZERO:
            return ["diff-tree", "--root", "--no-commit-id", "-r", sha]
        return ["diff", base, sha]

    def files(self, base, sha):
        raw = self.git(*self.diff_args(base, sha), "--no-renames", "--name-only", "-z", "--")
        try:
            files = [path.decode("utf-8") for path in raw.split(b"\0") if path]
        except UnicodeError:
            raise Failure("A changed filename is not valid UTF-8; normalize it before analysis") from None
        if len(files) > MAX_FILES:
            raise Failure("More than 2,000 changed files in one commit; split the change for analysis")
        return files

    def evidence(self, sha):
        base = self.parent(sha)
        files = self.files(base, sha)
        limitations, batches, batch = [], [], []
        for path in files:
            excluded = any(fnmatch.fnmatch(path.rsplit("/", 1)[-1], rule) for rule in EXCLUDE_CONTENT)
            if excluded:
                patch = "[Content excluded: potentially sensitive file]"
                limitations.append(f"Content excluded: {path}")
            else:
                raw, truncated = self.git(
                    *self.diff_args(base, sha), "--patch", "--no-renames", "--no-ext-diff",
                    "--no-textconv", "--unified=3", "--", f":(literal){path}", limit=MAX_FILE_PATCH,
                )
                patch = raw.decode("utf-8", errors="replace")
                if truncated:
                    limitations.append(f"Patch truncated after {MAX_FILE_PATCH} bytes: {path}")
                if "Binary files " in patch or "GIT binary patch" in patch:
                    limitations.append(f"Binary content not analyzed: {path}")
                patch, redacted = redact(patch)
                if redacted:
                    limitations.append(f"Possible credentials redacted: {path}")
            entry = {"file": path, "patch": patch}
            # JSON encoding can expand control characters; count serialized bytes.
            while len(json.dumps([entry]).encode()) > MAX_BATCH_BYTES:
                entry["patch"] = entry["patch"][:len(entry["patch"]) // 2]
                note = f"Patch truncated to fit serialized input limit: {path}"
                if note not in limitations:
                    limitations.append(note)
                if not entry["patch"]:
                    raise Failure("A changed filename exceeds the model input limit")
            if batch and len(json.dumps(batch + [entry]).encode()) > MAX_BATCH_BYTES:
                batches.append(batch)
                batch = []
            batch.append(entry)
        if batch:
            batches.append(batch)
        title = self.git("show", "-s", "--format=%s", sha).decode("utf-8", errors="replace").strip()
        title, _ = redact(title)
        return {"base_sha": base, "sha": sha, "title": title[:500], "files": files,
                "limitations": limitations, "batches": batches}


def redact(text):
    patterns = (
        r"(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b",
        r"(?im)((?:password|passwd|secret|api[_-]?key|access[_-]?token)\s*[=:]\s*)[^\r\n]+",
    )
    original = text
    for pattern in patterns:
        text = re.sub(pattern, "[REDACTED]", text)
    return text, text != original
