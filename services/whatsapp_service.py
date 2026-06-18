import requests
import logging
from config.settings import settings
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class WhatsAppService:
    @staticmethod
    def _get_headers(token: str) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

    @staticmethod
    def _send_post_request(payload: Dict[str, Any], custom_token: Optional[str] = None, custom_phone_id: Optional[str] = None) -> bool:
        """
        Sends the payload to the Meta WhatsApp Business API.
        If custom_token/phone_id are provided, uses them. Otherwise falls back to global settings.
        """
        phone_id = custom_phone_id or settings.WHATSAPP_PHONE_NUMBER_ID
        token = custom_token or settings.WHATSAPP_TOKEN

        # Check if running with mock settings
        if not token or token == "EAAXxXX..." or not phone_id or phone_id == "1234567890":
            logger.info(f"[MOCK WHATSAPP] Sending message payload: {payload}")
            return True

        url = f"https://graph.facebook.com/v19.0/{phone_id}/messages"
        headers = WhatsAppService._get_headers(token)

        try:
            response = requests.post(url, json=payload, headers=headers, timeout=10)
            if response.status_code in [200, 201]:
                logger.info(f"WhatsApp message sent successfully to {payload.get('to')}")
                return True
            else:
                logger.error(f"Failed to send WhatsApp message. Status: {response.status_code}, Response: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Exception while sending WhatsApp message: {str(e)}")
            return False

    @classmethod
    def send_text_message(cls, to_phone: str, text: str, token: Optional[str] = None, phone_id: Optional[str] = None) -> bool:
        """
        Sends a simple text message.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "text",
            "text": {
                "preview_url": True,
                "body": text
            }
        }
        return cls._send_post_request(payload, token, phone_id)

    @classmethod
    def send_template_message(cls, to_phone: str, template_name: str, language_code: str = "es_MX", components: Optional[List[Dict[str, Any]]] = None, token: Optional[str] = None, phone_id: Optional[str] = None) -> bool:
        """
        Sends a pre-approved template message to bypass the 24-hour window constraint.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {
                    "code": language_code
                }
            }
        }
        if components:
            payload["template"]["components"] = components
            
        return cls._send_post_request(payload, token, phone_id)

    @classmethod
    def send_document_message(cls, to_phone: str, document_url: str, filename: str, caption: Optional[str] = None, token: Optional[str] = None, phone_id: Optional[str] = None) -> bool:
        """
        Sends a document (e.g. a PDF quotation) via its URL.
        """
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "document",
            "document": {
                "link": document_url,
                "filename": filename,
                "caption": caption or "Tu cotización está lista."
            }
        }
        return cls._send_post_request(payload, token, phone_id)

    @classmethod
    def send_interactive_buttons(cls, to_phone: str, body_text: str, buttons: List[Dict[str, str]], token: Optional[str] = None, phone_id: Optional[str] = None) -> bool:
        """
        Sends an interactive button message.
        :param buttons: List of dicts with 'id' and 'title'. Max 3 buttons.
                        Example: [{"id": "btn_1", "title": "Option 1"}]
        """
        if len(buttons) > 3:
            raise ValueError("WhatsApp interactive messages support a maximum of 3 buttons.")

        formatted_buttons = []
        for btn in buttons:
            formatted_buttons.append({
                "type": "reply",
                "reply": {
                    "id": btn["id"],
                    "title": btn["title"][:20]  # WhatsApp title length limit is 20 chars
                }
            })

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to_phone,
            "type": "interactive",
            "interactive": {
                "type": "button",
                "body": {
                    "text": body_text
                },
                "action": {
                    "buttons": formatted_buttons
                }
            }
        }
        return cls._send_post_request(payload, token, phone_id)
