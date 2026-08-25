import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


def zimbra_host(cfg):
    return str(cfg.get("zimbra_host") or cfg.get("host") or "").strip()


def zimbra_email(cfg):
    return str(cfg.get("zimbra_email") or cfg.get("email") or "").strip()


def zimbra_password(cfg):
    return str(cfg.get("zimbra_password") or cfg.get("password") or "").strip()


def require_zimbra_config(cfg):
    missing = []
    if not zimbra_host(cfg):
        missing.append("ZIMBRA_HOST")
    if not zimbra_email(cfg):
        missing.append("ZIMBRA_EMAIL")
    if not zimbra_password(cfg):
        missing.append("ZIMBRA_PASSWORD")
    if missing:
        raise ValueError("Missing transfer config: " + ", ".join(missing))


def _local_name(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def soap_request(host, body_xml, auth_token=""):
    header = f"<authToken>{html.escape(auth_token)}</authToken>" if auth_token else ""
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">
  <soap:Header><context xmlns="urn:zimbra">{header}</context></soap:Header>
  <soap:Body>{body_xml}</soap:Body>
</soap:Envelope>
"""
    request = urllib.request.Request(
        f"https://{host}/service/soap",
        data=envelope.encode("utf-8"),
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return ET.fromstring(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"Zimbra SOAP request failed ({exc.code}): {detail or exc.reason}") from exc


def zimbra_login(cfg):
    host = zimbra_host(cfg)
    account = html.escape(zimbra_email(cfg))
    password = html.escape(zimbra_password(cfg))
    root = soap_request(
        host,
        f"""<AuthRequest xmlns="urn:zimbraAccount">
  <account by="name">{account}</account>
  <password>{password}</password>
</AuthRequest>""",
    )
    token = next((elem.text for elem in root.iter() if _local_name(elem.tag) == "authToken"), "")
    if not token:
        raise RuntimeError("Zimbra login failed: auth token not found")
    return token


def upload_attachment(host, token, filename, data, content_type="application/octet-stream"):
    boundary = "----codex-zimbra-upload"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        f"https://{host}/service/upload?fmt=raw",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Cookie": f"ZM_AUTH_TOKEN={token}",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        text = response.read().decode("utf-8", errors="replace")
    match = re.search(r'["\']?aid["\']?\s*[:=]\s*["\']([^"\']+)["\']', text)
    if match:
        return match.group(1)
    quoted = re.findall(r"'([^']+)'", text)
    if len(quoted) >= 2:
        return quoted[-1]
    raise RuntimeError(f"Zimbra upload failed: attachment id not found in response {text[:300]}")


def zimbra_move_message(host, token, message_id, folder_id):
    soap_request(
        host,
        (
            f'<MsgActionRequest xmlns="urn:zimbraMail">'
            f'<action id="{html.escape(message_id)}" op="move" l="{html.escape(str(folder_id))}"/>'
            f"</MsgActionRequest>"
        ),
        token,
    )


def zimbra_mark_read(host, token, message_id):
    soap_request(
        host,
        (
            f'<MsgActionRequest xmlns="urn:zimbraMail">'
            f'<action id="{html.escape(str(message_id))}" op="read"/>'
            f"</MsgActionRequest>"
        ),
        token,
    )


def zimbra_list_folders(host, token):
    root = soap_request(
        host,
        '<GetFolderRequest xmlns="urn:zimbraMail"><folder/></GetFolderRequest>',
        token,
    )
    folders = []
    for elem in root.iter():
        if _local_name(elem.tag) != "folder":
            continue
        folder_id = (elem.get("id") or "").strip()
        name = (elem.get("name") or "").strip()
        parent_id = (elem.get("l") or "").strip()
        if not folder_id or not name:
            continue
        folders.append({"id": folder_id, "name": name, "parent_id": parent_id})
    return folders


def zimbra_find_folder(folders, *, name=None, name_prefix=None, parent_id=None):
    name_needle = (name or "").strip().lower() or None
    prefix = (name_prefix or "").strip().lower() or None
    parent = str(parent_id).strip() if parent_id is not None and str(parent_id).strip() else None

    matches = []
    for folder in folders or []:
        if parent is not None and folder.get("parent_id") != parent:
            continue
        folder_name = (folder.get("name") or "").strip().lower()
        if name_needle is not None and folder_name != name_needle:
            continue
        if prefix is not None and not folder_name.startswith(prefix):
            continue
        matches.append(folder)

    if not matches:
        return None
    matches.sort(key=lambda f: (len(f.get("name") or ""), f.get("name") or ""))
    return matches[0]


def _normalize_recipients(value):
    if isinstance(value, (list, tuple, set)):
        return [str(addr).strip() for addr in value if str(addr).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def zimbra_send_email(cfg, to, subject, body, attachments=None, folder_id=None, cc=None, content_type="text/plain"):
    require_zimbra_config(cfg)
    host = zimbra_host(cfg)
    token = zimbra_login(cfg)
    attach_ids = []
    for item in attachments or []:
        attach_ids.append(
            upload_attachment(
                host,
                token,
                item["filename"],
                item["data"],
                item.get("content_type", "application/octet-stream"),
            )
        )

    attach_xml = "".join(f'<attach aid="{html.escape(aid)}"/>' for aid in attach_ids)
    subject_text = str(subject or "").strip()
    recipients = _normalize_recipients(to)
    if not recipients:
        raise ValueError("Missing email recipient")
    to_xml = "".join(f'<e t="t" a="{html.escape(addr)}"/>' for addr in recipients)
    cc_xml = "".join(f'<e t="c" a="{html.escape(addr)}"/>' for addr in _normalize_recipients(cc))
    soap_request(
        host,
        f"""<SendMsgRequest xmlns="urn:zimbraMail">
  <m>
    {to_xml}
    {cc_xml}
    <su>{html.escape(subject_text)}</su>
    <mp ct="{html.escape(content_type)}"><content>{html.escape(str(body or ""))}</content></mp>
    {attach_xml}
  </m>
</SendMsgRequest>""",
        token,
    )

    dest = str(folder_id or "").strip()
    if not dest or dest == "2":
        return

    # Self-transfer mail lands in Inbox; move it into the configured receive folder.
    for attempt in range(8):
        for message_id in zimbra_search(host, token, "2", 20):
            message = zimbra_get_message(host, token, message_id)
            if message and (message.get("subject") or "").strip() == subject_text:
                zimbra_move_message(host, token, message_id, dest)
                return
        time.sleep(0.5 * (attempt + 1))
    raise RuntimeError(f"Transfer sent but message not found in Inbox to move to folder {dest}")


def zimbra_search(host, token, folder_id, limit, sort_by="dateDesc", unread_only=False):
    query = f"inid:{folder_id}"
    if unread_only:
        query = f"{query} is:unread"
    query = html.escape(query)
    sort = html.escape(str(sort_by or "dateDesc"))
    root = soap_request(
        host,
        f"""<SearchRequest xmlns="urn:zimbraMail" types="message" sortBy="{sort}" limit="{int(limit)}">
  <query>{query}</query>
</SearchRequest>""",
        token,
    )
    return [elem.get("id", "") for elem in root.iter() if _local_name(elem.tag) == "m" and elem.get("id")]


def _html_gap():
    # Allow tags / whitespace / common HTML entities between marker words.
    return r"(?:<[^>]+>|\s|&nbsp;|&#160;)*"


def _trim_forward_body(html_or_text):
    """Keep only the HTML/text slice from Hi all through Best regards."""
    text = str(html_or_text or "")
    if not text.strip():
        return text

    gap = _html_gap()
    start_re = re.compile(rf"(?is)\bHi{gap}all\b")
    end_re = re.compile(rf"(?is)\bBest{gap}regards\b,?")

    start_match = start_re.search(text)
    if not start_match:
        return text

    end_match = end_re.search(text, start_match.start())
    if not end_match:
        return text

    return text[start_match.start() : end_match.end()]


def _html_to_plain(html_or_text):
    text = str(html_or_text or "")
    if not text.strip():
        return ""

    text = re.sub(r"(?is)<(br|/div|/p|/tr|/li|hr)\b[^>]*>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", "", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_alert_sections(plain_text):
    """Split plain alert text into [(title, body), ...] with Summary first."""
    text = str(plain_text or "").strip()
    if not text:
        return []

    header_re = re.compile(
        r"(?m)^(?:---\s*(.+?)\s*---|(Root Cause Analysis[^\n]*))\s*$"
    )
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


def _severity_badge(value):
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
        f'background:{bg};color:{fg};font-size:12px;font-weight:700;'
        f'letter-spacing:0.02em;">{html.escape(raw)}</span>'
    )


def _is_key_value_section(title):
    low = (title or "").strip().lower()
    return low in {"alert details", "activity details"}


def _render_key_value_rows(body):
    rows = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            rows.append(
                f'<div style="padding:8px 0;color:#111827;border-bottom:1px solid #f1f5f9;">'
                f"{html.escape(line)}</div>"
            )
            continue
        label, value = line.split(":", 1)
        label = label.strip()
        value = value.strip()
        if label.lower() == "severity":
            value_html = _severity_badge(value)
        else:
            value_html = html.escape(value)
        rows.append(
            '<div style="display:flex;gap:12px;padding:8px 0;border-bottom:1px solid #f1f5f9;">'
            f'<div style="min-width:140px;font-weight:700;color:#334155;">{html.escape(label)}</div>'
            f'<div style="color:#111827;flex:1;">{value_html}</div>'
            "</div>"
        )
    if not rows:
        return '<div style="color:#64748b;">No details</div>'
    # Drop last border for cleaner card edge.
    rows[-1] = rows[-1].replace("border-bottom:1px solid #f1f5f9;", "border-bottom:none;")
    return "\n".join(rows)


def _render_narrative_body(body):
    chunks = [c.strip() for c in re.split(r"\n\s*\n", body) if c.strip()]
    if not chunks:
        chunks = [line.strip() for line in body.splitlines() if line.strip()]

    parts = []
    for chunk in chunks:
        escaped = html.escape(chunk).replace("\n", "<br>\n")
        if re.match(r"^[a-z]\.\s", chunk, re.IGNORECASE):
            # Plain lettered actions — no nested sub-cards.
            parts.append(
                f'<p style="margin:0 0 10px 0;padding-left:8px;color:#111827;">{escaped}</p>'
            )
        else:
            parts.append(f'<p style="margin:0 0 12px 0;">{escaped}</p>')
    if parts:
        parts[-1] = parts[-1].replace("margin:0 0 12px 0;", "margin:0;")
        parts[-1] = parts[-1].replace("margin:0 0 10px 0;", "margin:0;")
    return "\n".join(parts)


def _extract_greeting(summary_body):
    """Pull leading Hi all out of the summary so it can sit above cards."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", str(summary_body or "")) if p.strip()]
    if paragraphs and re.match(r"(?i)^Hi\s+all\b", paragraphs[0]):
        return paragraphs[0], "\n\n".join(paragraphs[1:]).strip()
    return "", str(summary_body or "").strip()


def _render_summary_section(body):
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    parts = []
    for para in paragraphs:
        escaped = html.escape(para).replace("\n", "<br>\n")
        parts.append(f'<p style="margin:0 0 12px 0;color:#334155;">{escaped}</p>')
    if parts:
        parts[-1] = parts[-1].replace("margin:0 0 12px 0;", "margin:0;")
    return "\n".join(parts)


def _render_section_card(title, body):
    title_text = (title or "Details").strip()
    if title_text.lower() == "summary":
        content = _render_summary_section(body)
    elif _is_key_value_section(title_text):
        content = _render_key_value_rows(body)
    else:
        content = _render_narrative_body(body)

    if not content.strip():
        return ""

    return (
        '<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:8px;'
        'padding:16px;margin:0 0 14px 0;">\n'
        '<div style="border-left:4px solid #0f766e;padding-left:10px;margin-bottom:12px;">\n'
        f'<div style="font-size:13px;font-weight:700;letter-spacing:0.04em;'
        f'text-transform:uppercase;color:#0f172a;">{html.escape(title_text)}</div>\n'
        "</div>\n"
        f"{content}\n"
        "</div>"
    )


def _format_forward_html(plain_text, signature_html="", signature_position="down"):
    plain = str(plain_text or "").strip()
    if not plain:
        return ""

    # Drop trailing Best regards so the styled/account signature is the closer.
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
            "<strong style=\"color:#0f172a;\">CITICTEL-CPC SOC TEAM</strong>"
            "</div>"
        )

    if position == "up":
        inner = f"{signature}\n{content}\n"
    else:
        inner = f"{content}\n{signature}\n"

    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;'
        "color:#1f2937;line-height:1.5;max-width:720px;background:#f8fafc;"
        'padding:16px;border-radius:10px;">\n'
        f"{inner}"
        "</div>"
    )


def _clean_subject(subject):
    cleaned = str(subject or "").strip()
    prefix_re = re.compile(r"^(?:re|fwd|fw)\s*:\s*", re.IGNORECASE)
    while True:
        updated = prefix_re.sub("", cleaned, count=1).strip()
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


DEFAULT_FORWARD_SIGNATURE_ID = "25ea6e17-aec8-4af4-8ab4-ac2795396549"
DEFAULT_FORWARD_SIGNATURE_NAME = (
    "SOC Team (Zimbra0020-0020CITIC0020Telecom0020CPC0020SOC@example.com)"
)


def zimbra_get_signature(host, token, name=None, signature_id=None):
    """Return signature HTML content by id and/or name via GetSignaturesRequest."""
    want_id = str(signature_id or "").strip()
    want_name = str(name or "").strip()
    root = soap_request(
        host,
        '<GetSignaturesRequest xmlns="urn:zimbraAccount"/>',
        token,
    )
    for elem in root.iter():
        if _local_name(elem.tag) != "signature":
            continue
        sig_id = (elem.get("id") or "").strip()
        sig_name = (elem.get("name") or "").strip()
        if want_id and sig_id != want_id:
            continue
        if want_name and sig_name != want_name:
            continue
        if not want_id and not want_name:
            continue
        for child in elem.iter():
            if _local_name(child.tag) == "content" and (child.text or "").strip():
                return child.text
    return ""


def _social_link_label(href):
    low = str(href or "").lower()
    if "facebook.com" in low:
        return "Facebook"
    if "linkedin.com" in low:
        return "LinkedIn"
    if "twitter.com" in low or "x.com" in low:
        return "X"
    if "youtube.com" in low:
        return "YouTube"
    if "instagram.com" in low:
        return "Instagram"
    if "citictel-cpc.com" in low:
        return "Website"
    return "Link"


def _sanitize_signature_html(signature_html):
    """Drop signature images when the signature includes 'Connect with us'."""
    text = str(signature_html or "")
    if not text.strip():
        return ""
    if not re.search(r"connect\s+with\s+us\s*:", text, re.IGNORECASE):
        return text

    # Remove all pictures from this signature.
    text = re.sub(r"(?is)<img\b[^>]*/?>", "", text)

    # Icon-only social anchors become empty after img removal — use text labels.
    parts = []
    pos = 0
    for m in re.finditer(
        r'(?is)(<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>)(.*?)(</a>)',
        text,
    ):
        parts.append(text[pos : m.start()])
        open_tag, href, inner, close_tag = m.group(1), m.group(2), m.group(3), m.group(4)
        visible = re.sub(r"(?is)<[^>]+>", "", inner)
        visible = html.unescape(visible).replace("\xa0", " ").strip()
        if visible:
            parts.append(m.group(0))
        else:
            label = html.escape(_social_link_label(html.unescape(href)))
            parts.append(f"{open_tag}{label}{close_tag}")
        pos = m.end()
    parts.append(text[pos:])
    return "".join(parts)


def zimbra_forward_as_is(
    cfg,
    message_id,
    to,
    cc=None,
    token=None,
    signature_id=None,
    signature_name=None,
    signature_position=None,
):
    """Forward a trimmed, restyled HTML message from the logged-in SOC account."""
    require_zimbra_config(cfg)
    host = zimbra_host(cfg)
    token = token or zimbra_login(cfg)
    recipients = _normalize_recipients(to)
    if not recipients:
        raise ValueError("Missing email recipient")
    msg_id = str(message_id or "").strip()
    if not msg_id:
        raise ValueError("Missing message id")

    message = zimbra_get_message(host, token, msg_id)
    if not message:
        raise RuntimeError(f"Message not found: {msg_id}")

    subject = _clean_subject((message.get("subject") or "").strip())

    original_html = (message.get("body_html") or "").strip()
    original_text = (message.get("body") or "").strip()
    if original_html:
        content_html = original_html
    elif original_text:
        content_html = html.escape(original_text).replace("\n", "<br>\n")
    else:
        content_html = ""

    sig_id = str(signature_id if signature_id is not None else cfg.get("signature_id") or "").strip()
    sig_name = str(
        signature_name if signature_name is not None else cfg.get("signature_name") or ""
    ).strip()
    if not sig_id and not sig_name:
        sig_id = DEFAULT_FORWARD_SIGNATURE_ID
        sig_name = DEFAULT_FORWARD_SIGNATURE_NAME

    # Prefer id when configured; fall back to name.
    signature_html = ""
    if sig_id:
        signature_html = zimbra_get_signature(host, token, signature_id=sig_id)
    if not signature_html and sig_name:
        signature_html = zimbra_get_signature(host, token, name=sig_name)
    signature_html = _sanitize_signature_html(signature_html)

    position = str(
        signature_position
        if signature_position is not None
        else cfg.get("signature_position") or "down"
    ).strip().lower()
    if position not in {"up", "down"}:
        position = "down"

    trimmed = _trim_forward_body(content_html)
    body = _format_forward_html(
        _html_to_plain(trimmed),
        signature_html=signature_html,
        signature_position=position,
    )

    to_xml = "".join(f'<e t="t" a="{html.escape(addr)}"/>' for addr in recipients)
    cc_xml = "".join(f'<e t="c" a="{html.escape(addr)}"/>' for addr in _normalize_recipients(cc))
    root = soap_request(
        host,
        f"""<SendMsgRequest xmlns="urn:zimbraMail">
  <m>
    {to_xml}
    {cc_xml}
    <su>{html.escape(subject)}</su>
    <mp ct="text/html"><content>{html.escape(body)}</content></mp>
  </m>
</SendMsgRequest>""",
        token,
    )
    if next((e for e in root.iter() if _local_name(e.tag) == "Fault"), None) is not None:
        raise RuntimeError("Zimbra SendMsg forward returned a SOAP fault")
    return root


def _extract_message_bodies(msg_elem):
    plain_parts = []
    html_parts = []
    for elem in msg_elem.iter():
        if _local_name(elem.tag) != "mp":
            continue
        if elem.get("filename") or elem.get("cd") == "attachment":
            continue
        content_elem = next((child for child in list(elem) if _local_name(child.tag) == "content"), None)
        if content_elem is None or not (content_elem.text or "").strip():
            continue
        ct = (elem.get("ct") or "").lower()
        text = content_elem.text
        if ct.startswith("text/plain"):
            plain_parts.append(text)
        elif ct.startswith("text/html"):
            html_parts.append(text)
    return {
        "text": "\n".join(plain_parts).strip(),
        "html": "\n".join(html_parts).strip(),
    }


def zimbra_get_message(host, token, message_id):
    root = soap_request(
        host,
        f'<GetMsgRequest xmlns="urn:zimbraMail"><m id="{html.escape(message_id)}" html="1" needExp="1"/></GetMsgRequest>',
        token,
    )
    msg = next((elem for elem in root.iter() if _local_name(elem.tag) == "m" and elem.get("id") == message_id), None)
    if msg is None:
        return None

    subject_elem = next((elem for elem in msg.iter() if _local_name(elem.tag) == "su"), None)
    addresses = []
    attachments = []
    for elem in msg.iter():
        name = _local_name(elem.tag)
        if name == "e":
            addresses.append({"type": elem.get("t", ""), "email": elem.get("a", "")})
        elif name == "mp" and (elem.get("filename") or elem.get("cd") == "attachment"):
            attachments.append(
                {
                    "filename": elem.get("filename", ""),
                    "part": elem.get("part", ""),
                    "content_type": elem.get("ct", ""),
                }
            )

    bodies = _extract_message_bodies(msg)
    return {
        "id": message_id,
        "subject": (subject_elem.text if subject_elem is not None else "") or "",
        "from": next((a["email"] for a in addresses if a["type"] == "f"), ""),
        "to": [a["email"] for a in addresses if a["type"] == "t"],
        "body": bodies["text"],
        "body_html": bodies["html"],
        "attachments": attachments,
    }


def download_attachment(cfg, token, message_id, part):
    host = zimbra_host(cfg)
    account = urllib.parse.quote(zimbra_email(cfg), safe="")
    query = urllib.parse.urlencode({"id": message_id, "part": part})
    request = urllib.request.Request(
        f"https://{host}/home/{account}/?{query}",
        headers={"Cookie": f"ZM_AUTH_TOKEN={token}"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def zimbra_delete_message(host, token, message_id):
    soap_request(
        host,
        f'<MsgActionRequest xmlns="urn:zimbraMail"><action id="{html.escape(message_id)}" op="delete"/></MsgActionRequest>',
        token,
    )
