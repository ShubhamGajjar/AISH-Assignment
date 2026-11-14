"""Custom Gmail MCP server exposing a ``send_email`` tool.

Transport: stdio (spawned by an MCP client: this project's pipeline or Claude Code).
Delivery:  Gmail SMTP over implicit TLS (smtp.gmail.com:465), authenticated with a Gmail
           App Password, so no Google Cloud project or OAuth consent screen is needed.

Configuration comes from the environment (normally the ``env`` block of the MCP config):
    GMAIL_ADDRESS             the Gmail account that sends the mail (required)
    GMAIL_APP_PASSWORD        a 16-character Gmail App Password (required)
    GMAIL_ALLOWED_RECIPIENTS  optional comma-separated allowlist; when set, any other
                              recipient is refused (a guard against mailing real people)

Note: with the stdio transport, stdout carries the JSON-RPC protocol, so this module only
logs to stderr.
"""
from __future__ import annotations

import logging
import os
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import format_datetime, formataddr, make_msgid

from mcp.server.mcpserver import MCPServer
# MCP SDK v2 returns only a generic "Error executing tool" for ordinary exceptions; a
# ToolError's message is passed to the client, so expected failures are raised as ToolError.
from mcp.server.mcpserver.exceptions import ToolError

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s gmail-mcp %(levelname)s %(message)s")
log = logging.getLogger("gmail-mcp")

SMTP_HOST = os.environ.get("GMAIL_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("GMAIL_SMTP_PORT", "465"))

server = MCPServer(
    "gmail",
    instructions="Sends plain-text email from the configured Gmail account via the send_email tool.",
)


def _allowed(recipient: str) -> bool:
    allow = os.environ.get("GMAIL_ALLOWED_RECIPIENTS", "").strip()
    if not allow:
        return True
    return recipient.strip().lower() in {a.strip().lower() for a in allow.split(",") if a.strip()}


@server.tool(description=(
    "Send a plain-text email from the configured Gmail account. "
    "Optional 'references' / 'in_reply_to' set the threading headers (message-id values "
    "such as '<id@host>'); 'original_recipient' is recorded in an X-Original-To header."
))
def send_email(
    to: str,
    subject: str,
    body: str,
    references: str | None = None,
    in_reply_to: str | None = None,
    original_recipient: str | None = None,
    from_name: str = "Email Deduplication System",
) -> dict:
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        raise ToolError("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set in the MCP server env")
    if not _allowed(to):
        raise ToolError(f"recipient {to!r} is not in GMAIL_ALLOWED_RECIPIENTS")

    msg = EmailMessage()
    msg["From"] = formataddr((from_name, address))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = format_datetime(datetime.now(timezone.utc))
    msg["Message-ID"] = make_msgid(domain=address.rpartition("@")[2] or None)
    if references:
        msg["References"] = references
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if original_recipient:
        msg["X-Original-To"] = original_recipient
    msg.set_content(body)

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(address, password)
            refused = smtp.send_message(msg)
    except smtplib.SMTPAuthenticationError as exc:
        raise ToolError(f"Gmail rejected the login (check the App Password): {exc.smtp_code} "
                        f"{exc.smtp_error.decode(errors='replace') if isinstance(exc.smtp_error, bytes) else exc.smtp_error}") from exc
    except smtplib.SMTPServerDisconnected as exc:
        # Seen in practice when GMAIL_ADDRESS is not the account that issued the App Password.
        raise ToolError(f"SMTP delivery failed: SMTPServerDisconnected: {exc} (check that "
                        "GMAIL_APP_PASSWORD was created for the GMAIL_ADDRESS account)") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise ToolError(f"SMTP delivery failed: {type(exc).__name__}: {exc}") from exc
    if refused:
        raise ToolError(f"recipients refused by server: {refused}")

    log.info("sent %s to %s", msg["Message-ID"], to)
    return {"status": "sent", "message_id": msg["Message-ID"], "to": to, "date": msg["Date"]}


if __name__ == "__main__":
    server.run("stdio")
