from google.cloud import storage
from datetime import timedelta
from config.settings import settings
import logging

logger = logging.getLogger(__name__)

class GoogleStorageService:
    @staticmethod
    def _get_client() -> storage.Client:
        # Falls back to default credentials in GCP environment or local env
        return storage.Client(project=settings.GCP_PROJECT_ID)

    @classmethod
    def upload_pdf_stream(cls, pdf_content: bytes, destination_blob_name: str) -> str:
        """
        Uploads raw PDF bytes to the GCS bucket.
        :param pdf_content: Binary PDF data.
        :param destination_blob_name: Name of the destination file inside the bucket (e.g. 'quotes/quote_123.pdf').
        """
        client = cls._get_client()
        bucket = client.bucket(settings.GCS_BUCKET_NAME)
        
        # Create bucket if it doesn't exist (Only for development ease)
        if not bucket.exists():
            try:
                bucket = client.create_bucket(settings.GCS_BUCKET_NAME)
            except Exception as e:
                logger.error(f"Failed to automatically create GCS bucket: {str(e)}")

        blob = bucket.blob(destination_blob_name)
        blob.upload_from_string(pdf_content, content_type="application/pdf")
        logger.info(f"Successfully uploaded PDF to GCS: {destination_blob_name}")
        return destination_blob_name

    @classmethod
    def generate_signed_url(cls, blob_name: str, expiration_days: int = 7) -> str:
        """
        Generates a signed URL with 7-day expiration.
        """
        client = cls._get_client()
        bucket = client.bucket(settings.GCS_BUCKET_NAME)
        blob = bucket.blob(blob_name)

        try:
            # Signing URLs requires a service account credential with IAM permissions.
            url = blob.generate_signed_url(
                version="v4",
                expiration=timedelta(days=expiration_days),
                method="GET"
            )
            return url
        except Exception as e:
            logger.warning(
                f"Could not generate signed URL: {str(e)}. "
                "Ensure credentials have iam.serviceAccounts.signBlob permission. "
                "Returning public URL fallback."
            )
            # Fallback to standard storage link (assumes bucket/object might be public, or for dev)
            return f"https://storage.googleapis.com/{settings.GCS_BUCKET_NAME}/{blob_name}"
