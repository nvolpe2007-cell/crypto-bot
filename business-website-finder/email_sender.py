import asyncio
import logging
import os
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


class EmailSenderBot:
    SMTP_HOST = "smtp.gmail.com"
    SMTP_PORT = 465

    def __init__(self, config: dict) -> None:
        self.sender_email: str = os.getenv("GMAIL_ADDRESS", "")
        self.app_password: str = os.getenv("GMAIL_APP_PASSWORD", "")
        self.sender_name: str = os.getenv(
            "SENDER_NAME",
            config.get("email", {}).get("sender_name", "Nick Volpe"),
        )
        self.send_delay: float = config.get("rate_limits", {}).get("email_send_delay_sec", 30)

    async def send_one(self, to_addr: str, subject: str, body: str) -> bool:
        if not self.sender_email or not self.app_password:
            logger.error(
                "Gmail credentials not set. Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD in .env"
            )
            return False
        try:
            msg = self._build_message(to_addr, subject, body)
            await asyncio.to_thread(self._send_smtp, to_addr, msg)
            logger.info(f"Email sent to {to_addr}")
            return True
        except smtplib.SMTPException as e:
            logger.warning(f"SMTP error sending to {to_addr}: {e}")
            return False
        except Exception as e:
            logger.warning(f"Unexpected error sending to {to_addr}: {e}")
            return False

    def _send_smtp(self, to_addr: str, msg: MIMEMultipart) -> None:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(self.SMTP_HOST, self.SMTP_PORT, context=context) as server:
            server.login(self.sender_email, self.app_password)
            server.sendmail(self.sender_email, to_addr, msg.as_string())

    def _build_message(self, to_addr: str, subject: str, body: str) -> MIMEMultipart:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{self.sender_name} <{self.sender_email}>"
        msg["To"] = to_addr
        html_body = "<br><br>".join(
            f"<p>{para.strip()}</p>"
            for para in body.split("\n\n")
            if para.strip()
        )
        html_body = (
            "<html><body style='font-family:Arial,sans-serif;font-size:14px;color:#222;'>"
            + html_body
            + "</body></html>"
        )
        msg.attach(MIMEText(body, "plain"))
        msg.attach(MIMEText(html_body, "html"))
        return msg
