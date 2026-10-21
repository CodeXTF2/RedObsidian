"""A small, escape-everything Markdown parser shared by the HTML report preview and the DOCX writer.

Findings routinely contain attack payloads, so raw HTML is never passed through: every character of
text is escaped, links must be http(s) or same-site paths, and images must be same-site paths.
"""
import re
from html import escape

FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([\w+-]*)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
IMAGE_LINE_RE = re.compile(r"^\s*!\[([^\]]*)\]\(([^)\s]+)\)\s*$")
INLINE_RE = re.compile(
    r"(?P<code>`[^`]+`)"
    r"|(?P<image>!\[[^\]]*\]\([^)\s]+\))"
    r"|(?P<link>\[[^\]]+\]\([^)\s]+\))"
    r"|(?P<bold>\*\*[^*]+\*\*|__[^_]+__)"
    r"|(?P<italic>\*[^*\s][^*]*\*|_[^_\s][^_]*_)"
)


def safe_link(url):
    url = (url or "").strip()
    if re.match(r"^https?://", url, re.I) or (url.startswith("/") and not url.startswith("//")):
        return url
    return None


def safe_image(url):
    url = (url or "").strip()
    return url if url.startswith("/") and not url.startswith("//") else None


def parse_blocks(text):
    """Split Markdown into (kind, data) blocks: heading, paragraph, code, list, quote, image, hr."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        fence = FENCE_RE.match(line)
        if fence:
            marker, lang = fence.group(1), fence.group(2)
            body = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            blocks.append(("code", {"text": "\n".join(body), "lang": lang}))
            i += 1
            continue
        heading = HEADING_RE.match(line)
        if heading:
            blocks.append(("heading", {"level": len(heading.group(1)), "text": heading.group(2)}))
            i += 1
            continue
        if HR_RE.match(line):
            blocks.append(("hr", {}))
            i += 1
            continue
        image = IMAGE_LINE_RE.match(line)
        if image:
            blocks.append(("image", {"alt": image.group(1), "url": image.group(2)}))
            i += 1
            continue
        for kind, pattern in (("bullet", BULLET_RE), ("ordered", ORDERED_RE)):
            if pattern.match(line):
                items = []
                while i < len(lines) and pattern.match(lines[i]):
                    items.append(pattern.match(lines[i]).group(1))
                    i += 1
                blocks.append(("list", {"ordered": kind == "ordered", "items": items}))
                break
        else:
            if QUOTE_RE.match(line):
                quoted = []
                while i < len(lines) and QUOTE_RE.match(lines[i]):
                    quoted.append(QUOTE_RE.match(lines[i]).group(1))
                    i += 1
                blocks.append(("quote", {"text": " ".join(quoted)}))
                continue
            paragraph = []
            while i < len(lines) and lines[i].strip() and not _starts_block(lines[i]):
                paragraph.append(lines[i].strip())
                i += 1
            blocks.append(("paragraph", {"text": "\n".join(paragraph)}))
    return blocks


def _starts_block(line):
    return any(pattern.match(line) for pattern in (FENCE_RE, HEADING_RE, BULLET_RE, ORDERED_RE, QUOTE_RE,
                                                   HR_RE, IMAGE_LINE_RE))


def parse_inline(text):
    """Split a line into (kind, value) runs: text, code, bold, italic, link (text, url), image (alt, url)."""
    runs = []
    position = 0
    for match in INLINE_RE.finditer(text or ""):
        if match.start() > position:
            runs.append(("text", text[position:match.start()]))
        token = match.group(0)
        kind = match.lastgroup
        if kind == "code":
            runs.append(("code", token[1:-1]))
        elif kind in ("link", "image"):
            label, url = re.match(r"!?\[([^\]]*)\]\(([^)\s]+)\)", token).groups()
            runs.append((kind, (label, url)))
        elif kind == "bold":
            runs.append(("bold", token[2:-2]))
        else:
            runs.append(("italic", token[1:-1]))
        position = match.end()
    if position < len(text or ""):
        runs.append(("text", text[position:]))
    return runs


def inline_html(text):
    parts = []
    for kind, value in parse_inline(text):
        if kind == "text":
            parts.append(escape(value).replace("\n", "<br>"))
        elif kind == "code":
            parts.append(f"<code>{escape(value)}</code>")
        elif kind == "bold":
            parts.append(f"<strong>{escape(value)}</strong>")
        elif kind == "italic":
            parts.append(f"<em>{escape(value)}</em>")
        elif kind == "link":
            label, url = value
            href = safe_link(url)
            parts.append(f'<a href="{escape(href)}" rel="noreferrer">{escape(label)}</a>' if href else escape(label))
        elif kind == "image":
            alt, url = value
            src = safe_image(url)
            parts.append(f'<img src="{escape(src)}" alt="{escape(alt)}">' if src else escape(alt))
    return "".join(parts)


def to_html(text):
    out = []
    for kind, data in parse_blocks(text):
        if kind == "heading":
            level = min(6, data["level"] + 2)  # nested under the report's own headings
            out.append(f"<h{level}>{inline_html(data['text'])}</h{level}>")
        elif kind == "paragraph":
            out.append(f"<p>{inline_html(data['text'])}</p>")
        elif kind == "code":
            out.append(f"<pre><code>{escape(data['text'])}</code></pre>")
        elif kind == "list":
            tag = "ol" if data["ordered"] else "ul"
            items = "".join(f"<li>{inline_html(item)}</li>" for item in data["items"])
            out.append(f"<{tag}>{items}</{tag}>")
        elif kind == "quote":
            out.append(f"<blockquote>{inline_html(data['text'])}</blockquote>")
        elif kind == "image":
            src = safe_image(data["url"])
            if src:
                alt = escape(data["alt"])
                caption = f"<figcaption>{alt}</figcaption>" if alt else ""
                out.append(f'<figure><img src="{escape(src)}" alt="{alt}">{caption}</figure>')
        elif kind == "hr":
            out.append("<hr>")
    return "\n".join(out)
