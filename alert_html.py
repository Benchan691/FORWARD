"""Strip Cloudfall quote noise and restyle alert bodies as HTML cards."""

from __future__ import annotations

import html
import re


def clean_subject(subject):
    cleaned = str(subject or "").strip()
    prefix_re = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
    while True:
        updated = prefix_re.sub("", cleaned, count=1).strip()
        if updated == cleaned:
            return cleaned
        cleaned = updated


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


def sanitize_signature_html(signature_html):
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


def strip_quoted_original(content: str) -> str | None:
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


def html_to_plain(html_or_text: str) -> str:
    text = str(html_or_text or "")
    if not text.strip():
        return ""
    text = re.sub(r"(?is)<(br|/div|/p|/tr|/li|hr)\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def drop_caution(plain: str) -> str:
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


def format_forward_html(plain_text: str, signature_html: str = "", signature_position: str = "down") -> str:
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


def join_body_and_signature(body: str, signature: str, *, html: bool, position: str) -> str:
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


def prepare_send_bodies(
    *,
    body_text: str,
    body_html: str,
    signature_text: str = "",
    signature_html: str = "",
    signature_position: str = "down",
) -> tuple[str, str]:
    """Return (plain, styled_html) for send_message, or raise SkipForward."""
    cleaned_text = strip_quoted_original(body_text)
    cleaned_html = strip_quoted_original(body_html)
    if cleaned_text is None and cleaned_html is None:
        raise SkipForward("Cloudfall closing/signature not found")
    plain = drop_caution(html_to_plain(cleaned_html or cleaned_text or ""))
    sent_html = format_forward_html(
        plain,
        signature_html=signature_html,
        signature_position=signature_position,
    )
    if not sent_html.strip():
        raise SkipForward("Styled body is empty")
    sent_text = join_body_and_signature(
        plain,
        signature_text,
        html=False,
        position=signature_position,
    )
    return sent_text, sent_html
