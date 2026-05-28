"""Real-endpoint smoke test against MinIO (or any S3-compatible endpoint).

Gated on the MEMEMCP_TEST_S3_ENDPOINT / MEMEMCP_TEST_S3_BUCKET env pair so it stays
out of routine local runs. Bring up `deploy/docker-compose.test.yml` for a local MinIO.
"""

from __future__ import annotations

import os

import pytest

ENDPOINT = os.environ.get("MEMEMCP_TEST_S3_ENDPOINT")
BUCKET = os.environ.get("MEMEMCP_TEST_S3_BUCKET")
ACCESS_KEY = os.environ.get("MEMEMCP_TEST_S3_ACCESS_KEY", "memetest")
SECRET_KEY = os.environ.get("MEMEMCP_TEST_S3_SECRET_KEY", "memetest123")
REGION = os.environ.get("MEMEMCP_TEST_S3_REGION", "us-east-1")

if not ENDPOINT or not BUCKET:
    pytest.skip(
        "Set MEMEMCP_TEST_S3_ENDPOINT and MEMEMCP_TEST_S3_BUCKET to run the MinIO "
        "smoke (see deploy/docker-compose.test.yml).",
        allow_module_level=True,
    )

from meme_mcp.rendering.image_store import S3ImageStore  # noqa: E402


@pytest.fixture()
def s3_store() -> S3ImageStore:
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name=REGION,
    )
    try:
        client.head_bucket(Bucket=BUCKET)
    except Exception:
        client.create_bucket(Bucket=BUCKET)
    return S3ImageStore(
        endpoint=ENDPOINT,
        bucket=BUCKET,
        region=REGION,
        access_key_id=ACCESS_KEY,
        secret_access_key=SECRET_KEY,
    )


def test_smoke_round_trip(s3_store: S3ImageStore) -> None:
    content = b"minio-smoke-payload"
    path = s3_store.put(content, "png")
    assert s3_store.get(path) == content
