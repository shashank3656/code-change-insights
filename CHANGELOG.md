# Changelog

## 1.1.5

- Submit only fields from the current SetuOps CI webhook contract while retaining the complete AI assessment in workflow artifacts.

## 1.1.4

- Align SetuOps submissions with the current webhook contract: standard changes and a 120-minute watch.
- Allow environment to be omitted so SetuOps can resolve it from its service catalog.

## 1.1.3

- Trust the action-created isolated workspace so Cursor can run non-interactively in CI.

## 1.1.2

- Preserve redacted Cursor CLI error details in failed assessment records for actionable CI diagnostics.

## 1.1.1

- Pass the analysis prompt as the Cursor CLI positional prompt argument in headless mode.

## 1.1.0

- Switched change analysis from the OpenAI Responses API to Cursor Agent CLI.
- Added `CURSOR_API_KEY` and `CURSOR_MODEL`, defaulting to `composer-2.5`.
- Run Cursor in an isolated temporary directory with a restricted environment and validate its JSON locally.

## 1.0.0

- Shared composite action for repository-local merge ingestion and release assessments.
- Structured AI change summaries, risk factors, evidence limitations, and recommended checks.
- Durable, retryable per-commit storage in the caller repository.
- Release-range aggregation with preview and SetuOps Change Request submission.
- Caller workflow examples, private-repository sharing guidance, and automated tests.
