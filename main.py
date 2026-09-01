"""Scan unread Pure Fitness alerts and forward them."""

from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path

from zimbra_client import Attachment, ZimbraClient

from alert_html import SkipForward, clean_subject, prepare_send_bodies, sanitize_signature_html


ROOT = Path(__file__).resolve().parent

DEFAULT_FORWARD_SIGNATURE_ID = "25ea6e17-aec8-4af4-8ab4-ac2795396549"
DEFAULT_FORWARD_SIGNATURE_NAME = (
    "SOC Team (Zimbra0020-0020CITIC0020Telecom0020SOC@example.com)"
)


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


def load_config(path: Path | None = None) -> dict:
    config_path = path or ROOT / "config.json"
    if not config_path.is_file():
        raise FileNotFoundError(f"config.json not found: {config_path}")
    with config_path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _forward_attachments(client, source):
    attachments = []
    for item in source.attachments:
        if not item.part:
            continue
        attachments.append(
            Attachment(
                filename=item.filename,
                content_type=item.content_type or "application/octet-stream",
                data=client.download_attachment(source.id, item.part),
            )
        )
    return attachments


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
    signature_html = sanitize_signature_html(signature.text_html)
    if not signature_html and signature_text:
        signature_html = html.escape(signature_text).replace("\n", "<br>\n")
    return signature_text, signature_html


def forward_message(
    client,
    message_id,
    subject,
    to,
    cc,
    signature_id,
    signature_name,
    signature_position="down",
):
    signature_text, signature_html = _signature_parts(client, signature_id, signature_name)
    source = client.get_message(message_id)
    body_text, body_html = prepare_send_bodies(
        body_text=source.body_text,
        body_html=source.body_html,
        signature_text=signature_text,
        signature_html=signature_html,
        signature_position=signature_position,
    )
    return client.send_message(
        to=to,
        cc=cc,
        subject=clean_subject(subject) or source.subject,
        text=body_text,
        html=body_html,
        attachments=_forward_attachments(client, source) or None,
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


def main(*, dry_run: bool = False, test: bool = False) -> None:
    if dry_run and test:
        raise ValueError("Use only one of --dry-run or --test")

    load_env()
    app_cfg = load_config()

    folders_cfg = app_cfg.get("folders")
    if not isinstance(folders_cfg, list) or not folders_cfg:
        raise ValueError("Missing or invalid folders in config.json (expected non-empty list)")

    forward_cfg = app_cfg.get("forward") or {}
    test_forward_cfg = app_cfg.get("test_forward") or {}
    limit = int(forward_cfg.get("limit") or 50)
    signature_id = str(forward_cfg.get("signature_id") or DEFAULT_FORWARD_SIGNATURE_ID).strip()
    signature_name = str(forward_cfg.get("signature_name") or DEFAULT_FORWARD_SIGNATURE_NAME).strip()
    signature_position = str(forward_cfg.get("signature_position") or "down").strip().lower()
    if signature_position not in {"up", "down"}:
        raise ValueError('forward.signature_position must be "up" or "down"')

    use_test_recipients = dry_run or test
    if use_test_recipients:
        to_addrs = test_forward_cfg.get("to") or []
        cc_addrs = test_forward_cfg.get("cc") or []
        if not to_addrs:
            raise ValueError("Missing test_forward.to recipients in config.json")
    else:
        to_addrs = forward_cfg.get("to") or []
        cc_addrs = forward_cfg.get("cc") or []
        if not to_addrs:
            raise ValueError("Missing forward.to recipients in config.json")

    if dry_run:
        search_query = ""
        search_limit = 1
    else:
        search_query = "is:unread"
        search_limit = limit

    zimbra_cfg = {
        "host": os.environ.get("SEND_EMAIL_HOST", "").strip(),
        "email": os.environ.get("SEND_EMAIL_USER", "").strip(),
        "password": os.environ.get("SEND_EMAIL_PASSWORD", "").strip(),
        "verify_ssl": True,
    }
    with ZimbraClient(zimbra_cfg) as client:
        print(f"Zimbra login OK for {client.config.email}")
        if dry_run:
            print(
                "DRY-RUN mode: ignore unread filter, send one message to "
                "test_forward recipients, and do not mark it read"
            )
        elif test:
            print(
                "TEST mode: unread only, send to test_forward recipients, "
                "and mark messages read"
            )
        print(f"Forward to={to_addrs} cc={cc_addrs}")
        print(
            f"Signature id={signature_id or '-'} name={signature_name or '-'} "
            f"position={signature_position}"
        )

        forwarded = 0
        failed = 0
        skipped = 0
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

            scan_label = "any" if dry_run else "unread"
            print(
                f"Scanning {scan_label} in folder name={folder_name or '-'} "
                f"parent_id={parent_id or '-'} id={folder_id} (limit={search_limit})"
            )
            results = client.search_messages(
                query=search_query, folder_id=folder_id, limit=search_limit
            )
            if not results.messages:
                print(f"No {scan_label} messages found.")
                continue

            print(f"Found {len(results.messages)} {scan_label} message(s)")
            for summary in results.messages:
                try:
                    forward_message(
                        client,
                        summary.id,
                        summary.subject,
                        to=to_addrs,
                        cc=cc_addrs,
                        signature_id=signature_id,
                        signature_name=signature_name,
                        signature_position=signature_position,
                    )
                    if dry_run:
                        forwarded += 1
                        print(
                            f"DRY-RUN OK  id={summary.id} subject={summary.subject!r} "
                            f"to={to_addrs} cc={cc_addrs}"
                        )
                        break

                    client.mark_read(summary.id)
                    forwarded += 1
                    prefix = "TEST OK" if test else "OK"
                    print(f"{prefix}  id={summary.id} subject={summary.subject!r}")
                except SkipForward as exc:
                    skipped += 1
                    print(f"SKIP id={summary.id} subject={summary.subject!r} reason={exc}")
                except Exception as exc:
                    failed += 1
                    print(f"FAIL id={summary.id} subject={summary.subject!r} error={exc}")

            if dry_run and forwarded:
                break

        print(f"Done. forwarded={forwarded} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan unread alerts and forward them.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Send one message (read or unread) to test_forward recipients without marking it read",
    )
    mode.add_argument(
        "--test",
        action="store_true",
        help="Send unread messages to test_forward recipients and mark them read",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run, test=args.test)
