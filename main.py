"""Scan unread Pure Fitness alerts and forward them."""

from __future__ import annotations

import html
import json
import os
import re
import argparse
from pathlib import Path

from zimbra_client import ZimbraClient


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
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


def build_zimbra_cfg() -> dict:
    return {
        "host": os.environ.get("SEND_EMAIL_HOST", "").strip(),
        "email": os.environ.get("SEND_EMAIL_USER", "").strip(),
        "password": os.environ.get("SEND_EMAIL_PASSWORD", "").strip(),
        "verify_ssl": True,
    }


def load_config(path: Path | None = None) -> dict:
    config_path = path or ROOT / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"config.json not found: {config_path}")
    with config_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _clean_subject(subject):
    cleaned = str(subject or "").strip()
    prefix_re = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
    while True:
        updated = prefix_re.sub("", cleaned, count=1).strip()
        if updated == cleaned:
            return cleaned
        cleaned = updated


DEFAULT_FORWARD_SIGNATURE_ID = "25ea6e17-aec8-4af4-8ab4-ac2795396549"
DEFAULT_FORWARD_SIGNATURE_NAME = (
    "SOC Team (Zimbra0020-0020CITIC0020Telecom0020SOC@example.com)"
)


def _social_link_label(href):
    low = str(href or "").lower()
    for marker, label in (
        ("facebook.com", "Facebook"),
        ("linkedin.com", "LinkedIn"),
        ("twitter.com", "X"),
        ("x.com", "X"),
        ("youtube.com", "YouTube"),
        ("instagram.com", "Instagram"),
        ("citictel-cpc.com", "Website"),
    ):
        if marker in low:
            return label
    return "Link"


def _sanitize_signature_html(signature_html):
    text = str(signature_html or "")
    if not text.strip() or not re.search(r"connect\s+with\s+us\s*:", text, re.IGNORECASE):
        return text

    text = re.sub(r"(?is)<img\b[^>]*/?>", "", text)
    parts = []
    position = 0
    for match in re.finditer(
        r'(?is)(<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>)(.*?)(</a>)',
        text,
    ):
        parts.append(text[position : match.start()])
        open_tag, href, inner, close_tag = match.groups()
        visible = re.sub(r"(?is)<[^>]+>", "", inner)
        visible = html.unescape(visible).replace("\xa0", " ").strip()
        if visible:
            parts.append(match.group(0))
        else:
            label = html.escape(_social_link_label(html.unescape(href)))
            parts.append(f"{open_tag}{label}{close_tag}")
        position = match.end()
    parts.append(text[position:])
    return "".join(parts)


def _signature_parts(client, signature_id, signature_name):
    signatures = client.list_signatures() if signature_id or signature_name else ()

    def find(predicate):
        return next((item for item in signatures if predicate(item)), None)

    signature = find(lambda item: signature_id and item.id == signature_id)
    if signature is None:
        signature = find(lambda item: signature_name and item.name == signature_name)
    if signature is None:
        return "", ""

    signature_text = signature.text_plain
    signature_html = _sanitize_signature_html(signature.text_html)
    if not signature_html and signature_text:
        signature_html = html.escape(signature_text).replace("\n", "<br>\n")
    return signature_text, signature_html


def forward_message(client, message_id, subject, to, cc, signature_id, signature_name):
    signature_text, signature_html = _signature_parts(client, signature_id, signature_name)
    return client.forward_message(
        message_id,
        to=to,
        cc=cc,
        subject=_clean_subject(subject) or None,
        text=signature_text,
        html=signature_html,
    )


def resolve_folder_id(client, parent_id: str, folder_name: str) -> str:
    parent_id = str(parent_id or "").strip()
    folder_name = str(folder_name or "").strip()
    if not parent_id:
        raise ValueError("Missing pure_fitness_parent_id")
    if not folder_name:
        raise ValueError("Missing folder_name")

    matches = [
        folder
        for folder in client.list_folders(folder_id=parent_id)
        if folder.name == folder_name and folder.parent_id == parent_id
    ]
    if not matches:
        raise ValueError(
            f'Folder {folder_name!r} not found under parent id={parent_id}'
        )
    if len(matches) > 1:
        raise ValueError(
            f'Multiple folders named {folder_name!r} under parent id={parent_id}'
        )
    return matches[0].id


def main(*, dry_run: bool = False) -> None:
    load_env()
    app_cfg = load_config()
    zimbra_cfg = build_zimbra_cfg()

    folders_cfg = app_cfg.get("folders")
    if not isinstance(folders_cfg, list) or not folders_cfg:
        raise ValueError("Missing or invalid folders in config.json (expected non-empty list)")

    forward_cfg = app_cfg.get("forward") or {}
    to_addrs = forward_cfg.get("to") or []
    cc_addrs = forward_cfg.get("cc") or []
    limit = int(forward_cfg.get("limit") or 50)
    signature_id = str(forward_cfg.get("signature_id") or DEFAULT_FORWARD_SIGNATURE_ID).strip()
    signature_name = str(forward_cfg.get("signature_name") or DEFAULT_FORWARD_SIGNATURE_NAME).strip()
    signature_position = str(forward_cfg.get("signature_position") or "down").strip().lower()
    if signature_position not in {"up", "down"}:
        raise ValueError('forward.signature_position must be "up" or "down"')
    if not to_addrs:
        raise ValueError("Missing forward.to recipients in config.json")

    with ZimbraClient(zimbra_cfg) as client:
        print(f"Zimbra login OK for {client.config.email}")
        if dry_run:
            print("DRY-RUN mode: messages will not be forwarded or marked read")
        print(
            f"Signature id={signature_id or '-'} name={signature_name or '-'} "
            f"position={signature_position}"
        )

        forwarded = 0
        failed = 0
        for index, folder_cfg in enumerate(folders_cfg, start=1):
            if not isinstance(folder_cfg, dict):
                raise ValueError(f"folders[{index - 1}] must be an object")

            parent_id = str(folder_cfg.get("pure_fitness_parent_id") or "").strip()
            folder_name = str(folder_cfg.get("folder_name") or "").strip()
            folder_id = str(
                folder_cfg.get("folder_id") or folder_cfg.get("alert_call_folder_id") or ""
            ).strip()
            if not folder_id and folder_name:
                folder_id = resolve_folder_id(client, parent_id, folder_name)
            elif not folder_id:
                raise ValueError(
                    f"Missing folders[{index - 1}].folder_id or "
                    f"folders[{index - 1}].folder_name in config.json"
                )

            print(
                f"Scanning unread in folder name={folder_name or '-'} "
                f"parent_id={parent_id or '-'} id={folder_id} (limit={limit})"
            )
            results = client.search_messages(query="is:unread", folder_id=folder_id, limit=limit)
            if not results.messages:
                print("No unread messages found.")
                continue

            print(f"Found {len(results.messages)} unread message(s)")
            for summary in results.messages:
                try:
                    if dry_run:
                        print(
                            f"DRY-RUN id={summary.id} subject={summary.subject!r} "
                            f"to={to_addrs} cc={cc_addrs}"
                        )
                        forwarded += 1
                        continue

                    forward_message(
                        client,
                        summary.id,
                        summary.subject,
                        to=to_addrs,
                        cc=cc_addrs,
                        signature_id=signature_id,
                        signature_name=signature_name,
                    )
                    client.mark_read(summary.id)
                    forwarded += 1
                    print(f"OK  id={summary.id} subject={summary.subject!r}")
                except Exception as exc:
                    failed += 1
                    print(f"FAIL id={summary.id} subject={summary.subject!r} error={exc}")

        if dry_run:
            print(f"Done. would_forward={forwarded} failed={failed}")
        else:
            print(f"Done. forwarded={forwarded} failed={failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan unread alerts and forward them.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List unread messages without forwarding or marking them read",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)
