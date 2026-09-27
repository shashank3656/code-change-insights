# Code Change Insights

A shared GitHub Action that explains merged code changes, assesses their risk,
and reuses the saved assessments when creating a SetuOps Change Request.

```text
Application repo: merge to main
  → code-change-insights (AI analysis)
  → application repo: change-intelligence branch

Later: release / Change Request job
  → saved assessments for deployed SHA .. target SHA
  → SetuOps: one Change Request with the release summary and risks
```

The analysis code lives here. Each application keeps its own code, assessments,
credentials, and deployment baseline. Ingestion never contacts SetuOps.

## Add it to an application repository

Copy these two files into that application's `.github/workflows/` directory:

- [Merge ingestion workflow](examples/ingest-changes.yml)
- [Change Request workflow](examples/create-change-request.yml)

Configure these **Actions secrets and variables in each application repository**:

| Kind | Name | Purpose |
| --- | --- | --- |
| Secret | `CURSOR_API_KEY` | Generate new AI assessments with Cursor Agent CLI |
| Variable | `CURSOR_MODEL` | Cursor model ID; defaults to `composer-2.5` |
| Secret | `CHANGE_WEBHOOK_SECRET` | Submit a Change Request to SetuOps |
| Variable | `SETUOPS_API_URL` | SetuOps HTTPS origin or complete change webhook URL |

For your existing SetuOps deployment, use
`https://backend-api-production-ed.up.railway.app` as `SETUOPS_API_URL`.
The action adds `/api/webhooks/change` if needed.

This action is in a **private repository**. In this repository's Settings →
Actions → General → Access, enable access from repositories owned by
`shashank3656`. That supports other private repositories in the same account,
including Application_Onboarding. Public callers and other owners require a
compatible sharing arrangement; private sharing does not make the action public.
[GitHub private action sharing](https://docs.github.com/en/actions/how-tos/reuse-automations/share-across-private-repositories).

## Ingest merged changes

The example workflow triggers on pushes to `main`, including PR merges. The
essential step, after checking out the application with `fetch-depth: 0`, is:

```yaml
- uses: shashank3656/code-change-insights@v1
  id: insights
  with:
    operation: ingest
    cursor-api-key: ${{ secrets.CURSOR_API_KEY }}
    cursor-model: ${{ vars.CURSOR_MODEL || 'composer-2.5' }}
```

The caller job needs `contents: write` and `pull-requests: read`. The default
`github-token` is the caller's `github.token`; it does not require a cross-repo PAT.
The action itself is downloaded using GitHub's private-action access mechanism.

Every first-parent commit transition in the push is analyzed. Merge, squash,
rebase, and direct-push changes are covered. Assessments are stored as
`assessments/<full-sha>.json` on an orphan `change-intelligence` branch **in the
calling repository**. No raw patches are stored there. Successful assessments
are reused on retries; failed ones can be retried.

## Reuse the assessments in a Change Request

The example Change Request workflow accepts the actual previously deployed SHA,
target release SHA, service, environment, and title. It starts in preview mode.
The action can also be called directly from an existing release job:

```yaml
- uses: shashank3656/code-change-insights@v1
  id: change
  with:
    operation: change-request
    base-sha: ${{ steps.release.outputs.deployed_sha }}
    target-sha: ${{ steps.release.outputs.target_sha }}
    service: my-application
    environment: prod
    title: Deploy application release
    submit: 'true'
    setuops-api-url: ${{ vars.SETUOPS_API_URL }}
    change-webhook-secret: ${{ secrets.CHANGE_WEBHOOK_SECRET }}
```

This job needs only `contents: read` and no AI key. It must run on the application's
configured default branch with a full-history checkout, after ingestion finishes.
Missing or failed assessments stop submission. A release includes all saved
assessments from its deployed baseline through its target, not only the latest PR.

With `submit: 'false'`, it produces `change-request.json` and `change-request.md`
without contacting SetuOps. With `submit: 'true'`, it sends a normal Change Request
in `new` status. It does not deploy, approve a release, or advance the deployed SHA.

Upload the reports from `${{ steps.change.outputs.artifact-directory }}` using
`actions/upload-artifact`. On submission, `change-number` and `change-id` outputs
identify the SetuOps record. Ingestion exposes `assessment-count`; request
preparation exposes `risk-level`.

## Versions and configuration

`v1.1.0` uses Cursor Agent CLI for analysis; `v1` is the current major-version entry point.
Use a full commit SHA in `uses:` when you want immutable version pinning. The
exact action version supplies both the metadata and Python code; callers do not
need a separately pinned toolkit checkout.

The examples assume `main`. For a different branch, change the trigger, job
condition, checkout ref, and the action's `default-branch` input together.
Use `source-directory` for applications checked out below the workspace root.
Python 3.10+, Git, and curl are required; Ubuntu GitHub-hosted runners are supported.
The action installs Cursor Agent CLI from Cursor's official installer for ingestion.
There are no runtime Python package dependencies.

See [operating guide](docs/change-intelligence.md) for backfill, failure behavior,
input/output details, data handling, and release review considerations.
[Action metadata](action.yml) is the complete input/output contract.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary Git repositories and mocked Cursor/HTTP boundaries. CI also exercises
the composite action on an empty release range, which must fail without calling
Cursor or SetuOps. No credentials for those services are needed to test the toolkit.
