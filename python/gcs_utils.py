"""
Google Cloud Storage utility functions.
Uses Application Default Credentials (ADC) on GCE — no key file needed.
Set GCS_BUCKET env var to override the default bucket name.
"""
import json
import os

from google.cloud import storage

BUCKET_NAME = os.environ.get('GCS_BUCKET', 'whatsapp-tiktok-restaurants')

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = storage.Client()
    return _client


def _get_bucket():
    return _get_client().bucket(BUCKET_NAME)


def upload_file(local_path, gcs_path, content_type=None):
    """Upload a local file to GCS."""
    bucket = _get_bucket()
    blob = bucket.blob(gcs_path)
    if content_type:
        blob.content_type = content_type
    blob.upload_from_filename(local_path)
    print(f'[GCS] Uploaded {local_path} -> gs://{BUCKET_NAME}/{gcs_path}')


def upload_json(data, gcs_path):
    """Upload a dict/list as JSON to GCS."""
    bucket = _get_bucket()
    blob = bucket.blob(gcs_path)
    blob.upload_from_string(
        json.dumps(data, indent=2, ensure_ascii=False),
        content_type='application/json',
    )
    print(f'[GCS] Uploaded JSON -> gs://{BUCKET_NAME}/{gcs_path}')


def download_json(gcs_path):
    """Download a JSON file from GCS. Returns None if not found."""
    bucket = _get_bucket()
    blob = bucket.blob(gcs_path)
    if not blob.exists():
        return None
    content = blob.download_as_text()
    return json.loads(content)


def list_blobs(prefix):
    """List blob names under a prefix."""
    bucket = _get_bucket()
    return [blob.name for blob in bucket.list_blobs(prefix=prefix)]
