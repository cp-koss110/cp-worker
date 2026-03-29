"""
Worker Service - DevOps Exam Costa
Polls SQS, uploads messages to S3, deletes from queue after success
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from prometheus_client import REGISTRY, Counter, Histogram, Info, start_http_server

# ==========================================
# Logging — JSON format
# ==========================================
class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%SZ"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)

_handler = logging.StreamHandler()
_handler.setFormatter(_JsonFormatter())
logging.root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
logging.root.handlers = [_handler]

# Keep AWS/HTTP internals quiet regardless of LOG_LEVEL
logging.getLogger("botocore").setLevel(logging.WARNING)
logging.getLogger("boto3").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ==========================================
# Configuration
# ==========================================
AWS_REGION = os.getenv("AWS_REGION", "us-east-2")
APP_VERSION = os.getenv("APP_VERSION", "unknown")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "5"))
MAX_MESSAGES = int(os.getenv("MAX_MESSAGES_PER_POLL", "10"))
VISIBILITY_TIMEOUT = int(os.getenv("SQS_VISIBILITY_TIMEOUT", "30"))
WAIT_TIME_SECONDS = int(os.getenv("SQS_WAIT_TIME_SECONDS", "20"))  # Long polling
METRICS_PORT = int(os.getenv("METRICS_PORT", "8001"))

# LocalStack support
LOCALSTACK_ENDPOINT = os.getenv("LOCALSTACK_ENDPOINT", "")
AWS_KWARGS: Dict[str, Any] = {"region_name": AWS_REGION}
if LOCALSTACK_ENDPOINT:
    AWS_KWARGS["endpoint_url"] = LOCALSTACK_ENDPOINT

# ==========================================
# AWS Clients
# ==========================================
sqs_client = boto3.client("sqs", **AWS_KWARGS)
s3_client = boto3.client("s3", **AWS_KWARGS)

# ==========================================
# Prometheus Metrics
# ==========================================
def _safe_register(cls, name, *args, **kwargs):
    """Register a metric or return the existing one.

    importlib.reload() re-runs module-level code against the same global
    CollectorRegistry, causing ValueError on duplicate names. This helper
    returns the already-registered collector instead of raising.
    """
    try:
        return cls(name, *args, **kwargs)
    except ValueError:
        for collector in set(REGISTRY._names_to_collectors.values()):
            if getattr(collector, "_name", None) == name:
                return collector
        raise


BUILD_INFO = _safe_register(Info, "worker_build", "Worker build information")
BUILD_INFO.info({"version": APP_VERSION, "service": "worker"})

MESSAGES_POLLED = _safe_register(
    Counter, "worker_messages_polled", "Total messages received from SQS"
)
MESSAGES_PROCESSED = _safe_register(
    Counter, "worker_messages_processed", "Messages processed by the worker", ["status"]
)
S3_UPLOADS = _safe_register(
    Counter, "worker_s3_uploads", "S3 upload attempts", ["status"]
)
PROCESSING_DURATION = _safe_register(
    Histogram,
    "worker_message_processing_duration_seconds",
    "End-to-end time to process one SQS message",
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)


# ==========================================
# Worker Logic
# ==========================================
class Worker:
    """SQS → S3 message processor."""

    def __init__(
        self,
        queue_url: str,
        bucket_name: str,
        poll_interval: int = POLL_INTERVAL,
    ) -> None:
        self.queue_url = queue_url
        self.bucket_name = bucket_name
        self.poll_interval = poll_interval
        self._running = False
        self._stats = {"processed": 0, "failed": 0, "started_at": datetime.now(timezone.utc).isoformat()}

    def start(self) -> None:
        """Start the worker loop (blocking)."""
        logger.info(
            "Worker starting: queue=%s bucket=%s interval=%ds",
            self.queue_url,
            self.bucket_name,
            self.poll_interval,
        )
        self._running = True

        while self._running:
            try:
                messages = self._poll()
                if messages:
                    logger.info("Received %d message(s)", len(messages))
                    for msg in messages:
                        self._handle(msg)
                else:
                    logger.debug("No messages, waiting %ds", self.poll_interval)
                    time.sleep(self.poll_interval)
            except KeyboardInterrupt:
                logger.info("Interrupted, stopping worker")
                break
            except Exception as e:
                logger.error("Unexpected error in worker loop: %s", e, exc_info=True)
                time.sleep(self.poll_interval)

        logger.info(
            "Worker stopped. Stats: processed=%d failed=%d",
            self._stats["processed"],
            self._stats["failed"],
        )

    def stop(self) -> None:
        """Signal the worker to stop after current message."""
        logger.info("Worker stopping...")
        self._running = False

    def _poll(self) -> List[Dict]:
        """Poll SQS for messages using long polling."""
        try:
            response = sqs_client.receive_message(
                QueueUrl=self.queue_url,
                MaxNumberOfMessages=MAX_MESSAGES,
                WaitTimeSeconds=WAIT_TIME_SECONDS,
                VisibilityTimeout=VISIBILITY_TIMEOUT,
                MessageAttributeNames=["All"],
            )
            messages = response.get("Messages", [])
            if messages:
                MESSAGES_POLLED.inc(len(messages))
            return messages
        except (ClientError, BotoCoreError) as e:
            logger.error("SQS poll error: %s", e)
            time.sleep(self.poll_interval)
            return []

    def _handle(self, message: Dict) -> None:
        """Process a single SQS message: upload to S3, then delete."""
        receipt_handle = message.get("ReceiptHandle", "")
        message_id = message.get("MessageId", "unknown")

        start = time.perf_counter()
        try:
            body = json.loads(message.get("Body", "{}"))
            logger.info("Processing message: id=%s", message_id)

            # Upload to S3
            self._upload_to_s3(body, message_id)

            # Delete from queue (only after successful S3 upload)
            self._delete_from_sqs(receipt_handle, message_id)

            self._stats["processed"] += 1
            MESSAGES_PROCESSED.labels(status="success").inc()
            logger.info("Message processed successfully: id=%s", message_id)

        except json.JSONDecodeError as e:
            logger.error("Invalid JSON in message %s: %s", message_id, e)
            self._stats["failed"] += 1
            MESSAGES_PROCESSED.labels(status="failed").inc()
            # Don't delete - let it expire and go to DLQ

        except S3UploadError as e:
            logger.error("S3 upload failed for message %s: %s", message_id, e)
            self._stats["failed"] += 1
            MESSAGES_PROCESSED.labels(status="failed").inc()
            # Don't delete - message stays in queue, will retry

        except Exception as e:
            logger.error("Unexpected error processing message %s: %s", message_id, e, exc_info=True)
            self._stats["failed"] += 1
            MESSAGES_PROCESSED.labels(status="failed").inc()

        finally:
            PROCESSING_DURATION.observe(time.perf_counter() - start)

    def _upload_to_s3(self, body: Dict, message_id: str) -> None:
        """Upload message body as JSON to S3."""
        timestamp = datetime.now(timezone.utc)
        date_prefix = timestamp.strftime("%Y/%m/%d")
        s3_key = f"messages/{date_prefix}/{message_id}.json"

        # Enrich with processing metadata
        enriched = {
            **body,
            "_worker_metadata": {
                "processed_at": timestamp.isoformat(),
                "s3_key": s3_key,
                "worker_version": APP_VERSION,
            },
        }

        try:
            s3_client.put_object(
                Bucket=self.bucket_name,
                Key=s3_key,
                Body=json.dumps(enriched, indent=2),
                ContentType="application/json",
                Metadata={
                    "message-id": message_id,
                    "processed-at": timestamp.isoformat(),
                },
            )
            S3_UPLOADS.labels(status="success").inc()
            logger.info("Uploaded to S3: s3://%s/%s", self.bucket_name, s3_key)
        except (ClientError, BotoCoreError) as e:
            S3_UPLOADS.labels(status="failed").inc()
            raise S3UploadError(f"S3 upload failed: {e}") from e

    def _delete_from_sqs(self, receipt_handle: str, message_id: str) -> None:
        """Delete message from SQS after successful processing."""
        try:
            sqs_client.delete_message(
                QueueUrl=self.queue_url,
                ReceiptHandle=receipt_handle,
            )
            logger.debug("Deleted message from SQS: id=%s", message_id)
        except (ClientError, BotoCoreError) as e:
            logger.error("Failed to delete message %s from SQS: %s", message_id, e)
            raise

    @property
    def stats(self) -> Dict:
        return dict(self._stats)


class S3UploadError(Exception):
    """Raised when S3 upload fails."""


# ==========================================
# Signal Handlers
# ==========================================
_worker_instance: Optional[Worker] = None


def _handle_signal(signum: int, frame: Any) -> None:
    logger.info("Received signal %d, initiating graceful shutdown", signum)
    if _worker_instance:
        _worker_instance.stop()


# ==========================================
# Entry Point
# ==========================================
def main() -> None:
    global _worker_instance

    # Validate required config
    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL is not set")
        sys.exit(1)
    if not S3_BUCKET_NAME:
        logger.error("S3_BUCKET_NAME is not set")
        sys.exit(1)

    # Start Prometheus metrics HTTP server in background thread
    metrics_thread = threading.Thread(
        target=start_http_server,
        args=(METRICS_PORT,),
        daemon=True,
    )
    metrics_thread.start()
    logger.info("Prometheus metrics server started on port %d", METRICS_PORT)

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    _worker_instance = Worker(
        queue_url=SQS_QUEUE_URL,
        bucket_name=S3_BUCKET_NAME,
        poll_interval=POLL_INTERVAL,
    )
    _worker_instance.start()


if __name__ == "__main__":
    main()
