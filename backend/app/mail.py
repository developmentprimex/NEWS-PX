"""Standalone SMTP transport. Secrets are read only on the API server."""

import asyncio
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from app.config import settings


@dataclass(frozen=True)
class EmailAttachment:
    filename: str
    data: bytes
    content_type: str = "application/pdf"


class GmailService:
    def __init__(self, *, credentials: tuple[str, str] | None = None) -> None:
        sender, password = credentials if credentials is not None else (settings.EMAIL_USER, settings.EMAIL_PASSWORD)
        if not sender or not password:
            raise ValueError("Email credentials are not configured")
        self.sender = sender
        self.password = password.replace(" ", "")
        self.host = settings.EMAIL_HOST
        self.port = settings.EMAIL_PORT

    async def send_verified(self, subject: str, recipients: list[str], body: str, html: str | None = None,
                            *, attachments: list[EmailAttachment] | None = None) -> None:
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body, charset="utf-8")
        if html:
            message.add_alternative(html, subtype="html", charset="utf-8")
        for attachment in attachments or []:
            maintype, subtype = attachment.content_type.split("/", 1)
            message.add_attachment(attachment.data, maintype=maintype, subtype=subtype,
                                   filename=attachment.filename)

        def send() -> None:
            context = ssl.create_default_context()
            connection = (
                smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
                if self.port == 465
                else smtplib.SMTP(self.host, self.port, timeout=30)
            )
            with connection as smtp:
                smtp.ehlo()
                if self.port != 465:
                    smtp.starttls(context=context)
                    smtp.ehlo()
                smtp.login(self.sender, self.password)
                smtp.send_message(message, from_addr=self.sender, to_addrs=recipients)

        await asyncio.to_thread(send)
