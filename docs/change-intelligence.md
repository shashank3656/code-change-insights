# Operating Code Change Insights

## Ownership and boundaries

`shashank3656/code-change-insights` distributes one shared composite GitHub Action.
The caller checks out its own application. GitHub downloads the action separately.
The bootstrap runs the shared package in Python isolated mode so Python files in
the application cannot shadow the toolkit or standard library. The checkout is
read as Git data; application tests, build scripts, and migrations are not executed.

`GITHUB_REPOSITORY`, the event payload, GitHub token, and run URL belong to the caller.
Each caller gets its own `change-intelligence` branch. There is no central assessment
database or cross-application lookup. Temporary reports are written outside the
application checkout in a fresh runner directory.

## Enable a repository

1. Allow private-action access from other private repositories owned by the same
   user in the shared repository's Actions settings. Caller policies must also allow
   this action and the checkout/artifact actions.
2. Copy `examples/ingest-changes.yml` and `examples/create-change-request.yml` into
   the caller's `.github/workflows/` directory and commit them to its default branch.
3. Configure `OPENAI_API_KEY`, `OPENAI_MODEL`, `CHANGE_WEBHOOK_SECRET`, and
   `SETUOPS_API_URL` in that caller. API keys belong in secrets. The model and API URL
   can be repository variables. Configure secrets only for the operations you use.
4. Allow ingestion's `contents: write` and `pull-requests: read` token permissions.
   Branch rules must permit the workflow to write `change-intelligence`. Protect
   completed evidence from unrelated human edits; never merge this branch into main.
5. Backfill history from the actual last deployed revision if the first intended
   release contains changes predating this integration.

Shared private actions can be used by the same owner's private repositories when
access is enabled. This does not grant support to arbitrary public repositories
or repositories owned by other users. Organization-owned deployment requires the
corresponding organization sharing configuration. Repository visibility is not
changed by the action. GitHub's policy is documented at
https://docs.github.com/en/actions/how-tos/reuse-automations/share-across-private-repositories.

## Backfill and retries

Run the copied ingestion workflow manually with full lowercase 40-character
`base_sha` and `target_sha` values. The base is excluded, and the target is included.
Forty zeros may be used only for ingestion of initial history. The target must be
on the configured default branch's first-parent history. The workflow must be
launched from that branch.

A push can contain several commits. The action analyzes each transition against
its first parent, including the cumulative effect of a merge commit. It does not
rely on pull-request fields being present in a push event. Associated merged PRs
are fetched separately from GitHub, with pagination.

For a failed run, inspect its workflow summary and records on `change-intelligence`.
Failures are saved with `status: failed`; successful records use `status: completed`.
After correcting configuration, permissions, or service availability, rerun the
job. Completed records are reused without another AI call. Storage conflicts are
retried. Different push SHAs are not serialized into one cancelable pending job.

Ranges over 1,000 first-parent commits must be backfilled in smaller pieces.
Divergent histories and force pushes require an explicit reviewed baseline.
Completed records are immutable for their SHA and schema version. A future schema
migration needs an explicit reassessment policy; changing the configured model
does not silently overwrite previously completed evidence.

## Release selection and submission

Supply the actual last successfully deployed SHA for the target environment and
the intended target SHA. A previous merge or previous Change Request does not
prove that a deployment succeeded. Neither ingestion nor submission advances a
baseline. Forty zeros cannot be a Change Request baseline.

The release job requires a matching completed assessment for every first-parent
transition in the range. It validates repository identity, SHAs, schema version,
and changed-file coverage. Missing, failed, mismatched, or corrupt evidence stops
submission; there is no fallback to the latest available assessment.

The summary includes behavior changes, risk factors with file paths, recommended
validation, rollback considerations, limitations, and PR references for all included
commits. Overall risk is the highest assessment severity, not an average. The AI
has not executed the recommended tests or verified the deployment.

Set `submit: 'false'` for a preview; no SetuOps credentials are required. Preview
produces `change-request.json` and `change-request.md`. `submit: 'true'` prepares
and then submits in the same action invocation, so a failed preparation cannot
send a stale payload. Successful submission also writes `setuops-response.json`.
The output `artifact-directory` points to these files, including on later failures.
Caller examples upload them to Actions artifacts for review.

SetuOps receives its existing `/api/webhooks/change` contract: `repo`, `sha`,
`previous_sha`, `service`, `environment`, `title`, `summary`, `risk_level`, `impact`,
`backout_plan`, files, PR metadata, pipeline link, and optional supply-chain links.
The payload requests `change_type: normal` and `status: new`; it does not start a
post-deployment watch or bypass SetuOps's approval rules.

The existing SetuOps endpoint deduplicates by repository + SHA + environment and
may preserve nonempty fields on an existing CHG. Reusing that identity is not a
promise to replace an already reviewed summary. POSTs are not automatically
retried. After a timeout, check SetuOps's records before rerunning.

## Evidence quality and data handling

The AI receives code patches and commit titles as untrusted data, with no tools.
Structured output and local validation require a consistent schema and reject
references to files outside the supplied batch. This constrains output shape;
risks still need human review.

Patch input is bounded and processed in batches. Truncated, binary, excluded, or
redacted content is reported explicitly. Cross-batch interactions require review.
Oversized changes can fail rather than be silently omitted. Incomplete file
coverage sets assessed risk to `unknown`; because the existing SetuOps API supports
low/medium/high, unknown is mapped to high with a prominent manual-review note.

Common credential-file contents are excluded and obvious credentials are redacted
on a best-effort basis. This is not a complete secret scanner. Code patches are
sent to the selected OpenAI model. Responses requests use `store: false`; that
setting is not a blanket promise of zero provider retention under all policies.
Raw patches are not persisted by this toolkit. Summaries inherit the caller repo's
access level and remain on its storage branch until explicitly managed by its owner.

HTTP requests use bounded retries and timeouts, refuse redirects carrying
credentials, and avoid printing remote response bodies or secrets in error logs.
The request job needs no AI key; the ingestion job needs no SetuOps secret.

## Action inputs and outputs

See `action.yml` for the complete schema. Key inputs are `operation` (`ingest` or
`change-request`), `default-branch` (main by default), and `source-directory` (the
workspace root by default). Ingestion on push infers the before/after SHAs from
the event. Manual ingestion and request creation take `base-sha` and `target-sha`.

Optional `sbom-url`, `attestation-url`, and `image-digest` are forwarded to SetuOps.
`setuops-api-url` accepts an HTTPS origin or the full webhook endpoint. For the
current deployment use https://backend-api-production-ed.up.railway.app.

Outputs:

| Output | When available |
| --- | --- |
| `artifact-directory` | After action validation, including later failures |
| `assessment-count` | Successful ingestion; includes reused records |
| `risk-level` | Successful request preparation; SetuOps-compatible risk |
| `change-number` | Successful SetuOps submission |
| `change-id` | Successful SetuOps submission |

## Protocol references

- https://docs.github.com/en/actions/reference/workflows-and-actions/metadata-syntax
- https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents
- https://developers.openai.com/api/docs/guides/structured-outputs
- https://developers.openai.com/api/docs/guides/migrate-to-responses
