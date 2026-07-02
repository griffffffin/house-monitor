"""SMTP email notification, sent via Gmail."""

import asyncio
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Any, Dict


class EmailNotifier:
    """Sends plain-text emails over SMTP. Runs the blocking smtplib call in
    a thread pool executor so it doesn't block the asyncio event loop."""

    def __init__(self, config: Dict[str, Any]):
        self.config = config

    async def send(self, subject: str, body: str) -> bool:
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(None, self._send_sync, subject, body)
        except Exception as e:
            logging.error(f"Email send error (async): {e}")
            return False

    def _send_sync(self, subject: str, body: str) -> bool:
        try:
            msg = MIMEMultipart()
            msg["From"] = self.config["sender_email"]
            msg["To"] = self.config["recipient_email"]
            msg["Subject"] = subject
            msg.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(self.config["smtp_server"], self.config["smtp_port"]) as server:
                server.starttls()
                server.login(self.config["sender_email"], self.config["sender_password"])
                server.send_message(msg)
            return True
        except Exception as e:
            logging.error(f"Email send error (sync): {e}")
            return False
