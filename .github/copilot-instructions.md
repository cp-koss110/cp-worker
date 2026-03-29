# Copilot Code Review Instructions — cp-worker

## What this service does
SQS consumer microservice that polls a queue on a configurable interval, uploads each message as a JSON object to S3, then deletes it from the queue.

## Stack
- Python 3.12, boto3, Prometheus client
- Deployed on ECS Fargate
- LocalStack for local development

## Review priorities
- **Message ordering**: S3 upload must always complete successfully before the SQS `delete_message` call. If S3 fails, the message must stay in the queue for retry.
- **Error handling**: distinguish between `S3UploadError` (don't delete) and transient errors. No silent swallowing.
- **AWS calls**: all boto3 calls should handle `ClientError` and `BotoCoreError`.
- **Poll interval**: controlled by `POLL_INTERVAL_SECONDS` env var. Default is safe for production — do not hardcode.
- **Tests**: unit tests mock boto3 clients. Integration tests use LocalStack with hardcoded `test`/`test` credentials — never real AWS creds.
- **Dependencies**: `requirements.txt` for runtime, `requirements-dev.txt` for dev/test. `boto3` and `botocore` must always be pinned to matching versions.

## What to ignore
- `coverage.xml`, `coverage-summary.md` — generated files
- `.secrets.baseline` — detect-secrets baseline, not sensitive
- `.actrc`, `.secrets.example` — local tooling config
