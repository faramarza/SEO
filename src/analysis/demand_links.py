"""Put a demand-task link at the end of the right paragraph, by itself (Governor
writes the page; the owner just clicks). Pure functions, unit-tested."""
import re


def insert_at_paragraph_end(html, find, paste):
    """Insert `paste` right after `find` when `find` is the END of a paragraph
    (only whitespace, then </p> or </li>) that has no link yet, and appears exactly
    once. Returns the new html, or None when that can't be done safely."""
    if not html or not find or not paste:
        return None
    hits = [m for m in re.finditer(re.escape(find), html)]
    ends = []
    for m in hits:
        after = html[m.end():m.end() + 40]
        close = re.match(r"\s*</(p|li)>", after, re.I)
        if not close:
            continue
        start = max(html.rfind("<p", 0, m.start()), html.rfind("<li", 0, m.start()))
        if start >= 0 and re.search(r"(?i)<a\b", html[start:m.start()]):
            continue   # the paragraph already has a link
        ends.append(m)
    if len(ends) != 1:
        return None
    m = ends[0]
    text = paste if paste.startswith(" ") else " " + paste
    return html[:m.end()] + text + html[m.end():]
