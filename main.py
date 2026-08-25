"""Scan unread Pure Fitness alerts and forward them."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from plugin.zimbra import (
    require_zimbra_config,
    zimbra_email,
    zimbra_forward_as_is,
    zimbra_get_message,
    zimbra_host,
    zimbra_login,
    zimbra_mark_read,
    zimbra_search,
)

ROOT = Path(__file__).resolve().parent


def load_env(path: Path | None = None) -> None:
    env_path = path or ROOT / ".env"
    if not env_path.is_file():
        raise FileNotFoundError(f".env not found: {env_path}")

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        os.environ.setdefault(key, value)


def normalize_host(raw_host: str) -> str:
    host = (raw_host or "").strip()
    if not host:
        return ""
    if "://" not in host:
        host = f"https://{host}"
    parsed = urlparse(host)
    return (parsed.hostname or "").strip()


def build_zimbra_cfg() -> dict:
    return {
        "zimbra_host": normalize_host(os.environ.get("SEND_EMAIL_HOST", "")),
        "zimbra_email": os.environ.get("SEND_EMAIL_USER", "").strip(),
        "zimbra_password": os.environ.get("SEND_EMAIL_PASSWORD", "").strip(),
    }


def load_config(path: Path | None = None) -> dict:
    config_path = path or ROOT / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"config.json not found: {config_path}")
    with config_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    load_env()
    cfg = build_zimbra_cfg()
    require_zimbra_config(cfg)
    app_cfg = load_config()

    folder_id = str((app_cfg.get("folders") or {}).get("alert_call_folder_id") or "").strip()
    if not folder_id:
        raise ValueError("Missing folders.alert_call_folder_id in config.json")

    forward_cfg = app_cfg.get("forward") or {}
    to_addrs = forward_cfg.get("to") or []
    cc_addrs = forward_cfg.get("cc") or []
    limit = int(forward_cfg.get("limit") or 50)
    signature_id = str(forward_cfg.get("signature_id") or "").strip()
    signature_name = str(forward_cfg.get("signature_name") or "").strip()
    signature_position = str(forward_cfg.get("signature_position") or "down").strip().lower()
    if signature_position not in {"up", "down"}:
        raise ValueError('forward.signature_position must be "up" or "down"')
    if not to_addrs:
        raise ValueError("Missing forward.to recipients in config.json")

    host = zimbra_host(cfg)
    token = zimbra_login(cfg)
    account = zimbra_email(cfg)
    print(f"Zimbra login OK for {account}")
    print(f"Scanning unread in folder id={folder_id} (limit={limit})")
    if signature_id or signature_name:
        print(
            f"Signature id={signature_id or '-'} name={signature_name or '-'} "
            f"position={signature_position}"
        )

    message_ids = zimbra_search(host, token, folder_id, limit, unread_only=True)
    if not message_ids:
        print("No unread messages found.")
        return

    print(f"Found {len(message_ids)} unread message(s)")
    ok = 0
    failed = 0

    for message_id in message_ids:
        subject = ""
        try:
            message = zimbra_get_message(host, token, message_id)
            subject = (message or {}).get("subject") or ""
            zimbra_forward_as_is(
                cfg,
                message_id,
                to=to_addrs,
                cc=cc_addrs,
                token=token,
                signature_id=signature_id,
                signature_name=signature_name,
                signature_position=signature_position,
            )
            zimbra_mark_read(host, token, message_id)
            ok += 1
            print(f"OK  id={message_id} subject={subject!r}")
        except Exception as exc:
            failed += 1
            print(f"FAIL id={message_id} subject={subject!r} error={exc}")

    print(f"Done. forwarded={ok} failed={failed}")


if __name__ == "__main__":
    main()
