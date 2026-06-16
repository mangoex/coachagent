from googleapiclient.discovery import build
from services.google_auth import get_user_credentials
from services.storage_service import GoogleStorageService
from typing import Dict, Any
import uuid
import logging

logger = logging.getLogger(__name__)

class GoogleDocsService:
    @staticmethod
    def _get_drive_client(refresh_token: str):
        creds = get_user_credentials(refresh_token)
        return build("drive", "v3", credentials=creds)

    @staticmethod
    def _get_docs_client(refresh_token: str):
        creds = get_user_credentials(refresh_token)
        return build("docs", "v1", credentials=creds)

    @classmethod
    def create_quote_from_template(
        cls, 
        refresh_token: str, 
        template_id: str, 
        replacements: Dict[str, Any]
    ) -> str:
        """
        Clones a Google Docs template, performs a batchUpdate to replace placeholders,
        exports the document to a PDF stream via Google Drive API, uploads it to GCS,
        cleans up the temporary document, and returns a signed 7-day GCS URL.
        
        :param replacements: Dict where keys are template tags (e.g. 'nombre_cliente' for '{{nombre_cliente}}')
        """
        drive_service = cls._get_drive_client(refresh_token)
        docs_service = cls._get_docs_client(refresh_token)

        # 1. Clone the template
        client_name = replacements.get("nombre_cliente", "Cliente")
        unique_id = uuid.uuid4().hex[:6]
        copied_filename = f"Cotizacion_{client_name.replace(' ', '_')}_{unique_id}"
        
        copy_metadata = {"name": copied_filename}
        logger.info(f"Cloning template doc: {template_id} to name: {copied_filename}")
        
        copied_file = drive_service.files().copy(
            fileId=template_id,
            body=copy_metadata
        ).execute()
        
        copied_file_id = copied_file.get("id")
        
        try:
            # 2. Perform batchUpdate replacement in Google Docs
            requests = []
            for key, val in replacements.items():
                requests.append({
                    "replaceAllText": {
                        "containsText": {
                            "text": f"{{{{{key}}}}}",  # Replaces {{key}}
                            "matchCase": True
                        },
                        "replaceText": str(val)
                    }
                })
            
            if requests:
                logger.info(f"Updating placeholders for doc ID: {copied_file_id}")
                docs_service.documents().batchUpdate(
                    documentId=copied_file_id,
                    body={"requests": requests}
                ).execute()

            # 3. Export file as PDF from Drive
            logger.info(f"Exporting doc ID {copied_file_id} to PDF")
            pdf_content = drive_service.files().export_media(
                fileId=copied_file_id,
                mimeType="application/pdf"
            ).execute()

            # 4. Upload PDF to GCS
            destination_blob = f"quotes/quote_{unique_id}.pdf"
            GoogleStorageService.upload_pdf_stream(pdf_content, destination_blob)

            # 5. Generate signed URL
            signed_url = GoogleStorageService.generate_signed_url(destination_blob)
            return signed_url

        finally:
            # 6. Delete temporary Google Doc to keep user's Google Drive clean
            try:
                logger.info(f"Cleaning up temporary doc ID: {copied_file_id}")
                drive_service.files().delete(fileId=copied_file_id).execute()
            except Exception as e:
                logger.warning(f"Could not delete temporary doc ID {copied_file_id}: {str(e)}")
