"""
Pytest configuration for Worker service tests.
Sets environment variables before any test module imports.
"""

import os

os.environ.setdefault(
    "SQS_QUEUE_URL", "https://sqs.us-east-2.amazonaws.com/123456789012/test-queue"
)
os.environ.setdefault("S3_BUCKET_NAME", "test-messages-bucket")
os.environ.setdefault("AWS_REGION", "us-east-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("POLL_INTERVAL", "1")
