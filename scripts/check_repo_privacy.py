#!/usr/bin/env python3
"""Reject private artifacts, personal emails, and recognizable credentials.

Messages identify the file/commit and rule, never the sensitive value. This is a
guardrail for accidental publication, not a substitute for reviewing client data.
"""

import argparse
import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath

PRIVATE_DIRS = {
    ".agents",
    ".claude",
    ".codex",
    ".impeccable",
    ".playwright-mcp",
    "audits",
    "backups",
    "caddy_logs",
    "data",
    "exports",
    "logs",
    "media",
    "playwright-report",
    "private",
    "secrets",
    "test-results",
}
PRIVATE_NOTES = {
    "AGENTS.md",
    "CLAUDE.md",
    "CLIENT_BOTS.md",
    "DESIGN.md",
    "HANDOFF.md",
    "INSTRUCTIONS.md",
    "POS-DESIGN.md",
    "PRODUCT.md",
}
PRIVATE_GLOBS = (
    "*.sqlite*",
    "*.db",
    "*.sql",
    "*.dump",
    "*.backup",
    "*.age",
    "*.pem",
    "*.key",
    "*.csv",
    "*.tsv",
    "*.xlsx",
    "*.xls",
    "*.pdf",
    "*.jsonl",
    "*.parquet",
    "*.zip",
    "*.tar",
    "*.gz",
    "*.log",
    "*.local.md",
    "age_identity*.txt",
    "*credentials*.json",
)
PUBLIC_EXAMPLES = {".env.example", "docs/catalog_import_example.csv"}
EMAIL = re.compile(rb"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
NOREPLY = re.compile(rb"[^\s@<>]+@users\.noreply\.github\.com\Z")
EXAMPLE_DOMAINS = {b"example.com", b"example.org", b"example.net"}
SECRET_PATTERNS = {
    "private key": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"
        rb"|AGE-SECRET-KEY-[A-Z0-9]{30,}"
    ),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{30,}\b"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "Telegram token": re.compile(rb"\b[0-9]{7,13}:[A-Za-z0-9_-]{30,}\b"),
}


def git(*args):
    return subprocess.check_output(["git", "-c", "core.quotePath=false", *args])


def private_path(path):
    if path in PUBLIC_EXAMPLES:
        return False
    p = PurePosixPath(path)
    name = p.name.lower()
    return (
        bool(set(p.parts) & PRIVATE_DIRS)
        or p.name in PRIVATE_NOTES
        or name == ".envrc"
        or name == ".env"
        or name.startswith(".env.")
        or (name.endswith(".md") and name.startswith(("audit", "аудит")))
        or any(fnmatch.fnmatchcase(name, pattern) for pattern in PRIVATE_GLOBS)
    )


def content_issues(data, private_values=()):
    issues = []
    for label, pattern in SECRET_PATTERNS.items():
        if pattern.search(data):
            issues.append(label)
    if any(value in data for value in private_values):
        issues.append("local credential value")
    # Binary assets can contain random byte sequences resembling email addresses.
    if b"\0" not in data:
        for match in EMAIL.finditer(data):
            domain = match[1].lower()
            if not (
                domain in EXAMPLE_DOMAINS
                or domain.endswith((b".example", b".test", b".invalid"))
                or NOREPLY.fullmatch(match[0])
            ):
                issues.append("personal/non-example email address")
                break
    return issues


def local_credentials():
    """Compare active local secrets without recording or printing them."""
    path = Path(".env")
    if not path.exists():
        return []
    values = []
    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if not re.search(r"SECRET|TOKEN|PASSWORD|_KEY$", key):
            continue
        if len(value) < 12 or re.search(r"change-me|dev-only|example|test", value, re.I):
            continue
        values.append(value.encode())
    return values


def index_entries(staged):
    changed = None
    if staged:
        changed = set(
            git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split(b"\0")
        )
    entries = set()
    for record in git("ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        info, path = record.split(b"\t", 1)
        if changed is None or path in changed:
            entries.add((info.split()[1].decode(), path.decode("utf-8", "surrogateescape")))
    return entries


def history_entries():
    entries = set()
    for tree in set(git("log", "--all", "--format=%T").splitlines()):
        for record in git("ls-tree", "-rz", "--full-tree", tree.decode()).split(b"\0"):
            if not record:
                continue
            info, path = record.split(b"\t", 1)
            if info.split()[1] == b"blob":
                entries.add((info.split()[2].decode(), path.decode("utf-8", "surrogateescape")))
    return entries


def check_entries(entries, private_values):
    failures = set()
    paths_by_blob = {}
    for sha, path in entries:
        paths_by_blob.setdefault(sha, set()).add(path)
        if private_path(path):
            failures.add((path, "private file type/location"))
    with subprocess.Popen(
        ["git", "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE
    ) as process:
        for sha, paths in paths_by_blob.items():
            process.stdin.write((sha + "\n").encode())
            process.stdin.flush()
            header = process.stdout.readline().split()
            if len(header) != 3:
                raise RuntimeError("Cannot read a Git object")
            data = process.stdout.read(int(header[2]))
            process.stdout.read(1)
            for issue in content_issues(data, private_values):
                failures.update((path, issue) for path in paths)
        process.stdin.close()
        if process.wait() != 0:
            raise RuntimeError("Cannot inspect Git objects")
    return failures


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true")
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--identity", action="store_true")
    args = parser.parse_args()
    private_values = local_credentials()
    entries = history_entries() if args.history else index_entries(args.staged)
    failures = check_entries(entries, private_values)
    if args.identity:
        for kind in ["GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT"]:
            identity = git("var", kind)
            email = re.search(rb"<([^<>]+)>", identity)
            if not email or not NOREPLY.fullmatch(email[1]):
                failures.add((kind, "configure a GitHub noreply email before committing"))
    if args.history:
        for record in git("log", "--all", "--format=%H%x00%ae%x00%ce").splitlines():
            sha, author, committer = record.split(b"\0")
            if not NOREPLY.fullmatch(author) or not NOREPLY.fullmatch(committer):
                failures.add((sha[:12].decode(), "commit metadata contains a non-noreply email"))
        for issue in content_issues(git("log", "--all", "--format=%B"), private_values):
            failures.add(("commit messages", issue))
    for path, issue in sorted(failures):
        print(f"FAIL {path}: {issue}")
    if failures:
        print(
            "Privacy check failed. Keep private files local; sanitize committed history before pushing."
        )
        return 1
    print(f"Privacy check passed ({len(entries)} file versions checked).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
