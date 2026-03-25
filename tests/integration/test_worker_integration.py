"""
Integration tests for the Worker service using LocalStack.

These tests require a running LocalStack instance and are skipped automatically
when the LOCALSTACK_ENDPOINT environment variable is not set.

Run locally:
    docker-compose -f ../../cp-infra/iac/docker-compose.local.yml up -d
    LOCALSTACK_ENDPOINT=http://localhost:4566 pytest tests/integration/ -v
"""

import json
import os
import time

import boto3
import pytest

LOCALSTACK_ENDPOINT = os.environ.get("LOCALSTACK_ENDPOINT", "")

pytestmark = pytest.mark.skipif(
    not LOCALSTACK_ENDPOINT,
    reason="LOCALSTACK_ENDPOINT not set — skipping integration tests",
)

AWS_REGION = os.environ.get("AWS_REGION", "us-east-2")
QUEUE_NAME = "exam-costa-worker-test-messages"
BUCKET_NAME = "exam-costa-worker-test-bucket"


# ==========================================
# LocalStack fixtures
# ==========================================


@pytest.fixture(scope="module")
def aws_clients():
    """Create real boto3 clients pointing at LocalStack."""
    kwargs = {
        "endpoint_url": LOCALSTACK_ENDPOINT,
        "region_name": AWS_REGION,
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
    }
    return {
        "sqs": boto3.client("sqs", **kwargs),
        "s3": boto3.client("s3", **kwargs),
    }


@pytest.fixture(scope="module")
def localstack_resources(aws_clients):
    """Create SQS queue and S3 bucket in LocalStack."""
    sqs = aws_clients["sqs"]
    s3 = aws_clients["s3"]

    # Create SQS queue (or get URL if it already exists)
    try:
        queue_resp = sqs.create_queue(QueueName=QUEUE_NAME)
        queue_url = queue_resp["QueueUrl"]
    except sqs.exceptions.QueueNameExists:
        queue_url = sqs.get_queue_url(QueueName=QUEUE_NAME)["QueueUrl"]

    # Create S3 bucket (ignore if it already exists from bootstrap-local.sh)
    try:
        if AWS_REGION == "us-east-1":
            s3.create_bucket(Bucket=BUCKET_NAME)
        else:
            s3.create_bucket(
                Bucket=BUCKET_NAME,
                CreateBucketConfiguration={"LocationConstraint": AWS_REGION},
            )
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass

    yield {"queue_url": queue_url, "bucket_name": BUCKET_NAME}

    # Cleanup
    try:
        sqs.delete_queue(QueueUrl=queue_url)
    except Exception:
        pass
    try:
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=BUCKET_NAME):
            for obj in page.get("Contents", []):
                s3.delete_object(Bucket=BUCKET_NAME, Key=obj["Key"])
        s3.delete_bucket(Bucket=BUCKET_NAME)
    except Exception:
        pass


@pytest.fixture
def worker(localstack_resources):
    """Create a Worker instance pointing at LocalStack resources."""
    os.environ["LOCALSTACK_ENDPOINT"] = LOCALSTACK_ENDPOINT
    os.environ["SQS_QUEUE_URL"] = localstack_resources["queue_url"]
    os.environ["S3_BUCKET_NAME"] = localstack_resources["bucket_name"]
    os.environ["AWS_ACCESS_KEY_ID"] = "test"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
    os.environ["AWS_REGION"] = AWS_REGION

    import importlib

    import app.worker as worker_module

    importlib.reload(worker_module)

    return worker_module.Worker(
        queue_url=localstack_resources["queue_url"],
        bucket_name=localstack_resources["bucket_name"],
        poll_interval=1,
    )


# ==========================================
# Tests
# ==========================================


def test_worker_processes_message(worker, aws_clients, localstack_resources):
    """Worker should upload message to S3 and delete from SQS."""
    sqs = aws_clients["sqs"]
    s3 = aws_clients["s3"]
    queue_url = localstack_resources["queue_url"]
    bucket_name = localstack_resources["bucket_name"]

    message_body = {
        "message_id": "integration-test-001",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "name": "Worker Integration Test",
        "category": "testing",
        "value": 42.0,
        "description": "Sent via integration test",
    }

    # Send a message to SQS
    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(message_body),
    )

    # Poll once to pick up the message
    messages = worker._poll()
    assert len(messages) == 1, f"Expected 1 message, got {len(messages)}"

    # Process the message
    worker._handle(messages[0])

    # Verify S3 upload
    objects = s3.list_objects_v2(Bucket=bucket_name).get("Contents", [])
    assert len(objects) >= 1, "Expected at least one object in S3"

    # Verify content
    s3_key = objects[0]["Key"]
    obj = s3.get_object(Bucket=bucket_name, Key=s3_key)
    uploaded = json.loads(obj["Body"].read())
    assert uploaded["name"] == "Worker Integration Test"
    assert "_worker_metadata" in uploaded
    assert "processed_at" in uploaded["_worker_metadata"]

    # Verify message was deleted from SQS (queue should be empty after short wait)
    time.sleep(1)
    remaining = sqs.receive_message(
        QueueUrl=queue_url,
        MaxNumberOfMessages=10,
        WaitTimeSeconds=1,
    ).get("Messages", [])
    assert len(remaining) == 0, "Message should have been deleted from SQS"


def test_worker_stats_incremented(worker, aws_clients, localstack_resources):
    """Worker stats should track processed count."""
    sqs = aws_clients["sqs"]
    queue_url = localstack_resources["queue_url"]

    initial_processed = worker.stats["processed"]

    sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(
            {
                "message_id": "stats-test-001",
                "timestamp": "2026-01-01T00:00:00+00:00",
                "name": "Stats Test",
                "category": "stats",
                "value": 1.0,
                "description": "Stats counter test",
            }
        ),
    )

    messages = worker._poll()
    if messages:
        worker._handle(messages[0])
        assert worker.stats["processed"] == initial_processed + 1


def test_worker_handles_invalid_json(worker, aws_clients, localstack_resources):
    """Worker should increment failed counter for invalid JSON, not crash."""
    sqs = aws_clients["sqs"]
    queue_url = localstack_resources["queue_url"]

    sqs.send_message(QueueUrl=queue_url, MessageBody="this is not json {{{")

    messages = worker._poll()
    if messages:
        initial_failed = worker.stats["failed"]
        worker._handle(messages[0])
        assert worker.stats["failed"] == initial_failed + 1
