#!/usr/bin/env python3
"""Send a plaintext email using Redmail.

The message is read as JSON from stdin:
  {"from": "...", "to": "...", "subject": "...", "text": "..."}

Provider configuration is detected from FROM_EMAIL:
  - gmail.com/googlemail.com: use Gmail SMTP
  - anything else: use Resend SMTP

Resend and Gmail are both sent through SMTP here:
  - Resend: smtp.resend.com:587, username "resend", password EMAIL_API_KEY
  - Gmail:  smtp.gmail.com:587, username FROM_EMAIL, password EMAIL_API_KEY
"""

import json
import os
import sys

from redmail import EmailSender


DEFAULT_TIMEOUT_SECONDS = 30


class ConfigError(ValueError):
    pass


def getenv(name, default=None):
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def read_message():
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"stdin must contain a JSON email payload: {exc}") from exc

    required = ["from", "to", "subject", "text"]
    missing = [field for field in required if not payload.get(field)]
    if missing:
        raise ConfigError(f"email payload is missing required field(s): {', '.join(missing)}")

    return payload


def sender_domain(from_email):
    address = from_email.strip().lower()
    if "<" in address and ">" in address:
        address = address.split("<", 1)[1].split(">", 1)[0].strip()
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1]


def infer_provider(from_email):
    domain = sender_domain(from_email)
    if domain in {"gmail.com", "googlemail.com"}:
        return "gmail"
    return "resend"


def provider_config():
    from_email = getenv("FROM_EMAIL")
    if not from_email:
        raise ConfigError("FROM_EMAIL is required")

    provider = infer_provider(from_email)
    password = getenv("EMAIL_API_KEY")
    if not password:
        raise ConfigError("EMAIL_API_KEY is required")

    if provider == "resend":
        host = "smtp.resend.com"
        port = 587
        username = "resend"
    elif provider == "gmail":
        host = "smtp.gmail.com"
        port = 587
        username = from_email
    else:
        raise ConfigError(f"unsupported inferred email provider: {provider}")

    return {
        "provider": provider,
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "timeout": int(getenv("EMAIL_TIMEOUT", str(DEFAULT_TIMEOUT_SECONDS))),
    }


def build_sender(config):
    kwargs = {
        "host": config["host"],
        "port": config["port"],
        "username": config["username"],
        "password": config["password"],
        "timeout": config["timeout"],
    }

    kwargs["use_starttls"] = True
    return EmailSender(**kwargs)


def send_email(payload, config):
    sender = build_sender(config)
    sender.send(
        subject=payload["subject"],
        sender=payload["from"],
        receivers=[payload["to"]],
        text=payload["text"],
    )


def main():
    try:
        payload = read_message()
        config = provider_config()
        send_email(payload, config)
    except Exception as exc:
        print(f"Failed to send email with Redmail: {exc}", file=sys.stderr)
        return 1

    print(f"Email accepted by {config['provider']} via {config['host']}:{config['port']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
