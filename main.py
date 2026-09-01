"""Scan unread Pure Fitness alerts and forward them."""

from __future__ import annotations

import html
import json
import os
import re
import argparse
from pathlib import Path

from zimbra_client import Attachment, ZimbraClient


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


_CLOUDFALL_TEAM_RE = re.compile(r"云纷科技\s*7\s*\*\s*24\s*Team")
_CLOUDFALL_WECHAT_RE = re.compile(r"微信公众号\s*:\s*cloudfallcn", re.IGNORECASE)
_CLOUDFALL_CLOSING_RE = re.compile(
    r"If you have any further questions or need more information",
    re.IGNORECASE,
)
_CLOUDFALL_SOC_TEAM_RE = re.compile(r"Cloudfall SOC Team")
_SIGNATURE_DASH_RE = re.compile(r"-{10,}")
_HTML_TAG_RE = re.compile(r"(?is)<\s*(/?)\s*([a-z][a-z0-9]*)([^>]*)>")
_VOID_HTML_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "source",
    "wbr",
}


class SkipForward(Exception):
    """Message should not be forwarded."""


def _visible_text(content: str) -> str:
    text = re.sub(r"(?is)<br\s*/?>", "\n", str(content or ""))
    text = re.sub(r"(?is)</(?:p|div|tr|h[1-6]|li)>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    return html.unescape(text).replace("\xa0", " ").strip()


def _close_open_html_tags(fragment: str) -> str:
    stack = []
    for match in _HTML_TAG_RE.finditer(fragment):
        closing, name, rest = match.group(1), match.group(2).lower(), match.group(3)
        if name in _VOID_HTML_TAGS or rest.rstrip().endswith("/"):
            continue
        if closing:
            if name in stack:
                while stack and stack[-1] != name:
                    stack.pop()
                if stack:
                    stack.pop()
            continue
        stack.append(name)
    return fragment + "".join(f"</{name}>" for name in reversed(stack))


def _align_cut_to_block(text: str, pos: int) -> int:
    prefix = text[:pos]
    dash = None
    for match in _SIGNATURE_DASH_RE.finditer(prefix):
        if pos - match.start() < 2000:
            dash = match
    if dash is not None:
        pos = dash.start()
    for _ in range(3):
        div_at = text.rfind("<div", 0, pos)
        if div_at == -1 or pos - div_at >= 2000:
            break
        pos = div_at
    return pos


def _quoted_original_cut(content: str) -> int | None:
    text = str(content or "")
    starts = []
    for pattern in (
        _CLOUDFALL_CLOSING_RE,
        _CLOUDFALL_SOC_TEAM_RE,
        _CLOUDFALL_TEAM_RE,
        _CLOUDFALL_WECHAT_RE,
    ):
        match = pattern.search(text)
        if match is not None:
            starts.append(match.start())
    if not starts:
        return None
    return _align_cut_to_block(text, min(starts))


def _strip_quoted_original(content: str) -> str | None:
    """Drop Cloudfall closing, signature, and the quoted original message."""
    text = str(content or "")
    cut = _quoted_original_cut(text)
    if cut is None:
        return None

    fragment = text[:cut].rstrip()
    last_lt = fragment.rfind("<")
    last_gt = fragment.rfind(">")
    if last_lt > last_gt:
        fragment = fragment[:last_lt].rstrip()

    if "<" in fragment:
        fragment = _close_open_html_tags(fragment)

    if not _visible_text(fragment):
        return None
    return fragment


def _html_to_plain(html_or_text: str) -> str:
    text = str(html_or_text or "")
    if not text.strip():
        return ""
    text = re.sub(r"(?is)<(br|/div|/p|/tr|/li|hr)\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _drop_caution(plain: str) -> str:
    return re.sub(
        r"(?is)^CAUTION:\s*This message was sent from outside of Company Domain\."
        r".*?(?:attachment\)\.?)\s*",
        "",
        str(plain or "").strip(),
        count=1,
    ).strip()


def _split_alert_sections(plain_text: str) -> list[tuple[str, str]]:
    text = str(plain_text or "").strip()
    if not text:
        return []
    header_re = re.compile(r"(?m)^(?:---\s*(.+?)\s*---|(Root Cause Analysis[^\n]*))\s*$")
    matches = list(header_re.finditer(text))
    if not matches:
        return [("Summary", text)]
    sections = []
    first = matches[0]
    summary = text[: first.start()].strip()
    if summary:
        sections.append(("Summary", summary))
    for idx, match in enumerate(matches):
        title = (match.group(1) or match.group(2) or "").strip()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        sections.append((title, body))
    return sections


def _severity_badge(value: str) -> str:
    raw = str(value or "").strip()
    low = raw.lower()
    if low == "critical":
        bg, fg = "#fee2e2", "#991b1b"
    elif low == "high":
        bg, fg = "#ffedd5", "#9a3412"
    elif low == "medium":
        bg, fg = "#fef3c7", "#92400e"
    elif low == "low":
        bg, fg = "#dcfce7", "#166534"
    else:
        bg, fg = "#e2e8f0", "#334155"
    return (
        f'<span style="display:inline-block;padding:2px 8px;border-radius:999px;'
        f"background:{bg};color:{fg};font-size:12px;font-weight:700;"
        f'letter-spacing:0.02em;">{html.escape(raw)}</span>'
    )


def _linkify(text: str) -> str:
    return re.sub(
        r"(https?://[^\s<]+)",
        (
            r'<a href="\1" style="color:#0f766e;word-break:break-all;'
            r'overflow-wrap:anywhere;word-wrap:break-word;">\1</a>'
        ),
        text,
    )


def _is_field_value_header(line: str) -> bool:
    return bool(re.match(r"(?i)^field\s*\|\s*value$", str(line or "").strip()))


def _is_key_value_section(title: str, body: str = "") -> bool:
    low = (title or "").strip().lower()
    if low in {"alert details", "activity details", "links"} or "event info" in low:
        return True
    return "field | value" in str(body or "").lower()


def _parse_kv_line(line: str) -> tuple[str, str] | None:
    raw = str(line or "").strip()
    if not raw or _is_field_value_header(raw):
        return None
    if " | " in raw:
        label, value = raw.split(" | ", 1)
        return label.strip(), value.strip()
    if ":" in raw and not raw.lower().startswith("http"):
        label, value = raw.split(":", 1)
        if label.strip():
            return label.strip(), value.strip()
    return None


def _kv_row_html(label: str, value_html: str) -> str:
    return (
        '<tr>'
        '<td style="width:140px;min-width:100px;max-width:160px;padding:8px 12px 8px 0;'
        'vertical-align:top;font-weight:700;color:#334155;border-bottom:1px solid #f1f5f9;">'
        f"{html.escape(label)}</td>"
        '<td style="padding:8px 0;vertical-align:top;color:#111827;border-bottom:1px solid #f1f5f9;'
        'word-break:break-all;overflow-wrap:anywhere;word-wrap:break-word;">'
        f"{value_html}</td>"
        "</tr>"
    )


def _render_key_value_rows(body: str) -> str:
    rows = []
    pending_label = None
    for raw_line in str(body or "").splitlines():
        line = raw_line.strip()
        if not line or _is_field_value_header(line):
            continue
        parsed = _parse_kv_line(line)
        if parsed is not None:
            label, value = parsed
            if not value:
                pending_label = label
                continue
            pending_label = None
            value_html = _severity_badge(value) if label.lower() == "severity" else _linkify(html.escape(value))
            rows.append(_kv_row_html(label, value_html))
            continue
        if pending_label:
            rows.append(_kv_row_html(pending_label, _linkify(html.escape(line))))
            pending_label = None
            continue
        rows.append(
            '<tr><td colspan="2" style="padding:8px 0;color:#111827;border-bottom:1px solid #f1f5f9;'
            f'word-break:break-all;overflow-wrap:anywhere;">{_linkify(html.escape(line))}</td></tr>'
        )
    if pending_label:
        rows.append(_kv_row_html(pending_label, ""))
    if not rows:
        return '<div style="color:#64748b;">No details</div>'
    rows[-1] = rows[-1].replace("border-bottom:1px solid #f1f5f9;", "border-bottom:none;")
    return (
        '<table width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="width:100%;max-width:100%;table-layout:fixed;border-collapse:collapse;">\n'
        f"{chr(10).join(rows)}\n"
        "</table>"
    )


def _render_narrative_body(body: str) -> str:
    chunks = [c.strip() for c in re.split(r"\n\s*\n", str(body or "")) if c.strip()]
    if not chunks:
        chunks = [line.strip() for line in str(body or "").splitlines() if line.strip()]
    parts = []
    for chunk in chunks:
        escaped = _linkify(html.escape(chunk).replace("\n", "<br>\n"))
        if re.match(r"^[a-z]\.\s", chunk, re.IGNORECASE):
            parts.append(
                f'<p style="margin:0 0 10px 0;padding-left:8px;color:#111827;'
                f'word-break:break-all;overflow-wrap:anywhere;">{escaped}</p>'
            )
        else:
            parts.append(
                f'<p style="margin:0 0 12px 0;word-break:break-all;overflow-wrap:anywhere;">{escaped}</p>'
            )
    if parts:
        parts[-1] = parts[-1].replace("margin:0 0 12px 0;", "margin:0;")
        parts[-1] = parts[-1].replace("margin:0 0 10px 0;", "margin:0;")
    return "\n".join(parts)


def _extract_greeting(summary_body: str) -> tuple[str, str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(summary_body or "")) if p.strip()]
    if paragraphs and re.match(r"(?i)^(Hi\s+all|Dear\b.+)", paragraphs[0]):
        return paragraphs[0], "\n\n".join(paragraphs[1:]).strip()
    return "", str(summary_body or "").strip()


def _render_summary_section(body: str) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(body or "")) if p.strip()]
    parts = []
    for para in paragraphs:
        escaped = _linkify(html.escape(para).replace("\n", "<br>\n"))
        parts.append(f'<p style="margin:0 0 12px 0;color:#334155;">{escaped}</p>')
    if parts:
        parts[-1] = parts[-1].replace("margin:0 0 12px 0;", "margin:0;")
    return "\n".join(parts)


def _render_section_card(title: str, body: str) -> str:
    title_text = (title or "Details").strip()
    if title_text.lower() == "summary":
        content = _render_summary_section(body)
    elif _is_key_value_section(title_text, body):
        content = _render_key_value_rows(body)
    else:
        content = _render_narrative_body(body)
    if not content.strip():
        return ""
    return (
        '<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;'
        'padding:16px;margin:0 0 14px 0;overflow:hidden;max-width:100%;">\n'
        '<div style="border-left:4px solid #0f766e;padding-left:10px;margin-bottom:12px;">\n'
        f'<div style="font-size:13px;font-weight:700;letter-spacing:0.04em;'
        f'text-transform:uppercase;color:#0f172a;">{html.escape(title_text)}</div>\n'
        "</div>\n"
        f"{content}\n"
        "</div>"
    )


def _format_forward_html(plain_text: str, signature_html: str = "", signature_position: str = "down") -> str:
    plain = str(plain_text or "").strip()
    if not plain:
        return ""
    plain = re.sub(r"(?is)\n*\s*Best\s+regards\s*,?\s*$", "", plain).strip()
    sections = _split_alert_sections(plain)
    if not sections:
        return ""

    greeting_html = ""
    rendered_sections = []
    for title, body in sections:
        if not body.strip():
            continue
        if title.strip().lower() == "summary":
            greeting, summary_body = _extract_greeting(body)
            if greeting:
                greeting_html = (
                    f'<p style="margin:0 0 16px 0;font-size:16px;font-weight:700;'
                    f'color:#0f172a;">{html.escape(greeting)}</p>\n'
                )
            if summary_body.strip():
                rendered_sections.append(_render_section_card(title, summary_body))
            continue
        rendered_sections.append(_render_section_card(title, body))

    cards = "\n".join(part for part in rendered_sections if part)
    content = f"{greeting_html}{cards}"
    sig = (signature_html or "").strip()
    position = str(signature_position or "down").strip().lower()
    if position not in {"up", "down"}:
        position = "down"
    if sig:
        signature = (
            f'<div style="margin:{"0 0 16px 0" if position == "up" else "16px 0 0 0"};">{sig}</div>'
        )
    else:
        signature = (
            '<div style="margin-top:8px;color:#475569;font-size:13px;line-height:1.5;">'
            "Best regards,<br>\n"
            '<strong style="color:#0f172a;">CITICTEL-CPC SOC TEAM</strong>'
            "</div>"
        )
    inner = f"{signature}\n{content}\n" if position == "up" else f"{content}\n{signature}\n"
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        "color:#1f2937;line-height:1.5;max-width:720px;background:#f8fafc;"
        'padding:16px;border-radius:10px;overflow:hidden;word-break:break-word;">\n'
        f"{inner}"
        "</div>"
    )


def _join_body_and_signature(body: str, signature: str, *, html: bool, position: str) -> str:
    body = str(body or "").strip()
    signature = str(signature or "").strip()
    if not signature:
        return body
    if not body:
        return signature
    sep = "<br><br>" if html else "\n\n"
    if position == "up":
        return f"{signature}{sep}{body}"
    return f"{body}{sep}{signature}"


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
    signature_html = _sanitize_signature_html(signature.text_html)
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
    cleaned_text = _strip_quoted_original(source.body_text)
    cleaned_html = _strip_quoted_original(source.body_html)
    if cleaned_text is None and cleaned_html is None:
        raise SkipForward("Cloudfall closing/signature not found")
    plain = _drop_caution(_html_to_plain(cleaned_html or cleaned_text or ""))
    sent_html = _format_forward_html(
        plain,
        signature_html=signature_html,
        signature_position=signature_position,
    )
    if not sent_html.strip():
        raise SkipForward("Styled body is empty")
    body_text = _join_body_and_signature(
        plain,
        signature_text,
        html=False,
        position=signature_position,
    )
    return client.send_message(
        to=to,
        cc=cc,
        subject=_clean_subject(subject) or source.subject,
        text=body_text,
        html=sent_html,
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


def main(*, dry_run: bool = False) -> None:
    load_env()
    app_cfg = load_config()
    zimbra_cfg = build_zimbra_cfg()

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

    if dry_run:
        to_addrs = test_forward_cfg.get("to") or []
        cc_addrs = test_forward_cfg.get("cc") or []
        search_query = ""
        search_limit = 1
        if not to_addrs:
            raise ValueError("Missing test_forward.to recipients in config.json")
    else:
        to_addrs = forward_cfg.get("to") or []
        cc_addrs = forward_cfg.get("cc") or []
        search_query = "is:unread"
        search_limit = limit
        if not to_addrs:
            raise ValueError("Missing forward.to recipients in config.json")

    with ZimbraClient(zimbra_cfg) as client:
        print(f"Zimbra login OK for {client.config.email}")
        if dry_run:
            print(
                "DRY-RUN mode: ignore unread filter, forward one message to "
                "test_forward recipients, and do not mark it read"
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
                    print(f"OK  id={summary.id} subject={summary.subject!r}")
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
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Forward one message (read or unread) to test_forward recipients without marking it read",
    )
    args = parser.parse_args()
    main(dry_run=args.dry_run)