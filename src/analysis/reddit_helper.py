"""Reddit helper: find this week's threads where Alphabet Trains' products genuinely
answer the question, and draft a helpful reply for Frank to edit and post himself.
Governor NEVER posts.

Rules the drafts follow (Reddit punishes advertising):
  - help first: answer the actual question in plain words;
  - warm-up mode (default): no mention of the store at all;
  - after warm-up: at most one honest line ("I run a small toy shop, so I'm biased")
    and only when one of our products truly fits; never a link unless Frank adds it;
  - no fake experiences ("my kids loved it"), no claims beyond the catalog facts.
Threads are found through Google (Serper, site:reddit.com, past week), because Reddit
often blocks servers from reading it directly.
Pure functions here; the web layer does the search and the AI call.
"""
from __future__ import annotations

import re

TOPICS = [  # (subreddit, search words, what we can honestly help with)
    ("Teachers", "classroom rug", "classroom rugs (Carpets for Kids), sizes from 6x9 to 8x12, seating rugs with squares"),
    ("ECEProfessionals", "rug OR carpet classroom", "classroom and circle time rugs for preschool and daycare"),
    ("ECEProfessionals", "classroom furniture OR shelves OR cubbies", "Whitney Brothers and Guidecraft classroom furniture"),
    ("Montessori", "gift", "personalized wooden name trains (ages 3+), wooden name puzzles, Guidecraft Montessori toys"),
    ("toddlers", "gift ideas", "personalized name trains for ages 3+, name puzzles, personalized books"),
    ("Parenting", "personalized gift", "personalized wooden name trains, name puzzles and personalized story books"),
    ("Grandparents", "gift grandchild", "personalized name trains and name puzzles as keepsake gifts"),
]

BANNED = re.compile(r"(#\s?1\b|best in the world|guarantee|cheapest|award|certified|non-?toxic|"
                    r"use (my|our) code|discount code|click here|link in|my (kids|son|daughter) (love|loved))", re.I)


def queries():
    """[(subreddit, google query, offer)] for the weekly search. Pure."""
    return [(sub, f'site:reddit.com/r/{sub} {words}', offer) for sub, words, offer in TOPICS]


def parse_results(serper_json: dict, sub: str, offer: str) -> list:
    """Serper organic results -> threads [{id, sub, title, url, snippet, offer}]. Pure."""
    out = []
    for r in (serper_json or {}).get("organic") or []:
        url = r.get("link") or ""
        m = re.search(r"reddit\.com/r/([^/]+)/comments/([a-z0-9]+)", url, re.I)
        if not m:
            continue
        out.append({"id": m.group(2).lower(), "sub": m.group(1), "title": (r.get("title") or "").replace(" : r/" + sub, "").strip(),
                    "url": url.split("?")[0], "snippet": r.get("snippet") or "", "offer": offer})
    return out


def new_threads(found: list, seen: set, limit: int = 6) -> list:
    """Dedupe across topics and against threads already shown. Pure."""
    out, ids = [], set(seen)
    for t in found:
        if t["id"] in ids:
            continue
        ids.add(t["id"])
        out.append(t)
        if len(out) >= limit:
            break
    return out


def system_prompt() -> str:
    return ("You help a small family toy-store owner write genuinely helpful Reddit replies. Reddit "
            "dislikes advertising. Write like a knowledgeable person, plain and warm, 50 to 120 words, no "
            "headings, no bullet lists unless the question asks for options, no links, no prices unless asked. "
            "Never claim personal experiences (no 'my kids loved it'). Return ONLY JSON.")


def user_prompt(thread: dict, mention: bool) -> str:
    rule = ("Do NOT mention any store or brand of ours at all; just help." if not mention else
            "If, and only if, one of these honestly fits the question, you may end with ONE line that starts "
            "\"Full disclosure: I run a small toy shop (Alphabet Trains), so I'm biased, but\" and names the item. "
            f"What we sell that might fit: {thread['offer']}. If it does not clearly fit, do not mention it.")
    return (f"Subreddit: r/{thread['sub']}\nThread title: {thread['title']}\nWhat Google shows of the post: "
            f"{thread['snippet']}\n\n{rule}\n\nReturn JSON: {{\"reply\": \"...\", \"fits\": true or false "
            f"(does one of our products genuinely fit this question?)}}")


def clean_reply(text: str, mention: bool) -> str:
    """Strip links, banned phrasing and (in warm-up) any store mention. Pure."""
    t = re.sub(r"https?://\S+|www\.\S+", "", str(text or ""))
    sents = [s for s in re.split(r"(?<=[.!?])\s+", t.strip()) if s and not BANNED.search(s)]
    if not mention:
        sents = [s for s in sents if not re.search(r"alphabet trains|full disclosure|my (shop|store)", s, re.I)]
    return " ".join(sents).strip()
