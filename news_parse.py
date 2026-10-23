"""news_parse.py - pure logic of the news sync (no network): row parser, time zone conversion, duplicate rule.

Input = the text of the Gemini output document, one article per row:
  Category | Impacted Assets | Title | Summary | Sentiment | Is Major | Published Time [zone] [| Keywords]
Rows are read from both ends (first three fields from the left, the tail from the right), so a `|` inside the summary no longer breaks a row; the older 7-column rows
(no Keywords) and a Published Time without a zone (taken as KST) still work. A zone is one of KST, JST, UTC, GMT, ET, EST, EDT, CET, CEST - converted with zoneinfo,
so US daylight saving needs no rule in the prompt.
"""
import difflib
import re
from collections import Counter
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from news_tags import CATEGORIES, CATEGORY_ALIASES, normalize_category, normalize_tags, rule_tags

KST = ZoneInfo("Asia/Seoul")
TZ_MAP = {"KST": "Asia/Seoul", "JST": "Asia/Tokyo", "UTC": "UTC", "GMT": "UTC", "ET": "America/New_York", "EST": "America/New_York", "EDT": "America/New_York",
          "CET": "Europe/Paris", "CEST": "Europe/Paris"}
_TAIL = re.compile(r"^(?P<head>.*)\|\s*(?P<score>[1-5])\s*\|\s*(?P<major>True|False|true|false|TRUE|FALSE)\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2})[ T](?P<hm>\d{2}:\d{2})\s*(?P<tz>[A-Za-z]{2,4})?\s*(?:\|\s*(?P<kw>[^|]*?))?\s*\|?\s*$", re.S)
# a new row glued to the end of the previous one on the same line ("... | 금리, 채권 외환/채권 | ...") starts at a category name followed by a pipe
_GLUED = re.compile(r"(?<=\S)[ 	]+(?=(?:거시경제/지수|외환/채권|원자재/에너지|원재료/에너지|지정학/글로벌|기업/산업)\s*\|)")
_KNOWN_FIRST = set(CATEGORIES) | set(CATEGORY_ALIASES)
MAX_ROW_LINES = 12


def to_kst(date_str, hm, tz=None):
    """Naive KST wall-clock datetime of `date hm` given in zone `tz` (None = KST). market_news.created_at keeps this wall clock (stored as if UTC, as before)."""
    zone = TZ_MAP.get((tz or "KST").upper())
    if zone is None:
        raise ValueError(f"unknown zone {tz}")
    t = datetime.strptime(f"{date_str} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(zone))
    return t.astimezone(KST).replace(tzinfo=None)


def parse_news_text(raw_text, now=None, max_age_hours=36, max_future_hours=3):
    """-> (rows, rejects). rows: dict(category, assets, title, summary, score, is_major, created_at (naive KST datetime), tags, tags_from_rules, unknown_tags).
    rejects: list of (reason, first 160 chars) - nothing is dropped silently. With `now` (naive KST), a row published more than max_age_hours before it or more than
    max_future_hours after it is rejected as stale / future (the prompts ask for the last 12 hours; an older date means a wrong date or an old article)."""
    rows, rejects, buf = [], [], []
    text = _GLUED.sub("\n", (raw_text or "").replace("```", "").replace("**", ""))

    def reject(reason):
        rejects.append((reason, " ".join(buf)[:160]))

    for line in text.split("\n"):
        s = line.strip()
        if not s or s.lower().startswith("category") or s.startswith("["):
            continue
        first = s.split("|", 1)[0].strip()
        if buf and "|" in s and first in _KNOWN_FIRST:      # a new row starts while the previous one never completed
            reject("incomplete row")
            buf = []
        buf.append(s)
        joined = "<br><br>".join(buf)
        m = _TAIL.match(joined)
        if not m:
            if len(buf) > MAX_ROW_LINES:
                reject("no valid tail (score | True/False | time) within %d lines" % MAX_ROW_LINES)
                buf = []
            continue
        parts = m.group("head").split("|", 3)
        if len(parts) == 3:          # Title column skipped by the model: the first chunk of the text is the headline, the rest is the summary
            chunks = [c.strip() for c in parts[2].split("<br><br>") if c.strip()]
            if len(chunks) >= 3:
                parts = [parts[0], parts[1], chunks[0].lstrip("- ").strip(), "<br><br>".join(chunks[1:])]
        if len(parts) < 4 or not parts[2].strip() or not parts[3].strip().strip("|").strip():
            reject("too few fields before the tail")
            buf = []
            continue
        try:
            created = to_kst(m.group("date"), m.group("hm"), m.group("tz"))
        except ValueError as e:
            reject(f"bad time: {e}")
            buf = []
            continue
        if now is not None and created < now - timedelta(hours=max_age_hours):
            reject(f"stale: published {created:%Y-%m-%d %H:%M}, more than {max_age_hours} h before the run")
            buf = []
            continue
        if now is not None and created > now + timedelta(hours=max_future_hours):
            reject(f"future: published {created:%Y-%m-%d %H:%M}")
            buf = []
            continue
        category, assets, title, summary = (p.strip() for p in parts)
        tags, unknown = normalize_tags(m.group("kw") or "")
        from_rules = False
        if not tags:
            tags, from_rules = rule_tags(title, summary, assets), True
        rows.append(dict(category=normalize_category(category, tags), assets=assets, title=title, summary=summary, score=int(m.group("score")),
                         is_major=m.group("major").lower() == "true", created_at=created, tags=tags, tags_from_rules=from_rules, unknown_tags=unknown))
        buf = []
    if buf:
        reject("unfinished row at the end")
    return rows, rejects


def _norm_title(t):
    return re.sub(r"[\W_]+", "", (t or "").lower())


def _bigrams(s):
    s = _norm_title(s)
    return Counter(s[i:i + 2] for i in range(len(s) - 1))


def _dice(a, b):
    x, y = _bigrams(a), _bigrams(b)
    return 2 * sum((x & y).values()) / max(1, sum(x.values()) + sum(y.values()))


def _keys(title, summary):
    """Content words (2+ chars, numbers kept) of the title and the first bullet of the summary."""
    first = (summary or "").split("<br><br>")[0]
    return {w for w in re.findall(r"[0-9A-Za-z가-힣%.]+", f"{title} {first}") if len(w) >= 2}


def is_duplicate(title, category, created_at, recent, threshold=0.8, hours=48, summary=""):
    """True when an article of `recent` [(title, category, created_at naive KST[, summary]), ...] within `hours` and in the same category looks like the same story:
    title similarity >= threshold (SequenceMatcher; identical titles always), OR bigram Dice of the titles >= 0.55, OR Dice >= 0.40 together with >= 50% shared content
    words of title + first bullet (needs the summaries). Measured 2026-10-23 on two real Gemini runs: same-event pairs score 0.09-0.75, different events 0.16-0.39, so the
    rule catches the paraphrases that share wording (gas prices, rate decision, 10-year yield) but not every re-write of one event (a semantic match would be needed)."""
    a = _norm_title(title)
    ka = _keys(title, summary) if summary else None
    for rec in recent:
        t2, c2, w2 = rec[0], rec[1], rec[2]
        if c2 != category or abs(created_at - w2) > timedelta(hours=hours):
            continue
        b = _norm_title(t2)
        if a == b:
            return True
        sm = difflib.SequenceMatcher(None, a, b)
        if sm.real_quick_ratio() >= threshold and sm.quick_ratio() >= threshold and sm.ratio() >= threshold:
            return True
        d = _dice(title, t2)
        if d >= 0.55:
            return True
        if d >= 0.40 and ka is not None and len(rec) > 3 and rec[3]:
            kb = _keys(t2, rec[3])
            if kb and len(ka & kb) / max(1, min(len(ka), len(kb))) >= 0.5:
                return True
    return False
