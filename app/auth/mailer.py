from email.message import EmailMessage

import aiosmtplib

from app.config import settings


async def _send(to_email: str, subject: str, body: str) -> None:
    if not settings.smtp_host:
        raise RuntimeError("SMTP_HOST is not configured")
    message = EmailMessage()
    message["From"] = settings.email_from
    message["To"] = to_email
    message["Subject"] = subject
    message.set_content(body)
    await aiosmtplib.send(
        message,
        hostname=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        start_tls=settings.smtp_starttls,
        use_tls=settings.smtp_use_tls,
        timeout=10,
    )


async def send_verification_email(to_email: str, token: str) -> None:
    url = f"{settings.email_verification_url}?token={token}"
    await _send(
        to_email,
        "Verify your TG SaaS email",
        "Use the link below to verify your email address. This link expires in 30 minutes.\n\n"
        f"{url}\n\nIf you did not create this account, ignore this message.",
    )


async def send_password_reset_email(to_email: str, token: str) -> None:
    url = f"{settings.password_reset_url}?token={token}"
    await _send(
        to_email,
        "Reset your TG SaaS password",
        "Use the link below to reset your password. This link expires in 30 minutes.\n\n"
        f"{url}\n\nIf you did not request this, ignore this message.",
    )
