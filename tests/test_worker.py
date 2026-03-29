"""
Unit tests for Worker service.
Uses mocked AWS clients - no real AWS calls.
"""

import json
from unittest.mock import patch

import pytest
from botocore.exceptions import ClientError


# ==========================================
# Fixtures
# ==========================================
QUEUE_URL = "https://sqs.us-east-2.amazonaws.com/123456789012/test-queue"
BUCKET_NAME = "test-messages-bucket"

SAMPLE_MESSAGE_BODY = {
    "message_id": "abc-123",
    "timestamp": "2024-01-01T00:00:00+00:00",
    "name": "Test Item",
    "category": "test",
    "value": 42.0,
    "description": "Test description",
}

SAMPLE_SQS_MESSAGE = {
    "MessageId": "sqs-msg-id-123",
    "ReceiptHandle": "receipt-handle-abc",
    "Body": json.dumps(SAMPLE_MESSAGE_BODY),
}


@pytest.fixture
def worker_env(monkeypatch):
    monkeypatch.setenv("SQS_QUEUE_URL", QUEUE_URL)
    monkeypatch.setenv("S3_BUCKET_NAME", BUCKET_NAME)
    monkeypatch.setenv("POLL_INTERVAL", "1")


@pytest.fixture
def mock_sqs():
    with patch("app.worker.sqs_client") as mock:
        yield mock


@pytest.fixture
def mock_s3():
    with patch("app.worker.s3_client") as mock:
        yield mock


@pytest.fixture
def worker(worker_env):
    from app.worker import Worker

    return Worker(queue_url=QUEUE_URL, bucket_name=BUCKET_NAME)


# ==========================================
# Worker Initialization Tests
# ==========================================
class TestWorkerInit:
    def test_worker_created_with_correct_queue(self, worker):
        assert worker.queue_url == QUEUE_URL

    def test_worker_created_with_correct_bucket(self, worker):
        assert worker.bucket_name == BUCKET_NAME

    def test_worker_not_running_initially(self, worker):
        assert worker._running is False

    def test_initial_stats_zero(self, worker):
        stats = worker.stats
        assert stats["processed"] == 0
        assert stats["failed"] == 0


# ==========================================
# SQS Polling Tests
# ==========================================
class TestSQSPolling:
    def test_poll_returns_messages(self, worker, mock_sqs):
        mock_sqs.receive_message.return_value = {"Messages": [SAMPLE_SQS_MESSAGE]}
        messages = worker._poll()
        assert len(messages) == 1

    def test_poll_returns_empty_when_no_messages(self, worker, mock_sqs):
        mock_sqs.receive_message.return_value = {}
        messages = worker._poll()
        assert messages == []

    def test_poll_uses_long_polling(self, worker, mock_sqs):
        mock_sqs.receive_message.return_value = {}
        worker._poll()
        call_kwargs = mock_sqs.receive_message.call_args[1]
        assert call_kwargs["WaitTimeSeconds"] > 0

    def test_poll_requests_max_10_messages(self, worker, mock_sqs):
        mock_sqs.receive_message.return_value = {}
        worker._poll()
        call_kwargs = mock_sqs.receive_message.call_args[1]
        assert call_kwargs["MaxNumberOfMessages"] == 10

    def test_poll_handles_sqs_error_gracefully(self, worker, mock_sqs):
        mock_sqs.receive_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist", "Message": "Queue not found"}},
            "ReceiveMessage",
        )
        messages = worker._poll()
        assert messages == []


# ==========================================
# S3 Upload Tests
# ==========================================
class TestS3Upload:
    def test_upload_succeeds(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")
        mock_s3.put_object.assert_called_once()

    def test_upload_uses_correct_bucket(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == BUCKET_NAME

    def test_upload_key_has_date_prefix(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Key"].startswith("messages/")

    def test_upload_key_ends_with_message_id(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-xyz")
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Key"].endswith("msg-xyz.json")

    def test_upload_content_type_is_json(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["ContentType"] == "application/json"

    def test_uploaded_body_contains_original_data(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")
        call_kwargs = mock_s3.put_object.call_args[1]
        uploaded = json.loads(call_kwargs["Body"])
        assert uploaded["name"] == SAMPLE_MESSAGE_BODY["name"]
        assert uploaded["category"] == SAMPLE_MESSAGE_BODY["category"]
        assert uploaded["value"] == SAMPLE_MESSAGE_BODY["value"]

    def test_upload_adds_worker_metadata(self, worker, mock_s3):
        worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")
        call_kwargs = mock_s3.put_object.call_args[1]
        uploaded = json.loads(call_kwargs["Body"])
        assert "_worker_metadata" in uploaded
        assert "processed_at" in uploaded["_worker_metadata"]

    def test_upload_failure_raises_s3_upload_error(self, worker, mock_s3):
        from app.worker import S3UploadError

        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
            "PutObject",
        )
        with pytest.raises(S3UploadError):
            worker._upload_to_s3(SAMPLE_MESSAGE_BODY, "msg-123")


# ==========================================
# Message Handling Tests
# ==========================================
class TestMessageHandling:
    def test_handle_processes_valid_message(self, worker, mock_sqs, mock_s3):
        worker._handle(SAMPLE_SQS_MESSAGE)
        mock_s3.put_object.assert_called_once()
        mock_sqs.delete_message.assert_called_once()

    def test_handle_deletes_after_s3_upload(self, worker, mock_sqs, mock_s3):
        """Ensure delete happens AFTER successful S3 upload."""
        call_order = []
        mock_s3.put_object.side_effect = lambda **kwargs: call_order.append("s3")
        mock_sqs.delete_message.side_effect = lambda **kwargs: call_order.append(
            "sqs_delete"
        )

        worker._handle(SAMPLE_SQS_MESSAGE)
        assert call_order == ["s3", "sqs_delete"]

    def test_handle_increments_processed_counter(self, worker, mock_sqs, mock_s3):
        worker._handle(SAMPLE_SQS_MESSAGE)
        assert worker.stats["processed"] == 1

    def test_handle_invalid_json_increments_failed(self, worker, mock_sqs, mock_s3):
        bad_message = {**SAMPLE_SQS_MESSAGE, "Body": "not valid json {"}
        worker._handle(bad_message)
        assert worker.stats["failed"] == 1

    def test_handle_invalid_json_does_not_delete_from_sqs(
        self, worker, mock_sqs, mock_s3
    ):
        bad_message = {**SAMPLE_SQS_MESSAGE, "Body": "invalid"}
        worker._handle(bad_message)
        mock_sqs.delete_message.assert_not_called()

    def test_handle_s3_failure_does_not_delete_from_sqs(
        self, worker, mock_sqs, mock_s3
    ):
        """If S3 fails, message must NOT be deleted from SQS (so it retries)."""
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
            "PutObject",
        )
        worker._handle(SAMPLE_SQS_MESSAGE)
        mock_sqs.delete_message.assert_not_called()

    def test_handle_s3_failure_increments_failed_counter(
        self, worker, mock_sqs, mock_s3
    ):
        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "Bucket not found"}},
            "PutObject",
        )
        worker._handle(SAMPLE_SQS_MESSAGE)
        assert worker.stats["failed"] == 1

    def test_handle_deletes_from_correct_queue(self, worker, mock_sqs, mock_s3):
        worker._handle(SAMPLE_SQS_MESSAGE)
        call_kwargs = mock_sqs.delete_message.call_args[1]
        assert call_kwargs["QueueUrl"] == QUEUE_URL

    def test_handle_uses_correct_receipt_handle(self, worker, mock_sqs, mock_s3):
        worker._handle(SAMPLE_SQS_MESSAGE)
        call_kwargs = mock_sqs.delete_message.call_args[1]
        assert call_kwargs["ReceiptHandle"] == SAMPLE_SQS_MESSAGE["ReceiptHandle"]


# ==========================================
# Stats Tests
# ==========================================
class TestStats:
    def test_stats_track_multiple_messages(self, worker, mock_sqs, mock_s3):
        worker._handle(SAMPLE_SQS_MESSAGE)
        worker._handle(SAMPLE_SQS_MESSAGE)
        worker._handle(SAMPLE_SQS_MESSAGE)
        assert worker.stats["processed"] == 3

    def test_stats_track_failures(self, worker, mock_sqs, mock_s3):
        bad = {**SAMPLE_SQS_MESSAGE, "Body": "invalid json"}
        worker._handle(bad)
        worker._handle(bad)
        assert worker.stats["failed"] == 2

    def test_stats_returns_copy_not_reference(self, worker):
        stats = worker.stats
        stats["processed"] = 999
        assert worker._stats["processed"] == 0
