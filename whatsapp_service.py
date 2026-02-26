import os
import logging
from twilio.rest import Client

log = logging.getLogger(__name__)


class WhatsAppService:
    def __init__(self):
        account_sid = os.environ.get("TWILIO_ACCOUNT_SID")
        auth_token  = os.environ.get("TWILIO_AUTH_TOKEN")
        from_number = os.environ.get("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")

        if not account_sid or not auth_token:
            raise ValueError("TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN must be set")

        self.client      = Client(account_sid, auth_token)
        self.from_number = from_number
        log.info(f"WhatsApp service initialised (from {self.from_number})")

    def send_message(self, to_number: str, message: str) -> str:
        """Send a WhatsApp message. `to_number` should be in E.164 format (+919XXXXXXXXX)."""
        # Ensure the number has whatsapp: prefix
        if not to_number.startswith("whatsapp:"):
            to_number = f"whatsapp:{to_number}"

        try:
            msg = self.client.messages.create(
                from_=self.from_number,
                to=to_number,
                body=message,
            )
            log.info(f"Message sent to {to_number} – SID: {msg.sid}")
            return msg.sid
        except Exception as e:
            log.error(f"Failed to send WhatsApp message to {to_number}: {e}")
            raise
