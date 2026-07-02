#!/usr/bin/env python3
"""
Daily arXiv keyword alert.

Fetches the most recent submissions in a chosen arXiv category, keeps only the
papers whose title or abstract matches the keyword rules in config.yaml, and
emails them to you. Already-seen IDs are stored in seen.json so the same paper
is never emailed twice.

Credentials are read from environment variables (set them as GitHub secrets):
    SMTP_USER  - the Gmail address you send FROM   (e.g. pngwenhan@gmail.com)
    SMTP_PASS  - a Gmail *App Password* (NOT your normal password)
    MAIL_TO    - recipient (optional; defaults to pngwenhan@gmail.com)
    SMTP_HOST  - optional, default smtp.gmail.com
    SMTP_PORT  - optional, default 587
"""

import os
import sys
import re
import json
import html
import time
import random
import smtplib
import datetime as dt
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
import feedparser
import yaml

API_URL = "https://export.arxiv.org/api/query"
RSS_URL = "https://rss.arxiv.org/rss/{cat}"
UA = {"User-Agent": "new-research-alert/1.0 (GitHub Actions; arXiv daily digest)"}
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
SEEN_PATH = ROOT / "seen.json"
DEFAULT_TO = "pngwenhan@gmail.com"


# ── config + state ──────────────────────────────────────────────────────────
def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    if SEEN_PATH.exists():
        try:
            return set(json.loads(SEEN_PATH.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen):
    # keep only the most recent 8000 ids so the file can't grow without bound
    SEEN_PATH.write_text(json.dumps(sorted(seen)[-8000:], indent=0))


# ── arXiv fetching ──────────────────────────────────────────────────────────
def _get(url, params=None, retries=6):
    """GET with polite backoff. Honors Retry-After and backs off on 429/5xx.

    arXiv rate-limits shared IPs (like GitHub's runners), so a 429 is common and
    usually transient. We wait it out with exponential backoff plus jitter.
    """
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=60, headers=UA)
            if r.status_code in (429, 500, 502, 503):
                ra = r.headers.get("Retry-After", "")
                wait = int(ra) if ra.isdigit() else min(90, 15 * (attempt + 1))
                print(f"  arXiv returned {r.status_code}; waiting {wait}s "
                      f"(attempt {attempt + 1}/{retries})")
                time.sleep(wait + random.uniform(0, 4))
                continue
            r.raise_for_status()
            return r.text
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(min(90, 10 * (attempt + 1)) + random.uniform(0, 4))
    if last_err:
        raise last_err
    raise RuntimeError("exhausted retries")


def fetch_recent(category, want=300):
    """Return (feed, source). Tries the API first, then the RSS feed.

    Returns (None, None) if both fail — the caller treats that as a transient
    outage and exits cleanly so the next scheduled run just picks up where this
    left off (nothing is lost thanks to seen.json + the lookback window).
    """
    # small random desync so many GitHub jobs don't hit arXiv the same second
    time.sleep(random.uniform(0, 8))

    params = {
        "search_query": f"cat:{category}",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": want,
    }
    try:
        feed = feedparser.parse(_get(API_URL, params))
        if feed.entries:
            return feed, "api"
        print("  API returned no entries; trying RSS fallback.")
    except Exception as e:  # noqa: BLE001
        print(f"  API fetch failed ({e}); trying RSS fallback.")

    try:
        feed = feedparser.parse(_get(RSS_URL.format(cat=category)))
        if feed.entries:
            return feed, "rss"
        print("  RSS returned no entries.")
    except Exception as e:  # noqa: BLE001
        print(f"  RSS fetch failed ({e}).")

    return None, None


# ── keyword matching ────────────────────────────────────────────────────────
def topic_matches(topic, hay):
    """hay is the lowercased 'title + abstract' string."""
    for rule in topic.get("rules", []):
        groups = rule.get("all_of", [])
        if groups and all(any(term.lower() in hay for term in group) for group in groups):
            return True
    return False


def matched_topics(cfg, title, summary):
    hay = (title + " " + summary).lower()
    return [t["name"] for t in cfg.get("topics", []) if topic_matches(t, hay)]


def _name_tokens(name):
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _token_match(wt, at):
    # exact token, or an initial matching the other's first letter (either way),
    # so "martin" matches "m." and "huei" matches "h." — handles arXiv initials.
    if wt == at:
        return True
    if len(at) == 1 and at == wt[0]:
        return True
    if len(wt) == 1 and wt == at[0]:
        return True
    return False


def matched_authors(cfg, authors):
    """Return watched-author entries that appear among this paper's authors.

    Matching is order-independent and initial-aware: every token of a watchlist
    entry must match some token of one author's name. So "Nelly Ng Huei Ying"
    matches "Nelly Huei Ying Ng", and "Martin Plenio" matches "M. B. Plenio".
    Use a fuller name to disambiguate common surnames.
    """
    watch = cfg.get("authors") or []
    if not watch:
        return []
    author_tokens = [_name_tokens(a) for a in authors]
    hits = []
    for w in watch:
        wts = _name_tokens(str(w))
        if not wts:
            continue
        for ats in author_tokens:
            if not all(any(_token_match(wt, at) for at in ats) for wt in wts):
                continue
            # Guard against all-initials coincidences (e.g. two "Y." initials
            # matching "Yuxiang" and "Yang"): require at least one substantial
            # name part (>= 3 chars) to match a full author token, not just an
            # initial. If the entry is entirely short tokens, require all exact.
            substantial = [wt for wt in wts if len(wt) >= 3]
            if substantial:
                if any(wt in ats for wt in substantial):
                    hits.append(w)
                    break
            elif all(wt in ats for wt in wts):
                hits.append(w)
                break
    return hits


def entry_id(entry):
    # Works for API (id like http://arxiv.org/abs/2401.01234v2) and RSS
    # (id/guid like oai:arXiv.org:2401.01234v1, or the link URL).
    for cand in (getattr(entry, "id", "") or "", getattr(entry, "link", "") or ""):
        m = re.search(r"(\d{4}\.\d{4,5})", cand)
        if m:
            return m.group(1)
    raw = (getattr(entry, "id", "") or "").split("/abs/")[-1]
    return raw.split("v")[0]


def get_authors(entry):
    # API: entry.authors is a list of {'name': ...}. RSS: dc:creator lands in
    # entry.author (and/or entry.authors) as names separated by commas/semicolons.
    if getattr(entry, "authors", None):
        names = [a.get("name", "").strip() for a in entry.authors
                 if isinstance(a, dict) and a.get("name")]
        if len(names) > 1:
            return names
        if len(names) == 1:
            return [n for n in re.split(r"\s*[;,]\s*", names[0]) if n]
    if getattr(entry, "author", None):
        return [n for n in re.split(r"\s*[;,]\s*", entry.author) if n]
    return []


def get_title(entry):
    t = getattr(entry, "title", "") or ""
    # RSS titles may end with " (arXiv:2401.01234v1 [quant-ph])"
    t = re.sub(r"\s*\(arXiv:.*?\)\s*$", "", t)
    return " ".join(t.split())


def get_summary(entry):
    s = getattr(entry, "summary", "") or ""
    # RSS descriptions prepend "arXiv:... Announce Type: ... Abstract: <text>"
    if "Abstract:" in s:
        s = s.split("Abstract:", 1)[1]
    return " ".join(s.split())


# ── email ───────────────────────────────────────────────────────────────────
def build_email_html(cfg, papers):
    cat = cfg.get("category", "quant-ph")
    today = dt.datetime.now().strftime("%A, %d %B %Y")
    parts = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,'
             'sans-serif;max-width:680px;margin:0 auto;color:#111;">']
    parts.append(f'<div style="font-size:18px;font-weight:700;margin:0 0 2px;">'
                 f'arXiv {html.escape(cat)} — daily digest</div>')
    parts.append(f'<div style="font-size:12px;color:#666;margin:0 0 18px;">'
                 f'{today} · {len(papers)} matching paper(s)</div>')
    for p in papers:
        authors = ", ".join(p["authors"][:8]) + (" et al." if len(p["authors"]) > 8 else "")
        tags = " · ".join(html.escape(t) for t in p["topics"])
        parts.append(
            '<div style="margin:0 0 20px;padding:0 0 16px;border-bottom:1px solid #eaeaea;">'
            f'<div style="font-size:15px;font-weight:600;line-height:1.35;">'
            f'<a href="{p["abs"]}" style="color:#0b5cad;text-decoration:none;">'
            f'{html.escape(p["title"])}</a></div>'
            f'<div style="font-size:12px;color:#555;margin:4px 0;">{html.escape(authors)}</div>'
            f'<div style="font-size:11px;color:#9a3412;margin:4px 0;">matched: {tags}</div>'
            f'<div style="font-size:13px;color:#222;line-height:1.45;margin:6px 0;">'
            f'{html.escape(p["summary"])}</div>'
            f'<div style="font-size:12px;"><a href="{p["abs"]}" style="color:#0b5cad;">abstract</a>'
            f' &nbsp;|&nbsp; <a href="{p["pdf"]}" style="color:#0b5cad;">pdf</a>'
            f' &nbsp;·&nbsp; <span style="color:#777;">submitted {p["published"]}</span></div>'
            '</div>'
        )
    parts.append('<div style="font-size:11px;color:#999;margin-top:8px;">'
                 'Generated by your GitHub Actions arXiv alert · edit config.yaml '
                 'to change keywords.</div></div>')
    return "".join(parts)


def build_email_text(papers):
    lines = []
    for p in papers:
        lines.append(p["title"])
        lines.append(p["abs"])
        lines.append("matched: " + ", ".join(p["topics"]))
        lines.append("")
    return "\n".join(lines)


def send_email(cfg, papers, user, password, host, port, mail_to):
    today = dt.datetime.now().strftime("%Y-%m-%d")
    cat = cfg.get("category", "quant-ph")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"[arXiv {cat}] {len(papers)} new paper(s) — {today}"
    msg["From"] = user
    msg["To"] = mail_to
    msg.attach(MIMEText(build_email_text(papers), "plain", "utf-8"))
    msg.attach(MIMEText(build_email_html(cfg, papers), "html", "utf-8"))
    with smtplib.SMTP(host, port, timeout=60) as s:
        s.starttls()
        s.login(user, password)
        s.sendmail(user, [mail_to], msg.as_string())


# ── main ────────────────────────────────────────────────────────────────────
def main():
    cfg = load_config()

    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    mail_to = os.environ.get("MAIL_TO", DEFAULT_TO)
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    if not smtp_user or not smtp_pass:
        sys.exit("ERROR: set the SMTP_USER and SMTP_PASS environment variables "
                 "(GitHub repo secrets).")

    lookback = int(cfg.get("lookback_days", 2))
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=lookback)

    feed, source = fetch_recent(cfg.get("category", "quant-ph"))
    if feed is None:
        # transient outage / rate limit — exit cleanly; next run catches up
        print("Could not reach arXiv this run (likely a temporary rate limit). "
              "Nothing lost — the next scheduled run will pick these up.")
        return
    print(f"Fetched {len(feed.entries)} entries via {source}.")
    seen = load_seen()

    papers = []
    for e in feed.entries:
        pid = entry_id(e)
        if pid in seen:
            continue
        if getattr(e, "published_parsed", None):
            pub = dt.datetime(*e.published_parsed[:6], tzinfo=dt.timezone.utc)
            if pub < cutoff:
                continue
            pub_str = pub.strftime("%Y-%m-%d")
        else:
            pub_str = "?"
        title = get_title(e)
        summary = get_summary(e)
        author_list = get_authors(e)
        topics = matched_topics(cfg, title, summary)
        author_hits = matched_authors(cfg, author_list)
        if not topics and not author_hits:
            continue
        labels = list(topics) + [f"author: {a}" for a in author_hits]
        papers.append({
            "id": pid,
            "title": title,
            "summary": summary,
            "authors": author_list,
            "abs": f"https://arxiv.org/abs/{pid}",
            "pdf": f"https://arxiv.org/pdf/{pid}",
            "published": pub_str,
            "topics": labels,
        })

    if not papers:
        print("No new matching papers today — nothing emailed.")
        return

    papers = papers[: int(cfg.get("max_email", 80))]
    send_email(cfg, papers, smtp_user, smtp_pass, smtp_host, smtp_port, mail_to)

    seen.update(p["id"] for p in papers)
    save_seen(seen)
    print(f"Emailed {len(papers)} paper(s) to {mail_to}.")


if __name__ == "__main__":
    main()
