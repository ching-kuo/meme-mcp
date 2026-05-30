"""S3ImageStore unit tests via moto. Real-endpoint smoke lives in
tests/test_s3_image_store_smoke.py, gated on MEMEMCP_TEST_S3_ENDPOINT.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from meme_mcp.rendering.image_store import S3ImageStore


@pytest.fixture()
def s3_store():  # type: ignore[no-untyped-def]
    with mock_aws():
        client = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="test",
            aws_secret_access_key="test",
        )
        client.create_bucket(Bucket="meme-test")
        store = S3ImageStore(
            endpoint=None,  # type: ignore[arg-type]
            bucket="meme-test",
            region="us-east-1",
            access_key_id="test",
            secret_access_key="test",
        )
        yield store


def test_put_then_get_roundtrip(s3_store: S3ImageStore) -> None:
    content = b"hello world"
    path = s3_store.put(content, "png")
    assert s3_store.get(path) == content


def test_content_addressed_keys_match_filesystem_shape(s3_store: S3ImageStore) -> None:
    content = b"deterministic"
    path1 = s3_store.put(content, "png")
    path2 = s3_store.put(content, "png")
    assert path1 == path2
    # Layout: <2-char shard>/<14-char suffix>.png
    assert path1[2] == "/"
    assert path1.endswith(".png")


def test_different_content_produces_different_keys(s3_store: S3ImageStore) -> None:
    a = s3_store.put(b"a", "png")
    b = s3_store.put(b"b", "png")
    assert a != b


def test_idempotent_put_via_head_then_put(s3_store: S3ImageStore) -> None:
    """The second put for the same content must not overwrite — assert via byte
    comparison after a deliberate manual overwrite, which the second put should NOT roll
    back."""
    content = b"original"
    path = s3_store.put(content, "png")
    # Manually overwrite the object — the second put with same content sees HeadObject
    # succeed and short-circuits.
    s3_store.client.put_object(Bucket=s3_store.bucket, Key=path, Body=b"manual override")
    s3_store.put(content, "png")
    assert s3_store.get(path) == b"manual override"


def test_get_missing_raises_file_not_found(s3_store: S3ImageStore) -> None:
    with pytest.raises(FileNotFoundError):
        s3_store.get("dead/beef.png")


def test_path_for_matches_put(s3_store: S3ImageStore) -> None:
    content = b"hello content addressing"
    assert s3_store.path_for(content, "png") == s3_store.put(content, "png")


def test_delete_removes_object_and_get_then_raises(s3_store: S3ImageStore) -> None:
    path = s3_store.put(b"deletable", "png")
    assert s3_store.delete(path) is True
    with pytest.raises(FileNotFoundError):
        s3_store.get(path)


def test_delete_absent_key_is_idempotent(s3_store: S3ImageStore) -> None:
    # DeleteObject on a key that never existed succeeds (idempotent).
    assert s3_store.delete("dead/beef.png") is True
