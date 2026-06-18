#!/usr/bin/env python3
"""Generate .github/workflows/main.yml from the reusable workflows in the folder.

WHY THIS EXISTS
---------------
GitHub Actions does NOT allow expressions in the `uses:` key of a reusable
workflow call (https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows
-> "You cannot use contexts or expressions in this keyword."). That makes a
*single static* `main.yml` incapable of calling an arbitrarily-named workflow
that someone drops into the folder at runtime.

So instead of a runtime trick, we make the pipeline dynamic at the source: this
script scans `.github/workflows/`, decides the order of every reusable workflow
from its file-name prefix (the "checker"), and writes a fully wired `main.yml`
with real `uses:` / `needs:` semantics. Drop a `*.yml` in, regenerate, done.

USAGE
-----
    python3 .github/scripts/generate-pipeline.py            # write main.yml
    python3 .github/scripts/generate-pipeline.py --check    # fail if stale (CI)

THE CHECKER (file-name prefix -> execution stage)
-------------------------------------------------
Every reusable workflow is placed into a stage based on the keywords in its
file name (see STAGES below). Files in the same stage run in PARALLEL; each
stage waits for ("needs") the previous non-empty stage. Examples:

    lint.yml, lint-eslint.yml      -> stage "lint"   (run first, in parallel)
    test.yml, test-integration.yml -> stage "test"   (after lint)
    build.yml                      -> stage "build"  (after test)
    deploy.yml                     -> stage "deploy" (last)
    anything-unrecognised.yml      -> stage "other"  (before deploy, see DEFAULT_ORDER)

A workflow can override its placement with a header comment, e.g.:

    # pipeline-stage: deploy      (use a named stage)
    # pipeline-order: 75          (use an explicit numeric order)
"""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

# The checker. Each stage has a numeric order (lower = earlier) and a list of
# keywords matched against the workflow file name. The first stage whose
# keywords match (by prefix, then by "word" containment) wins. Tune freely.
STAGES: list[tuple[str, int, list[str]]] = [
    ("setup",       10, ["setup", "bootstrap", "prepare", "preflight", "install", "deps", "dependencies"]),
    ("lint",        20, ["lint", "format", "fmt", "style", "static", "typecheck", "types", "vet"]),
    ("test",        30, ["test", "tests", "unit", "ut", "spec", "coverage"]),
    ("security",    40, ["security", "sec", "sast", "dast", "scan", "audit", "codeql", "trivy", "snyk"]),
    ("build",       50, ["build", "compile", "package", "bundle", "docker", "image", "artifact"]),
    ("integration", 60, ["integration", "e2e", "endtoend", "smoke", "acceptance", "contract"]),
    ("deploy",      80, ["deploy", "release", "publish", "cd", "rollout", "ship", "promote"]),
    # Unrecognised workflows land at DEFAULT_ORDER=85 (between deploy and notify).
    ("notify",      90, ["notify", "notification", "report", "announce", "slack", "teams", "cleanup"]),
]

# Workflows whose names match nothing above run here -- AFTER deploy, matching
# the requested "lint -> test -> build -> deploy -> others" order. Lower this
# below deploy (e.g. 45) instead if you'd rather unknown checks gate deployment.
DEFAULT_ORDER = 85
DEFAULT_STAGE = "other"

# Files in the workflows folder that are never treated as pipeline stages.
GENERATED_FILE = "main.yml"
EXCLUDE = {GENERATED_FILE, "generate-pipeline.yml"}

# Triggers for the generated main pipeline.
TRIGGERS = ["push", "pull_request", "workflow_dispatch"]

# Whether to pass the caller's secrets to EVERY called workflow. Default False
# = least privilege: a workflow only receives secrets if it opts in with a
# `# pipeline-secrets: inherit` header comment. `secrets: inherit` hands the
# full repo/org secret set to that stage, so inheriting everywhere would let an
# early lint/test job run with deploy-grade credentials. Flip to True to inherit
# for all, or opt in per file (see SECRETS_ANNOTATION_RE).
SECRETS_INHERIT = False

# A workflow file we are willing to reference from `uses:`. GitHub's local
# `uses: ./path` does not accept a ref, so an `@` or a space would silently
# break it -- such files are skipped with a warning instead of emitting bad YAML.
SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")
# Annotations live in `#`-comments; a trailing `# ...` explanation is tolerated.
STAGE_ANNOTATION_RE = re.compile(r"(?im)^\s*#\s*pipeline-stage\s*:\s*([A-Za-z0-9_-]+)\s*(?:#.*)?$")
ORDER_ANNOTATION_RE = re.compile(r"(?im)^\s*#\s*pipeline-order\s*:\s*(\d+)\s*(?:#.*)?$")
SECRETS_ANNOTATION_RE = re.compile(r"(?im)^\s*#\s*pipeline-secrets\s*:\s*inherit\s*(?:#.*)?$")
ON_KEY_RE = re.compile(r"""^(?:on|["']on["'])\s*:(.*)$""")

STAGE_ORDER_BY_NAME = {name: order for name, order, _ in STAGES}


# --------------------------------------------------------------------------- #
# The checker                                                                 #
# --------------------------------------------------------------------------- #

def classify(path: Path) -> tuple[int, list[str]]:
    """Return (order, warnings) for a workflow file. Order alone decides the
    stage; the display label is derived from the order (see label_for_order),
    so two files with the same order always share one parallel stage.

    Resolution order:
      1. `# pipeline-order: N` header annotation (explicit numeric override)
      2. `# pipeline-stage: name` header annotation (named-stage override)
      3. file-name prefix / keyword match against STAGES
      4. DEFAULT_ORDER fallback
    """
    text = _read(path)
    warnings: list[str] = []

    m = ORDER_ANNOTATION_RE.search(text)
    if m:
        return int(m.group(1)), warnings

    m = STAGE_ANNOTATION_RE.search(text)
    if m:
        wanted = m.group(1).lower()
        if wanted in STAGE_ORDER_BY_NAME:
            return STAGE_ORDER_BY_NAME[wanted], warnings
        warnings.append(
            f"'{path.name}': unknown pipeline-stage '{m.group(1)}' "
            f"(known: {', '.join(STAGE_ORDER_BY_NAME)}); using name-based classification."
        )

    name = path.stem.lower()
    tokens = [t for t in re.split(r"[^a-z0-9]+", name) if t]  # split on -, _, ., etc.
    first = tokens[0] if tokens else name

    # Strongest signal: the file-name *prefix* (first token) is a stage keyword.
    for _, order, keywords in STAGES:
        if first in keywords:
            return order, warnings
    # Weaker signal: any token matches a stage keyword.
    for _, order, keywords in STAGES:
        if any(tok in keywords for tok in tokens):
            return order, warnings

    return DEFAULT_ORDER, warnings


def label_for_order(order: int) -> str:
    """Human-readable stage label for a numeric order."""
    for name, o, _ in STAGES:
        if o == order:
            return name
    return DEFAULT_STAGE if order == DEFAULT_ORDER else f"order-{order}"


def is_reusable(path: Path) -> bool:
    """True only if `workflow_call` is a trigger under the top-level `on:` key.

    A plain text scan for `workflow_call:` would false-positive on a job/env key
    of that name, so we locate the top-level `on:` mapping and look only inside
    it (handles inline `on: workflow_call`, `on: [push, workflow_call]`, and the
    block form with `workflow_call:` indented under `on:`).
    """
    lines = _read(path).splitlines()
    for i, line in enumerate(lines):
        m = ON_KEY_RE.match(line)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline:  # inline value on the same line as `on:`
            return bool(re.search(r"\bworkflow_call\b", inline))
        # block form: inspect lines indented under `on:` until the next top-level key
        for nxt in lines[i + 1:]:
            if not nxt.strip() or nxt.lstrip().startswith("#"):
                continue
            if len(nxt) - len(nxt.lstrip()) == 0:
                break  # dedented back to a top-level key (jobs:, env:, ...)
            if re.match(r"\s*workflow_call\s*:", nxt):
                return True
        return False
    return False


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Job-id helpers                                                              #
# --------------------------------------------------------------------------- #

def job_id(stem: str, used: set[str]) -> str:
    """Turn a file stem into a unique, valid GitHub job id ([A-Za-z_][-\\w]*)."""
    jid = re.sub(r"[^A-Za-z0-9_-]", "-", stem).strip("-") or "job"
    if not re.match(r"[A-Za-z_]", jid):
        jid = f"job-{jid}"
    base, n = jid, 2
    while jid in used:
        jid = f"{base}-{n}"
        n += 1
    used.add(jid)
    return jid


# --------------------------------------------------------------------------- #
# Discovery + grouping                                                        #
# --------------------------------------------------------------------------- #

def discover(workflows_dir: Path) -> tuple[list[tuple[int, str, list[dict]]], list[str]]:
    """Return (stages, warnings).

    stages: list of (order, label, [job, ...]) sorted by order, only non-empty.
            each job = {"id", "file", "secrets"}.
    """
    files = sorted(
        p for p in workflows_dir.glob("*.y*ml")
        if p.suffix in (".yml", ".yaml") and p.name not in EXCLUDE
    )

    warnings: list[str] = []
    buckets: dict[int, list[dict]] = {}  # keyed by order only -> one stage per order
    used_ids: set[str] = set()

    for path in files:
        if not SAFE_FILENAME_RE.match(path.name):
            warnings.append(
                f"skipped '{path.name}': unsafe workflow file name "
                f"(only letters, digits, '.', '-', '_' are allowed)."
            )
            continue
        if not is_reusable(path):
            warnings.append(
                f"skipped '{path.name}': no `on: workflow_call:` trigger "
                f"(not a reusable workflow)."
            )
            continue
        order, w = classify(path)
        warnings.extend(w)
        jid = job_id(path.stem, used_ids)
        buckets.setdefault(order, []).append({
            "id": jid,
            "file": path.name,
            "secrets": SECRETS_INHERIT or bool(SECRETS_ANNOTATION_RE.search(_read(path))),
        })

    stages = [
        (order, label_for_order(order), sorted(buckets[order], key=lambda j: j["file"]))
        for order in sorted(buckets)
    ]
    return stages, warnings


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #

def render(stages: list[tuple[int, str, list[dict]]]) -> str:
    lines: list[str] = []
    a = lines.append

    a("# " + "=" * 74)
    a("# THIS FILE IS AUTO-GENERATED -- DO NOT EDIT BY HAND.")
    a("# Source of truth: the reusable workflows in .github/workflows/")
    a("# Regenerate with:  python3 .github/scripts/generate-pipeline.py")
    a("#")
    if stages:
        a("# Execution order (decided by file-name prefix; see the checker):")
        for idx, (order, label, jobs) in enumerate(stages, 1):
            names = ", ".join(j["file"] for j in jobs)
            parallel = " [parallel]" if len(jobs) > 1 else ""
            a(f"#   {idx}. {label:<12} -> {names}{parallel}")
    else:
        a("# No reusable workflows found in .github/workflows/ yet.")
    a("# " + "=" * 74)
    a("name: Main Pipeline")
    a("")
    a("on:")
    for trig in TRIGGERS:
        a(f"  {trig}:")
    a("")
    a("concurrency:")
    a("  group: main-pipeline-${{ github.workflow }}-${{ github.ref }}")
    a("  cancel-in-progress: true")
    a("")
    a("jobs:")

    if not stages:
        a("  no-pipelines:")
        a('    name: "No reusable workflows found"')
        a("    runs-on: ubuntu-latest")
        a("    steps:")
        a('      - run: |')
        a('          echo "::notice::No reusable workflows in .github/workflows/."')
        a('          echo "Add e.g. lint.yml / test.yml (with an \'on: workflow_call:\' trigger)"')
        a('          echo "then run: python3 .github/scripts/generate-pipeline.py"')
        return "\n".join(lines) + "\n"

    prev_job_ids: list[str] = []
    first_job = True
    for order, label, jobs in stages:
        this_stage_ids: list[str] = []
        for job in jobs:
            if not first_job:
                a("")
            first_job = False
            a(f"  {job['id']}:")
            a(f'    name: "{label}: {job["file"]}"')
            if prev_job_ids:
                a(f"    needs: [{', '.join(prev_job_ids)}]")
            a(f"    uses: ./.github/workflows/{job['file']}")
            if job["secrets"]:
                a("    secrets: inherit")
            this_stage_ids.append(job["id"])
        prev_job_ids = this_stage_ids

    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Entry point                                                                 #
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true",
                        help="exit non-zero if main.yml is out of date (do not write).")
    parser.add_argument("--workflows-dir", type=Path, default=None,
                        help="path to the workflows folder (default: auto-detected).")
    parser.add_argument("--quiet", action="store_true", help="suppress the plan summary.")
    args = parser.parse_args(argv)

    workflows_dir = args.workflows_dir or (Path(__file__).resolve().parents[1] / "workflows")
    if not workflows_dir.is_dir():
        print(f"error: workflows dir not found: {workflows_dir}", file=sys.stderr)
        return 2

    stages, warnings = discover(workflows_dir)
    new_content = render(stages)
    target = workflows_dir / GENERATED_FILE

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if not args.quiet:
        if stages:
            print("Pipeline order:")
            for idx, (order, label, jobs) in enumerate(stages, 1):
                files = ", ".join(j["file"] for j in jobs)
                print(f"  {idx}. {label:<12} (order {order}): {files}")
        else:
            print("No reusable workflows found; wrote a placeholder main.yml.")

    old_content = target.read_text(encoding="utf-8") if target.exists() else ""

    if args.check:
        if old_content != new_content:
            print(f"\n::error::{target} is out of date. "
                  f"Run: python3 .github/scripts/generate-pipeline.py", file=sys.stderr)
            diff = difflib.unified_diff(
                old_content.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"{GENERATED_FILE} (current)",
                tofile=f"{GENERATED_FILE} (expected)",
            )
            sys.stderr.writelines(diff)
            return 1
        print(f"OK: {GENERATED_FILE} is up to date.")
        return 0

    if old_content != new_content:
        target.write_text(new_content, encoding="utf-8")
        print(f"Wrote {target}")
    else:
        print(f"{target} already up to date.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
