"""Scan unread Pure Fitness alerts and forward them."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import traceback
from pathlib import Path

from zimbra_client import Attachment, ZimbraClient

from alert_html import SkipForward, clean_subject, prepare_send_bodies, sanitize_signature_html


ROOT = Path(__file__).resolve().parent

DEFAULT_FORWARD_SIGNATURE_ID = "25ea6e17-aec8-4af4-8ab4-ac2795396549"
DEFAULT_FORWARD_SIGNATURE_NAME = (
    "SOC Team (Zimbra0020-0020CITIC0020Telecom0020SOC@example.com)"
)

ERROR_ALERT_SUBJECT = (
    "[ACTION REQUIRED] Alert forwarder failed — send customer notification manually"
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


def _zimbra_config_from_env() -> dict:
    return {
        "host": os.environ.get("SEND_EMAIL_HOST", "").strip(),
        "email": os.environ.get("SEND_EMAIL_USER", "").strip(),
        "password": os.environ.get("SEND_EMAIL_PASSWORD", "").strip(),
        "verify_ssl": True,
    }


def _run_mode_label(*, dry_run: bool, test: bool) -> str:
    if dry_run:
        return "dry-run"
    if test:
        return "test"
    return "normal"


def _format_recipient_list(addresses) -> str:
    if not addresses:
        return "(none)"
    return ", ".join(str(addr) for addr in addresses)


def _build_error_alert_bodies(payload: dict) -> tuple[str, str]:
    action_lines = [
        "ACTION REQUIRED",
        "===============",
        (
            "Automatic customer notification failed. The SOC team must manually send "
            "the customer notification now. Do not assume the customer was notified."
        ),
        "",
        f"Run mode: {payload.get('run_mode', 'unknown')}",
        f"Customer To: {_format_recipient_list(payload.get('customer_to'))}",
        f"Customer Cc: {_format_recipient_list(payload.get('customer_cc'))}",
        (
            f"Forwarded: {payload.get('forwarded', 0)} | "
            f"Skipped: {payload.get('skipped', 0)} | "
            f"Failed: {payload.get('failed', 0)}"
        ),
        "",
        (
            "Failed source messages were not marked read and remain unread in the "
            "source folder."
        ),
    ]

    failures = payload.get("failures") or []
    if failures:
        action_lines.extend(["", "Failed messages:"])
        for item in failures:
            action_lines.append(
                f"- id={item.get('id', '-')} "
                f"subject={item.get('subject', '-')!r} "
                f"folder={item.get('folder', '-')} "
                f"error={item.get('error', '-')}"
            )

    fatal_error = payload.get("fatal_error")
    if fatal_error:
        action_lines.extend(
            [
                "",
                "Fatal error:",
                f"Type: {fatal_error.get('type', 'unknown')}",
                f"Message: {fatal_error.get('message', '')}",
                "",
                "Traceback:",
                fatal_error.get("traceback", "").rstrip(),
            ]
        )

    text_body = "\n".join(action_lines)

    html_parts = [
        "<h2 style=\"color:#b91c1c;\">ACTION REQUIRED</h2>",
        (
            "<p><strong>Automatic customer notification failed.</strong> "
            "The SOC team must <strong>manually send the customer notification</strong> "
            "now. Do not assume the customer was notified.</p>"
        ),
        "<ul>",
        f"<li><strong>Run mode:</strong> {html.escape(str(payload.get('run_mode', 'unknown')))}</li>",
        (
            f"<li><strong>Customer To:</strong> "
            f"{html.escape(_format_recipient_list(payload.get('customer_to')))}</li>"
        ),
        (
            f"<li><strong>Customer Cc:</strong> "
            f"{html.escape(_format_recipient_list(payload.get('customer_cc')))}</li>"
        ),
        (
            f"<li><strong>Counts:</strong> forwarded={payload.get('forwarded', 0)}, "
            f"skipped={payload.get('skipped', 0)}, failed={payload.get('failed', 0)}</li>"
        ),
        "</ul>",
        (
            "<p>Failed source messages were <strong>not marked read</strong> and remain "
            "unread in the source folder.</p>"
        ),
    ]

    if failures:
        html_parts.append("<h3>Failed messages</h3><ul>")
        for item in failures:
            html_parts.append(
                "<li>"
                f"<strong>id:</strong> {html.escape(str(item.get('id', '-')))}<br>"
                f"<strong>subject:</strong> {html.escape(str(item.get('subject', '-')))}<br>"
                f"<strong>folder:</strong> {html.escape(str(item.get('folder', '-')))}<br>"
                f"<strong>error:</strong> {html.escape(str(item.get('error', '-')))}"
                "</li>"
            )
        html_parts.append("</ul>")

    if fatal_error:
        html_parts.extend(
            [
                "<h3>Fatal error</h3>",
                (
                    f"<p><strong>Type:</strong> "
                    f"{html.escape(str(fatal_error.get('type', 'unknown')))}<br>"
                    f"<strong>Message:</strong> "
                    f"{html.escape(str(fatal_error.get('message', '')))}</p>"
                ),
                (
                    "<pre style=\"white-space:pre-wrap;background:#f8fafc;"
                    "padding:12px;border:1px solid #e2e8f0;\">"
                    f"{html.escape(fatal_error.get('traceback', '').rstrip())}"
                    "</pre>"
                ),
            ]
        )

    html_body = "\n".join(html_parts)
    return text_body, html_body


def send_error_alert(client, error_to, payload: dict) -> None:
    text_body, html_body = _build_error_alert_bodies(payload)
    client.send_message(
        to=error_to,
        cc=None,
        subject=ERROR_ALERT_SUBJECT,
        text=text_body,
        html=html_body,
        attachments=None,
    )


def notify_soc_on_error(*, error_to, payload: dict, client=None) -> None:
    if not error_to:
        print("WARNING: error_to is empty; SOC alert email not sent")
        return

    try:
        if client is None:
            load_env()
            with ZimbraClient(_zimbra_config_from_env()) as alert_client:
                send_error_alert(alert_client, error_to, payload)
        else:
            send_error_alert(client, error_to, payload)
        print(f"SOC alert sent to error_to={error_to}")
    except Exception as exc:
        print(f"WARNING: failed to send SOC alert email: {exc}")


def _error_alert_context(
    app_cfg: dict,
    *,
    dry_run: bool,
    test: bool,
) -> tuple[list, list, list]:
    error_to = app_cfg.get("error_to") or []
    forward_cfg = app_cfg.get("forward") or {}
    test_forward_cfg = app_cfg.get("test_forward") or {}
    use_test_recipients = dry_run or test
    if use_test_recipients:
        customer_to = test_forward_cfg.get("to") or []
        customer_cc = test_forward_cfg.get("cc") or []
    else:
        customer_to = forward_cfg.get("to") or []
        customer_cc = forward_cfg.get("cc") or []
    return error_to, customer_to, customer_cc


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
    error_to, customer_to, customer_cc = _error_alert_context(
        app_cfg, dry_run=dry_run, test=test
    )

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

    zimbra_cfg = _zimbra_config_from_env()
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
        failure_details: list[dict] = []
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

            folder_label = folder_name or folder_id
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
                    failure_details.append(
                        {
                            "id": summary.id,
                            "subject": summary.subject,
                            "folder": folder_label,
                            "error": str(exc),
                        }
                    )
                    print(f"FAIL id={summary.id} subject={summary.subject!r} error={exc}")

            if dry_run and forwarded:
                break

        print(f"Done. forwarded={forwarded} skipped={skipped} failed={failed}")

        if failed > 0:
            notify_soc_on_error(
                error_to=error_to,
                client=client,
                payload={
                    "run_mode": _run_mode_label(dry_run=dry_run, test=test),
                    "customer_to": customer_to,
                    "customer_cc": customer_cc,
                    "forwarded": forwarded,
                    "skipped": skipped,
                    "failed": failed,
                    "failures": failure_details,
                    "fatal_error": None,
                },
            )
            raise SystemExit(1)


def _notify_fatal_error(exc: BaseException, *, dry_run: bool, test: bool) -> None:
    error_to: list = []
    customer_to: list = []
    customer_cc: list = []
    try:
        load_env()
        app_cfg = load_config()
        error_to, customer_to, customer_cc = _error_alert_context(
            app_cfg, dry_run=dry_run, test=test
        )
    except Exception as load_exc:
        print(f"WARNING: could not load config for SOC alert: {load_exc}")

    notify_soc_on_error(
        error_to=error_to,
        payload={
            "run_mode": _run_mode_label(dry_run=dry_run, test=test),
            "customer_to": customer_to,
            "customer_cc": customer_cc,
            "forwarded": 0,
            "skipped": 0,
            "failed": 0,
            "failures": [],
            "fatal_error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            },
        },
    )


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
    try:
        main(dry_run=args.dry_run, test=args.test)
    except SystemExit:
        raise
    except Exception as exc:
        _notify_fatal_error(exc, dry_run=args.dry_run, test=args.test)
        raise SystemExit(1) from exc
