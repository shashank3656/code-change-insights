# Changelog

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
