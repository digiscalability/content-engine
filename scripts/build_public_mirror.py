"""Build the public mirror's tree (SRD C11, NFR-9; ADR 0014). Local only: it never creates a
repository, a remote or a commit. Abbas decides whether the mirror is published.

    uv run python scripts/build_public_mirror.py <out-dir> [--denylist FILE] [--repo DIR]

1. The tree is `git archive HEAD` (committed files only), without the private planning
   docs: the SRD, the build prompt and PROGRESS. The mirror starts a fresh history from
   this tree, so nothing from the private history goes public.
2. It is scanned three ways, and any finding fails the build:
   - secrets, with the pre-commit scanner (scripts/check_secrets.py);
   - personal terms from a denylist kept outside the repo, because the list is itself
     personal (default %LOCALAPPDATA%/content-engine/mirror-denylist.txt, one term per
     line, # for comments); findings name the term's number, never the term;
   - relative markdown links that point at a file the mirror does not have.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_secrets  # noqa: E402  (a sibling stdlib-only script)

PRIVATE = ("docs/SRD.md", "docs/BUILD_PROMPT.md", "docs/PROGRESS.md")
DEFAULT_DENYLIST = (
    Path(os.environ.get("LOCALAPPDATA", "~")) / "content-engine" / "mirror-denylist.txt"
)
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
TEXT_SUFFIXES = {".md", ".py", ".yaml", ".yml", ".toml", ".json", ".txt", ".html", ".sql",
                 ".ps1", ".cfg", ".ini", ".example", ""}  # fmt: skip


def build_tree(repo: Path, out: Path) -> Path:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"], cwd=repo, check=True, capture_output=True
    ).stdout
    out.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(out, filter="data")
    for private in PRIVATE:
        (out / private).unlink(missing_ok=True)
    return out


def _files(root: Path):
    for path in sorted(root.rglob("*")):
        if path.is_file() and ".git" not in path.parts:
            yield path, path.relative_to(root).as_posix()


def _text(path: Path) -> str | None:
    if path.suffix.lower() not in TEXT_SUFFIXES and not path.name.startswith("."):
        return None
    data = path.read_bytes()
    return None if b"\x00" in data else data.decode("utf-8", errors="replace")


def secret_findings(root: Path) -> list[str]:
    paths = {rel: path for path, rel in _files(root)}
    return check_secrets.scan_paths(
        list(paths), lambda rel: paths[rel].read_bytes().decode("utf-8", errors="replace")
    )


def denylist_findings(root: Path, terms: list[str]) -> list[str]:
    patterns = [re.compile(rf"(?<!\w){re.escape(t)}(?!\w)", re.IGNORECASE) for t in terms]
    findings = []
    for path, rel in _files(root):
        text = _text(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            findings += [
                f"{rel}:{lineno}: personal term #{n} from the denylist"
                for n, pattern in enumerate(patterns, start=1)
                if pattern.search(line)
            ]
    return findings


def broken_links(root: Path) -> list[str]:
    findings = []
    for path, rel in _files(root):
        if path.suffix.lower() != ".md":
            continue
        for target in LINK.findall(path.read_text(encoding="utf-8", errors="replace")):
            if re.match(r"^[a-z][a-z0-9+.-]*:|^#", target, re.IGNORECASE):
                continue  # a URL, a mail link or an anchor
            file_part = target.split("#", 1)[0]
            if file_part and not (path.parent / file_part).exists():
                findings.append(f"{rel}: link to {file_part} has no file")
    return findings


def read_denylist(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("out", type=Path, help="a new folder for the public tree")
    parser.add_argument("--denylist", type=Path, default=DEFAULT_DENYLIST.expanduser())
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    if not args.denylist.is_file():
        print(f"No denylist at {args.denylist}: write the personal terms the mirror must not "
              "contain there (one a line) and run again.", file=sys.stderr)  # fmt: skip
        return 2
    if args.out.exists():
        print(f"{args.out} already exists; give a new folder.", file=sys.stderr)
        return 2
    root = build_tree(args.repo, args.out)
    findings = [
        *secret_findings(root),
        *denylist_findings(root, read_denylist(args.denylist)),
        *broken_links(root),
    ]
    count = sum(1 for _ in _files(root))
    if findings:
        print(f"Public tree at {root} ({count} files) is NOT ready:", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        return 1
    print(f"Public tree at {root}: {count} files; no secrets, personal terms or broken links.")
    print("Publishing it (a fresh repository with one commit) waits for Abbas's OK (C11).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
