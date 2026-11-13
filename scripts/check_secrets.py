"""Pre-commit secret scan (SRD NFR-7, C6; ported from ventures-ops scripts/check-secrets.mjs).

Blocks staged files that look like they hold a credential, and secret-bearing file
types outright. Stdlib only, so the git hook runs with any Python 3.

    python scripts/check_secrets.py --staged    (the pre-commit hook)
    python scripts/check_secrets.py --history   (every blob reachable from any ref)
    python scripts/check_secrets.py <files...>

Findings name the file, line and pattern, never the matched value. Test fixtures build
fake keys at runtime ("sk-ant-" + "x" * 40) so their source does not match. A real false
positive can be waved through by adding "check-secrets: allow" to that line.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Google API key", re.compile(r"AIza[0-9A-Za-z_-]{35}")),
    ("Google OAuth client secret", re.compile(r"GOCSPX-[0-9A-Za-z_-]{20,}")),
    ("Google OAuth refresh token", re.compile(r"1//0[0-9A-Za-z_-]{40,}")),
    ("Service account private key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Meta access token", re.compile(r"EAA[A-Za-z0-9]{60,}")),
    ("Stripe secret key", re.compile(r"sk_(live|test)_[0-9A-Za-z]{20,}")),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Anthropic key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("OpenAI key", re.compile(r"sk-(proj-)?[A-Za-z0-9]{32,}")),
    ("ElevenLabs key", re.compile(r"sk_[0-9a-f]{48}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    (
        "Generic assignment",
        re.compile(
            r"""(?:api[_-]?key|secret|password|token)["']?\s*[:=]\s*["']([A-Za-z0-9_\-+/]{24,})["']""",
            re.IGNORECASE,
        ),
    ),
    (
        # A value pasted into .env.example (or any dotenv-style file): NAME=value, unquoted.
        # The secret word must be a whole `_`-separated segment of the name, so SIDECHAIN or
        # WIDTH_GUIDE are not mistaken for an ID.
        "Dotenv assignment",
        re.compile(
            r"""^\s*(?:export\s+)?(?:[A-Z0-9]+_)*(?:KEY|SECRET|TOKEN|PASSWORD|ID)(?:_[A-Z0-9]+)*"""
            r"""\s*=\s*["']?([^\s"'#]{16,})"""
        ),
    ),
]

ALLOW_MARKER = "check-secrets: allow"
_NAME_ONLY = re.compile(r"^[A-Z0-9_]+$")  # ALL_CAPS_SNAKE values are secret names, not values
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_EXAMPLE_ENV_FILES = {".env.example"}

Reader = Callable[[str], "str | None"]


def is_secret_bearing_path(path: str) -> bool:
    """True for file types that must never be committed, whatever they contain."""
    name = PurePosixPath(path.replace("\\", "/")).name.lower()
    if name in _EXAMPLE_ENV_FILES:
        return False
    if name == ".env" or name.startswith(".env."):
        return True
    if name == "secrets.md" or name.endswith(".secret.local"):
        return True
    if PurePosixPath(name).suffix in _SECRET_SUFFIXES:
        return True
    return name.endswith(".json") and ("serviceaccount" in name or "credentials" in name)


def scan_text(path: str, text: str) -> list[str]:
    """Findings for one file's text: '<path>:<line>: possible <pattern name>'."""
    if "\x00" in text:
        return []  # binary
    findings: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if ALLOW_MARKER in line:
            continue
        for name, pattern in PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            value = match.group(1) if match.re.groups and match.lastindex else None
            if value is not None and _NAME_ONLY.match(value):
                continue
            findings.append(f"{path}:{lineno}: possible {name}")
    return findings


def scan_paths(paths: list[str], read: Reader) -> list[str]:
    findings: list[str] = []
    for path in paths:
        if is_secret_bearing_path(path):
            findings.append(f"{path}: secret-bearing file type must not be committed")
            continue
        text = read(path)
        if text is not None:
            findings.extend(scan_text(path, text))
    return findings


def staged_files() -> list[str]:
    # -z and core.quotepath=false: otherwise git escapes non-ASCII names and they are skipped.
    out = subprocess.run(
        [
            "git",
            "-c",
            "core.quotepath=false",
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "--diff-filter=ACMR",
        ],
        check=True,
        capture_output=True,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def read_staged(path: str) -> str | None:
    result = subprocess.run(["git", "show", f":{path}"], capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8", errors="replace")


def scan_history(repo: Path | str = ".") -> tuple[list[str], int]:
    """Every blob reachable from any ref, scanned once, plus every path ever committed
    checked against the secret-bearing types. Returns (findings, blobs scanned)."""

    def git(*args: str, data: bytes | None = None) -> bytes:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", *args],
            cwd=repo, input=data, check=True, capture_output=True,
        ).stdout  # fmt: skip

    paths: dict[str, str] = {}  # object -> first path seen
    for line in git("rev-list", "--all", "--objects").decode("utf-8").splitlines():
        sha, _, path = line.partition(" ")
        if path:
            paths.setdefault(sha, path)
    kinds = git("cat-file", "--batch-check=%(objectname) %(objecttype)",
                data="\n".join(paths).encode()).decode().split()  # fmt: skip
    blobs = [sha for sha, kind in zip(kinds[::2], kinds[1::2], strict=True) if kind == "blob"]
    findings: list[str] = []
    seen_paths = set()
    for sha in blobs:
        path = paths[sha]
        if is_secret_bearing_path(path):
            if path not in seen_paths:
                findings.append(f"{path}: secret-bearing file type must not be committed")
                seen_paths.add(path)
            continue
        text = git("cat-file", "blob", sha).decode("utf-8", errors="replace")
        findings += [f"{finding} (blob {sha[:10]})" for finding in scan_text(path, text)]
    return findings, len(blobs)


def read_file(path: str) -> str | None:
    try:
        return Path(path).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if hasattr(sys.stderr, "reconfigure"):
        # Git paths are UTF-8; on Windows a piped stderr would otherwise be cp1252.
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    if args[:1] == ["--staged"]:
        findings = scan_paths(staged_files(), read_staged)
    elif args[:1] == ["--history"]:
        findings, scanned = scan_history()
        if not findings:
            print(f"Secret scan clean across history: {scanned} blobs from every ref.")
            return 0
        print("Secret scan found these in history:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    else:
        findings = scan_paths(args, read_file)
    if not findings:
        return 0
    print("Secret scan blocked this commit:", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(f'If a match is a false positive, add "{ALLOW_MARKER}" to that line.', file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
