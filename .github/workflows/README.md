# Dynamic Main Pipeline

`main.yml` is a **generated** orchestrator. It calls every reusable workflow in
this folder in the right order. You never edit `main.yml` by hand — you add a
workflow file, and the order is figured out from its name.

```
.github/
├── scripts/
│   └── generate-pipeline.py     # discovery + the prefix→order "checker"
└── workflows/
    ├── main.yml                 # GENERATED — the orchestrator (do not edit)
    ├── generate-pipeline.yml    # keeps main.yml in sync (CI)
    ├── lint.yml                 # ── your reusable workflows ──
    ├── test.yml
    ├── build.yml
    └── deploy.yml
```

## Why it's generated and not "live"

GitHub Actions **forbids expressions in the `uses:` key** of a reusable-workflow
call — `uses:` must be a literal path
([docs](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows):
*"You cannot use contexts or expressions in this keyword."*). So a single static
`main.yml` physically cannot call a workflow whose name it discovers at runtime —
`uses: ./.github/workflows/${{ matrix.file }}` is rejected.

The fix is to make the file **dynamic at its source**: a generator scans the
folder and writes a `main.yml` with real, literal `uses:` / `needs:` wiring. You
get genuine reusable-workflow semantics (nested jobs, the proper graph in the
Actions UI, `secrets:` passing) instead of a brittle runtime hack.

## Adding a pipeline

1. Drop a reusable workflow in `.github/workflows/`, named after its stage. The
   **first token** of the file name decides the stage — so call an integration
   suite `integration-tests.yml` (→ `integration`), not `test-integration.yml`
   (whose `test` prefix would land it in the `test` stage):
   ```yaml
   # .github/workflows/integration-tests.yml
   name: Integration tests
   on:
     workflow_call:        # ← REQUIRED. Files without this are ignored.
   jobs:
     it:
       runs-on: ubuntu-latest
       steps:
         - run: echo "..."
   ```
2. Regenerate (or let CI do it — see below):
   ```bash
   python3 .github/scripts/generate-pipeline.py
   ```
3. Commit `main.yml` together with your new file.

## The checker — how order is decided

The first token of the file name (its **prefix**) is matched against keyword
groups. Files in the same stage run **in parallel**; each stage waits for the
previous non-empty one.

| Order | Stage         | Matched name prefixes / keywords                                |
|------:|---------------|-----------------------------------------------------------------|
| 10    | `setup`       | setup, bootstrap, prepare, preflight, install, deps, dependencies |
| 20    | `lint`        | lint, format, fmt, style, static, typecheck, types, vet         |
| 30    | `test`        | test, tests, unit, ut, spec, coverage                           |
| 40    | `security`    | security, sec, sast, dast, scan, audit, codeql, trivy, snyk     |
| 50    | `build`       | build, compile, package, bundle, docker, image, artifact        |
| 60    | `integration` | integration, e2e, endtoend, smoke, acceptance, contract         |
| 80    | `deploy`      | deploy, release, publish, cd, rollout, ship, promote            |
| 85    | `other`       | *(anything unrecognised — runs **after** deploy, per "…→ deploy → others")* |
| 90    | `notify`      | notify, notification, report, announce, slack, teams, cleanup   |

So `lint.yml` and `lint-eslint.yml` both run first, in parallel; `test*.yml`
run next; and an unrecognised `chaos-monkey.yml` runs in the `other` stage
**after** `deploy`. (If you'd rather unknown checks *gate* deploy, lower
`DEFAULT_ORDER` below 80.) Edit the `STAGES` table at the top of
`generate-pipeline.py` to change keywords, add stages, or reorder. Files sharing
the same numeric order always form one parallel stage.

### Overriding a file's placement / secrets

Annotate the workflow file with a `#`-comment (a trailing `# explanation` is
fine):

```yaml
# pipeline-stage: deploy        # put me in the named "deploy" stage
# pipeline-order: 75            # …or pin an exact numeric order
# pipeline-secrets: inherit     # pass the caller's secrets to THIS workflow
```

## Keeping `main.yml` in sync (`generate-pipeline.yml`)

- **Pull requests & feature branches** → runs `--check` and **fails** if
  `main.yml` is stale, so a reviewer never sees an out-of-date pipeline. Fix by
  running the generator locally and committing.
- **Push to / manual run on the default branch** → regenerates and **commits**
  `main.yml` automatically (zero-touch). The triggers exclude `main.yml` itself,
  so this commit doesn't loop, and the push rebases-and-retries to survive a
  concurrent push.

> Auto-commit needs `contents: write` and a default branch that accepts pushes
> from `GITHUB_TOKEN`. If `main` is **protected** and rejects the push, the job
> does **not** fail — it logs a warning and leaves the PR `--check` gate to
> enforce freshness, so contributors regenerate locally instead. (To open a PR
> with the regenerated file rather than push directly, swap the commit step for
> `peter-evans/create-pull-request`.)

### Optional: regenerate on every local commit

Add a pre-commit hook so you never forget:

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: generate-main-pipeline
        name: Generate Main Pipeline
        entry: python3 .github/scripts/generate-pipeline.py
        language: system
        pass_filenames: false
        files: ^\.github/workflows/.*\.ya?ml$
```

## Notes & trade-offs

- **Secrets are least-privilege by default** (`SECRETS_INHERIT = False`): a
  workflow receives the caller's secrets only if it opts in with
  `# pipeline-secrets: inherit` (see `deploy.yml`). This stops an early lint/test
  stage from running with deploy-grade credentials. Set `SECRETS_INHERIT = True`
  to inherit everywhere.
- **A file is only included if it has `on: workflow_call:`.** Standalone
  workflows (e.g. `on: [push]`) and unsafe file names are skipped with a warning.
- **No `hashFiles()` guards** (unlike a hand-written static pipeline): the
  generator only ever lists files that exist, and the sync workflow guarantees
  `main.yml` matches the folder — so the guards would be dead weight, and they'd
  risk cascade-skipping downstream stages.
- **Runtime-only alternative:** if you cannot tolerate a generated file, the
  other way to be dynamic is a single `orchestrate` job that discovers files and
  triggers each via `gh workflow run` (every sub-workflow then also needs
  `on: workflow_dispatch:`), polling each run to completion. It avoids
  regeneration but loses nested `uses:` semantics and the clean job graph. The
  generator approach is recommended.
```
