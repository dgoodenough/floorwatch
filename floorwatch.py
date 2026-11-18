#!/usr/bin/env python3
"""
FloorWatch - a live, plain-English feed of the U.S. House and Senate floor.

Pulls the official feeds (House Clerk floor XML + roll-call XML, Senate
Periodical Press Gallery floor log + Senate roll-call XML), labels every
item as a REAL vote, PROCEDURAL vote, AMENDMENT, DEBATE, COMING UP or
ROUTINE, explains it in one line, and serves an auto-refreshing page.

    python3 floorwatch.py            # then open http://localhost:8765

Standard library only. Optional: set ANTHROPIC_API_KEY to add a short
AI-written "in plain English" line under each significant item.
"""
from __future__ import annotations

import argparse
import hashlib
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover - very old Python / no tzdata
    EASTERN = dt.timezone(dt.timedelta(hours=-5))

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 FloorWatch/1.0")

HOUSE_FLOOR_URL = "https://clerk.house.gov/floor/{ymd}.xml"
HOUSE_ROLL_URL = "https://clerk.house.gov/evs/{year}/roll{num:03d}.xml"
HOUSE_ROLL_PAGE = "https://clerk.house.gov/Votes/{year}{num}"
SENATE_LOG_URL = "https://www.periodicalpress.senate.gov/on-the-floor"
SENATE_MENU_URL = "https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{c}_{s}.xml"
SENATE_VOTE_URL = ("https://www.senate.gov/legislative/LIS/roll_call_votes/"
                   "vote{c}{s}/vote_{c}_{s}_{n:05d}.xml")
SENATE_VOTE_PAGE = ("https://www.senate.gov/legislative/LIS/roll_call_votes/"
                    "vote{c}{s}/vote_{c}_{s}_{n:05d}.htm")


def now_et() -> dt.datetime:
    return dt.datetime.now(EASTERN)


def congress_session(day: dt.date) -> tuple[int, int]:
    # A new Congress starts Jan 3 of odd years; close enough for a reader.
    congress = (day.year - 1789) // 2 + 1
    session = 1 if day.year % 2 == 1 else 2
    return congress, session


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", unescape(s or "")).strip()


# --------------------------------------------------------------------------
# Fetching (with an on-disk cache for things that never change)
# --------------------------------------------------------------------------
class Fetcher:
    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.errors: dict[str, str] = {}
        self.missing: set[str] = set()

    def get(self, url: str, immutable: bool = False, timeout: int = 20, ttl: int | None = None) -> bytes | None:
        key = re.sub(r"[^A-Za-z0-9._-]", "_", url.split("://", 1)[-1])[-180:]
        path = self.cache_dir / key
        if immutable and path.exists():
            return path.read_bytes()
        if ttl and path.exists() and time.time() - path.stat().st_mtime < ttl:
            return path.read_bytes()
        if immutable and url in self.missing:
            return None
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
            self.errors.pop(url, None)
            if immutable or ttl:
                path.write_bytes(data)
            return data
        except urllib.error.HTTPError as e:
            if ttl and path.exists() and time.time() - path.stat().st_mtime < 3 * 3600:
                return path.read_bytes()  # a copy from the last few hours beats nothing
            if e.code == 404:
                if immutable:
                    self.missing.add(url)
            else:
                self.errors[url] = f"HTTP {e.code}"
            return None
        except Exception as e:  # network down, timeout, TLS...
            if ttl and path.exists() and time.time() - path.stat().st_mtime < 3 * 3600:
                return path.read_bytes()
            self.errors[url] = type(e).__name__ + ": " + str(e)[:120]
            return None


# --------------------------------------------------------------------------
# The decoder: what kind of thing is this, and what does it mean?
# --------------------------------------------------------------------------
KINDS = {
    "final":      "REAL VOTE",
    "procedural": "PROCEDURAL",
    "amendment":  "AMENDMENT",
    "debate":     "ON THE FLOOR",
    "schedule":   "COMING UP",
    "routine":    "ROUTINE",
    "info":       "UPDATE",
}

# (regex, kind, label, explanation). First match wins, so order matters.
HOUSE_RULES = [
    (r"motion to reconsider (was )?laid on the table|motion to reconsider.*table", "routine",
     "Housekeeping", "Locks in the result so it can't be reopened. Automatic."),
    (r"previous question", "procedural", "Previous question on the rule",
     "Ends debate on the rule and blocks the minority from offering its own version. "
     "Nearly always party-line. Not a vote on the bill."),
    (r"motion to recommit", "procedural", "Motion to recommit",
     "The minority's last shot: send the bill back to committee. It almost always fails; "
     "final passage comes right after."),
    (r"motion to instruct", "procedural", "Motion to instruct negotiators",
     "Nonbinding advice to the House's negotiators. Mostly a messaging vote."),
    (r"motion to discharge|discharge petition", "procedural", "Discharge",
     "Forces a bill to the floor over leadership's objection. Rare and significant."),
    (r"motion to table|laid on the table", "procedural", "Motion to table",
     "Kills the motion or resolution without a direct vote on it. A 'yes' means kill it."),
    (r"motion to refer", "procedural", "Motion to refer",
     "Sends the matter off to a committee, often a way to avoid a direct vote."),
    (r"motion to adjourn", "procedural", "Motion to adjourn",
     "Usually a delay tactic or protest. Decides nothing about any bill."),
    (r"veto|objections of the president", "final", "Veto override",
     "The real vote: overrides the President's veto. Needs two-thirds."),
    (r"suspend the rules", "final", "Passage (fast-track)",
     "The real vote. Fast-track used for bills with broad support: no amendments, "
     "limited debate, needs two-thirds to pass."),
    (r"conference report", "final", "Conference report",
     "The real vote on the final House-Senate compromise."),
    (r"motion to concur|agree to the senate amendment|concur in the senate amendment", "final",
     "Accept the Senate's version", "The real vote: accepts the Senate's changes. If both "
     "chambers have now agreed, the bill heads to the President."),
    (r"(on agreeing to|amendment).{0,80}amendment|on agreeing to the .*amendment|"
     r"amendment (no\. ?\d+ )?(offered|agreed|failed|not agreed)", "amendment", "Amendment",
     "A vote on a proposed change to the bill, not on the bill itself."),
    (r"on passage|passed|on agreeing to the (resolution|concurrent resolution|joint resolution)|"
     r"motion to suspend", "final", "Passage",
     "The real vote on the measure itself."),
    (r"quorum", "routine", "Quorum call", "Checks that enough members are present."),
    (r"(the chair|speaker).{0,40}postponed|further proceedings.{0,40}postponed|"
     r"proceedings.{0,40}postponed", "schedule", "Vote postponed",
     "A recorded vote was requested but pushed back. It'll happen later, often in a batch."),
    (r"^debate\b|proceeded with (one hour|forty minutes|\d+ minutes|.*of debate)|general debate",
     "debate", "Debate", "Members are speaking for and against. No vote yet."),
    (r"providing for consideration|rule provides|rules committee", "debate", "The rule",
     "This resolution sets the ground rules (time, amendments) for debating another bill."),
    (r"considered under suspension|considered under the provisions of rule|considered as privileged|"
     r"considered as unfinished business|considered as (a )?privileged|"
     r"resolved (itself )?into the committee of the whole|considered by unanimous consent|"
     r"called up|took up|committee of the whole", "debate", "Now considering",
     "The House has taken this up."),
    (r"one minute speeches|one-minute|morning-hour|morning hour|special order|prayer|pledge of allegiance|"
     r"journal|pro tempore|convened|adjourn|recess|message from the senate|message from the president|"
     r"oath of office|chaplain|enrolled|signed by the speaker|executive communication",
     "routine", "Routine", ""),
]

SENATE_RULES = [
    (r"filed cloture", "schedule", "Cloture filed",
     "Sets up a test vote to end debate, by rule two days later unless they agree to go sooner."),
    (r"\bwill (convene|vote|resume|recess|proceed)|votes? (are|is) expected|roll ?call votes? (are )?expected|"
     r"no (roll call )?votes|unless an agreement|at a time to be determined|expected to vote",
     "schedule", "Coming up", "What the Senate has scheduled next."),
    (r"resumed consideration|resume consideration|began consideration|proceeded to (the )?consideration|"
     r"post-cloture|is (now )?considering|morning business", "debate", "On the floor",
     "What the Senate is working on. No vote yet."),
    (r"cloture.{0,80}motion to proceed|motion to proceed.{0,80}cloture", "procedural",
     "Cloture on starting debate",
     "Vote to break a filibuster on whether to even START debating. Needs 60. Passing only "
     "lets work begin."),
    (r"cloture.{0,60}(nomination|nominee|executive calendar|\bpn\d)|(nomination|\bpn\d).{0,80}cloture",
     "procedural", "Cloture on a nominee",
     "Ends debate on the nominee (simple majority). The confirmation vote follows, "
     "usually within two days."),
    (r"cloture.{0,60}(amdt|amendment)", "procedural", "Cloture on an amendment",
     "Ends debate on this amendment. Needs 60 for legislation, so it's a strong signal of "
     "whether the deal has the votes."),
    (r"cloture", "procedural", "Cloture on the bill",
     "The decisive test vote: ends debate on the bill. Needs 60. If it passes, final passage "
     "is very likely."),
    (r"motion to waive|waive .*budget|budget point of order", "procedural", "Waive budget rules",
     "Needs 60. If it fails, the bill or amendment dies on a budget technicality."),
    (r"point of order", "procedural", "Point of order", "A challenge that something breaks Senate rules."),
    (r"motion to table|tabled", "procedural", "Motion to table",
     "A 'yes' kills the amendment or motion without a direct vote on it."),
    (r"motion to discharge", "procedural", "Motion to discharge",
     "Pulls a measure out of committee to the floor. Often used for resolutions of disapproval."),
    (r"motion to instruct", "procedural", "Motion to instruct", "Nonbinding guidance to negotiators."),
    (r"motion to proceed", "procedural", "Motion to proceed",
     "Vote to begin debating the measure. Not passage."),
    (r"veto", "final", "Veto override", "The real vote: overrides the President's veto. Needs two-thirds."),
    (r"on the nomination|\bconfirm(ed|ation)\b|nomination.{0,40}(rejected|withdrawn)", "final",
     "Confirmation", "The real vote: confirms or rejects the nominee."),
    (r"concur", "final", "Accept the House's version",
     "The real vote: accepts the House's changes. If both chambers now agree, it goes to the President."),
    (r"\bamdt\b|amendment", "amendment", "Amendment",
     "A vote on a proposed change, not on the bill itself."),
    (r"on passage|\bpassed\b|on the (joint |concurrent )?resolution|conference report|"
     r"resolution of ratification|\badopted\b|agreed to the resolution", "final", "Passage",
     "The real vote on the measure itself."),
    (r"post-cloture|debate|morning business|speeches|resumed consideration|resume consideration|"
     r"began consideration|proceeded to", "debate", "Debate", "Senators are debating. No vote yet."),
    (r"convened|adjourned|recess|quorum|prayer|pledge|wrap ?up|in session", "routine", "Routine", ""),
]

UC_NOTE = " Done by unanimous consent: no recorded vote, because nobody objected."


def classify(chamber: str, text: str, hint: str = "") -> dict:
    t = norm(f"{text} {hint}").lower()
    rules = HOUSE_RULES if chamber == "house" else SENATE_RULES
    for pattern, kind, label, explain in rules:
        if re.search(pattern, t):
            if kind == "final" and ("unanimous consent" in t or "without objection" in t) \
                    and "roll" not in t and not re.search(r"\d+\s*-\s*\d+", t):
                explain += UC_NOTE
            return {"kind": kind, "badge": KINDS[kind], "label": label, "explain": explain}
    return {"kind": "info", "badge": KINDS["info"], "label": "Update", "explain": ""}


PENDING_BADGE = {"final": "PASSAGE QUESTION", "procedural": "PROCEDURAL MOTION",
                 "amendment": "AMENDMENT OFFERED"}


def mark_pending(it: dict):
    """A motion being *made* (no outcome yet) shouldn't wear a 'vote' badge."""
    if it["kind"] in PENDING_BADGE and not it.get("vote"):
        it["badge"] = PENDING_BADGE[it["kind"]]
        it["pending"] = True


BILL_RE = re.compile(
    r"\b(H\.\s?R\.|H\.\s?Res\.|H\.\s?J\.\s?Res\.|H\.\s?Con\.\s?Res\.|S\.\s?J\.\s?Res\.|"
    r"S\.\s?Con\.\s?Res\.|S\.\s?Res\.|S\.|PN)\s?(\d+)(-\d+)?", re.I)


def bill_refs(text: str) -> list[str]:
    out = []
    for m in BILL_RE.finditer(text or ""):
        p = re.sub(r"\s", "", m.group(1)).upper().replace("RES.", "Res.").replace("CON.", "Con.")
        p = p.replace("H.J.Res.", "H.J.Res.").replace("S.J.Res.", "S.J.Res.")
        ref = f"{p} {m.group(2)}"
        if p == "S." and not re.match(r"S\.\s?\d", m.group(0)):
            continue
        if ref not in out:
            out.append(ref)
    return out


FLOOR_NAME = re.compile(r"\b(H\.\s?R\.|S\.|H\.\s?J\.\s?Res\.|S\.\s?J\.\s?Res\.)\s?(\d+),\s+(?:the\s+)?"
                        r"([A-Z][A-Za-z0-9'’&\- ]{3,90}?\b(?:Act|Resolution)(?: of \d{4})?)")


def floor_names(text: str) -> dict:
    out = {}
    for m in FLOOR_NAME.finditer(text or ""):
        out[canon(m.group(1) + m.group(2))] = norm(m.group(3))
    return out


def canon(ref: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (ref or "").upper())


def rule_target(text: str) -> str | None:
    m = re.search(r"providing for (the )?consideration of (the )?(bill|joint resolution|resolution|"
                  r"concurrent resolution|senate amendment.{0,40}?)\s*\(([^)]+)\)", text or "", re.I)
    if m:
        return norm(m.group(4))
    m = re.search(r"providing for (the )?consideration of [^.;]{0,60}?((H\.\s?R\.|S\.|H\.\s?J\.\s?Res\.)\s?\d+)",
                  text or "", re.I)
    return norm(m.group(2)) if m else None


def vote_outcome(text: str) -> dict | None:
    t = norm(text)
    m = None
    for cand in re.finditer(r"(\d{1,3})\s*-\s*(\d{1,3})(?:\s*\((?:Roll|Record) no\.\s*(\d+)\))?", t, re.I):
        before = t[max(0, cand.start() - 12):cand.start()]
        if re.search(r"(Rept\.|Law|No\.|Res\.|Number:?|Calendar)\s*$", before, re.I):
            continue  # report / law / calendar numbers, not a tally
        m = cand
        break
    roll = re.search(r"Roll no\.\s*(\d+)", t, re.I)
    res = re.search(r"\b(Agreed to|Not Agreed to|Failed|Passed|Confirmed|Rejected|Adopted|Invoked|"
                    r"Not invoked|Sustained|Overridden)\b", t, re.I)
    if not (m or roll or res):
        return None
    out = {"result": res.group(1) if res else ""}
    if m:
        out["yeas"], out["nays"] = int(m.group(1)), int(m.group(2))
    if roll:
        out["roll"] = int(roll.group(1))
    if "by voice vote" in t.lower():
        out["voice"] = True
    return out


# --------------------------------------------------------------------------
# House
# --------------------------------------------------------------------------
def parse_house_time(el_time, day: dt.date) -> dt.datetime:
    fs = el_time.get("for-search") if el_time is not None else None
    if fs:
        try:
            return dt.datetime.strptime(fs[:15], "%Y%m%dT%H:%M:%S").replace(tzinfo=EASTERN)
        except ValueError:
            pass
    txt = norm(el_time.text if el_time is not None else "")
    m = re.match(r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*([AP])\.?M", txt, re.I)
    if m:
        h, mi, s = int(m.group(1)) % 12, int(m.group(2)), int(m.group(3) or 0)
        if m.group(4).upper() == "P":
            h += 12
        return dt.datetime(day.year, day.month, day.day, h, mi, s, tzinfo=EASTERN)
    return dt.datetime(day.year, day.month, day.day, tzinfo=EASTERN)


def parse_house_floor(xml_bytes: bytes, day: dt.date) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = []
    for fa in root.iter("floor_action"):
        desc_el = fa.find("action_description")
        desc = norm("".join(desc_el.itertext())) if desc_el is not None else ""
        bills = [norm("".join(x.itertext())) for x in fa.iter("action_item")]
        ts = parse_house_time(fa.find("action_time"), day)
        uid = fa.get("unique-id") or f"{day}{len(items)}"
        items.append({
            "id": f"h-{day:%Y%m%d}-{uid}",
            "chamber": "house",
            "ts": ts.isoformat(),
            "text": desc,
            "refs": [b for b in bills if b] or bill_refs(desc),
            "act_id": fa.get("act-id", ""),
        })
    return items


def parse_house_roll(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    md = root.find("vote-metadata")
    g = lambda tag: norm(md.findtext(tag) or "") if md is not None else ""
    party = {}
    for tp in root.iter("totals-by-party"):
        name = norm(tp.findtext("party") or "")
        key = {"Republican": "R", "Democratic": "D", "Independent": "I"}.get(name, name[:1])
        party[key] = {"y": int(tp.findtext("yea-total") or 0), "n": int(tp.findtext("nay-total") or 0),
                      "nv": int(tp.findtext("not-voting-total") or 0)}
    tv = root.find(".//totals-by-vote")
    yeas = int(tv.findtext("yea-total") or 0) if tv is not None else sum(p["y"] for p in party.values())
    nays = int(tv.findtext("nay-total") or 0) if tv is not None else sum(p["n"] for p in party.values())
    return {"roll": int(g("rollcall-num") or 0), "measure": g("legis-num"), "question": g("vote-question"),
            "vote_type": g("vote-type"), "result": g("vote-result"), "title": g("vote-desc"),
            "yeas": yeas, "nays": nays, "party": party,
            "date": g("action-date"), "time": g("action-time")}


# --------------------------------------------------------------------------
# Senate
# --------------------------------------------------------------------------
class _LogParser(HTMLParser):
    """Collects headings and paragraph/list text from the Periodical Press floor log."""
    BLOCKS = {"p", "li", "h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self):
        super().__init__()
        self.blocks: list[tuple[str, str]] = []
        self._stack: list[str] = []
        self._buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip += 1
        if tag in self.BLOCKS:
            self._flush()
            self._stack.append(tag)
        if tag == "br":
            self._buf.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "nav", "footer", "header"):
            self._skip = max(0, self._skip - 1)
        if tag in self.BLOCKS:
            self._flush(tag)

    def handle_data(self, data):
        if not self._skip:
            self._buf.append(data)

    def _flush(self, tag=None):
        text = norm("".join(self._buf))
        self._buf = []
        cur = tag or (self._stack[-1] if self._stack else "p")
        if tag and self._stack and self._stack[-1] == tag:
            self._stack.pop()
        if text:
            self.blocks.append((cur, text))


DATE_HEAD = re.compile(r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
                       r"([A-Z][a-z]+ \d{1,2}, \d{4})$")
SENATE_NOISE = re.compile(r"for reference purposes only|latest floor log|^on the floor$|"
                          r"^page \d|^(previous|next)$|congressional record|^\d+$|room s-\d|"
                          r"washington,? d\.?c\.? \d{5}|\(\d{3}\) ?\d{3}-\d{4}|\d{3}-\d{3}-\d{4}|"
                          r"^(follow us|contact|privacy|accessibility|home)\b", re.I)


def parse_senate_log(html_bytes: bytes) -> list[dict]:
    p = _LogParser()
    p.feed(html_bytes.decode("utf-8", "replace"))
    day, out = None, []
    for tag, text in p.blocks:
        m = DATE_HEAD.match(text)
        if m:
            if day is not None and out:
                break  # only the newest day on the page
            day = dt.datetime.strptime(m.group(2), "%B %d, %Y").date()
            continue
        if day is None or tag.startswith("h") or SENATE_NOISE.search(text) or len(text) < 12:
            continue
        out.append({"day": day.isoformat(), "text": text})
    return out


def senate_time_from_text(text: str, day: dt.date) -> dt.datetime | None:
    m = re.search(r"\b(?:at|convened at|adjourned at|recessed at)\s+(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m", text, re.I)
    if not m or re.search(r"\bwill\b", text, re.I):
        return None
    h, mi = int(m.group(1)) % 12, int(m.group(2) or 0)
    if m.group(3).lower() == "p":
        h += 12
    return dt.datetime(day.year, day.month, day.day, h, mi, tzinfo=EASTERN)


def parse_senate_menu(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    year = int(root.findtext("congress_year") or now_et().year)
    out = []
    for v in root.iter("vote"):
        num = int((v.findtext("vote_number") or "0").lstrip("0") or 0)
        q = v.find("question")
        question = norm("".join(q.itertext())) if q is not None else ""
        issue, result = norm(v.findtext("issue") or ""), norm(v.findtext("result") or "")
        eb = v.find("en_bloc")
        if eb is not None:
            ms = list(eb.iter("matter"))
            issue = ", ".join(norm(m.findtext("issue") or "") for m in ms)[:120]
            question = "En bloc: " + (norm(ms[0].findtext("question") or "") if ms else "")
            result = norm(ms[0].findtext("result") or "") if ms else ""
        try:
            day = dt.datetime.strptime(f"{norm(v.findtext('vote_date'))}-{year}", "%d-%b-%Y").date()
        except ValueError:
            continue
        out.append({"num": num, "day": day.isoformat(), "issue": issue, "question": question,
                    "result": result, "title": norm(v.findtext("title") or ""),
                    "yeas": int(v.findtext("vote_tally/yeas") or 0),
                    "nays": int(v.findtext("vote_tally/nays") or 0)})
    return out


def parse_senate_vote(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    g = lambda tag: norm(root.findtext(tag) or "")
    party: dict[str, dict] = {}
    for m in root.iter("member"):
        p = norm(m.findtext("party") or "?")
        cast = norm(m.findtext("vote_cast") or "").lower()
        d = party.setdefault(p, {"y": 0, "n": 0, "nv": 0})
        if cast in ("yea", "guilty"):
            d["y"] += 1
        elif cast in ("nay", "not guilty"):
            d["n"] += 1
        else:
            d["nv"] += 1
    ts = None
    m = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4}),?\s+(\d{1,2}:\d{2}\s*[AP]M)", g("vote_date"))
    if m:
        try:
            ts = dt.datetime.strptime(f"{m.group(1)} {m.group(2)}", "%B %d, %Y %I:%M %p").replace(
                tzinfo=EASTERN).isoformat()
        except ValueError:
            ts = None
    return {"question_text": g("vote_question_text"), "doc_text": g("vote_document_text"),
            "majority": g("majority_requirement"), "result_text": g("vote_result_text"),
            "party": party, "ts": ts}


# --------------------------------------------------------------------------
# Optional AI line (Anthropic API, stdlib HTTP)
# --------------------------------------------------------------------------
class Explainer:
    def __init__(self, cache_path: Path, background: bool = True):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        self.model = os.environ.get("FLOORWATCH_MODEL", "claude-haiku-5-5")
        self.path = cache_path
        self.cache: dict[str, str] = {}
        if cache_path.exists():
            try:
                self.cache = json.loads(cache_path.read_text())
            except Exception:
                self.cache = {}
        self.lock = threading.Lock()
        self.queue: list[tuple[str, str]] = []
        self.enabled = bool(self.key)
        self.last_error = ""
        if self.enabled and background:
            threading.Thread(target=self._worker, daemon=True).start()

    def get(self, item_id: str) -> str:
        return self.cache.get(item_id, "")

    def request(self, item_id: str, prompt: str):
        if not self.enabled or item_id in self.cache:
            return
        with self.lock:
            if all(i != item_id for i, _ in self.queue):
                self.queue.append((item_id, prompt))

    def drain(self, max_items: int = 25, deadline: float = 120):
        """Used by --once: explain up to max_items queued items, then stop."""
        end = time.time() + deadline
        for _ in range(max_items):
            with self.lock:
                job = self.queue.pop(0) if self.queue else None
            if not job or time.time() > end:
                return
            self._call(*job)

    def _worker(self):
        while True:
            with self.lock:
                job = self.queue.pop(0) if self.queue else None
            if not job:
                time.sleep(1)
                continue
            self._call(*job)

    def _call(self, item_id: str, prompt: str):
            try:
                body = json.dumps({"model": self.model, "max_tokens": 160,
                                   "messages": [{"role": "user", "content": prompt}]}).encode()
                req = urllib.request.Request(
                    "https://api.anthropic.com/v1/messages", data=body, method="POST",
                    headers={"x-api-key": self.key, "anthropic-version": "2023-06-01",
                             "content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    resp = json.loads(r.read())
                text = norm("".join(b.get("text", "") for b in resp.get("content", [])))
                with self.lock:
                    self.cache[item_id] = text
                    self.path.write_text(json.dumps(self.cache))
                self.last_error = ""
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:120]}"
                time.sleep(3)
            time.sleep(0.3)


AI_PROMPT = """You explain U.S. congressional floor activity to a smart viewer watching a livestream.
Chamber: {chamber}
Official text: {text}
{context}Our rule-based label: {label} - {explain}

In ONE sentence of at most 30 words, say in plain English what is actually being decided or done
right now and what it means for the underlying bill or nominee (e.g. what a "yes" does).
Be strictly neutral: no opinions on the policy or parties. If unsure what the measure is, say so
briefly rather than guess. Output only the sentence."""


# --------------------------------------------------------------------------
# The engine: refresh everything, build the feed
# --------------------------------------------------------------------------
class Engine:
    def __init__(self, data_dir: Path, days_back: int, fixed_date: dt.date | None, background_ai: bool = True):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.f = Fetcher(data_dir / "cache")
        self.state_path = data_dir / "state.json"
        self.state = {"senate_seen": {}}
        if self.state_path.exists():
            try:
                self.state = json.loads(self.state_path.read_text())
            except Exception:
                pass
        self.ai = Explainer(data_dir / "ai_cache.json", background_ai)
        self.days_back = days_back
        self.fixed_date = fixed_date
        self.feed: dict = {"house": [], "senate": [], "votes": [], "status": {}}
        self.lock = threading.Lock()
        self.titles: dict[str, str] = {}   # "H.R. 1234" -> title
        self.rules: dict[str, str] = {}    # "HRES900" -> "H.R. 1234"
        self.bills: dict[str, dict] = {}   # "HR1234" -> full bill page data
        self.live_path = data_dir / "bill_live.json"
        self.bill_live: dict[str, dict] = {}
        if self.live_path.exists():
            try:
                self.bill_live = json.loads(self.live_path.read_text())
            except Exception:
                self.bill_live = {}

    # ---- House ------------------------------------------------------------
    def house(self) -> tuple[list[dict], str | None]:
        today = self.fixed_date or now_et().date()
        days_found, items = [], []
        for back in range(0, 21):
            d = today - dt.timedelta(days=back)
            data = self.f.get(HOUSE_FLOOR_URL.format(ymd=f"{d:%Y%m%d}"), immutable=(back > 1))
            if not data:
                continue
            try:
                day_items = parse_house_floor(data, d)
            except ET.ParseError:
                continue
            if not day_items:
                continue
            days_found.append(d)
            items.extend(day_items)
            if len(days_found) >= self.days_back:
                break
        latest = max(days_found).isoformat() if days_found else None

        # Learn which H.Res. are "rules" and for what.
        for it in items:
            tgt = rule_target(it["text"])
            if tgt:
                for r in bill_refs(it["text"]):
                    if canon(r).startswith("HRES"):
                        self.rules[canon(r)] = tgt

        # Fetch the roll calls referenced in the log (in parallel, cached forever).
        rolls = {}
        wanted = []
        for it in items:
            out = vote_outcome(it["text"])
            it["vote"] = out
            if out and out.get("roll"):
                wanted.append((it["ts"][:4], out["roll"]))
        with cf.ThreadPoolExecutor(6) as ex:
            futs = {ex.submit(self.f.get, HOUSE_ROLL_URL.format(year=y, num=n), True): (y, n)
                    for y, n in set(wanted)}
            for fu in futs:
                data = fu.result()
                if data:
                    try:
                        rolls[futs[fu]] = parse_house_roll(data)
                    except ET.ParseError:
                        pass

        for it in items:
            hint = ""
            v = it.get("vote")
            roll = rolls.get((it["ts"][:4], v["roll"])) if v and v.get("roll") else None
            if roll:
                v.update({k: roll[k] for k in ("party", "question", "title", "measure", "yeas", "nays")})
                v["result"] = roll["result"] or v.get("result", "")
                v["url"] = HOUSE_ROLL_PAGE.format(year=it["ts"][:4], num=roll["roll"])
                if roll["measure"]:
                    it["refs"] = [roll["measure"]] + [r for r in it["refs"] if r != roll["measure"]]
                    # Only the bill's own votes carry the bill's title (not amendments or rules).
                    if roll["title"] and not re.search(r"amendment|providing for consideration",
                                                       roll["title"] + roll["question"], re.I):
                        self.titles.setdefault(canon(roll["measure"]), roll["title"])
                hint = roll["question"]
            it.update(classify("house", it["text"], hint))
            # A vote "on agreeing to the resolution" about a RULE is procedural, not final.
            target = None
            for r in it["refs"]:
                if canon(r) in self.rules:
                    target = self.rules[canon(r)]
            if not target and roll:
                target = rule_target(roll["title"])
            if target and it["kind"] == "final":
                it.update({"kind": "procedural", "badge": KINDS["procedural"], "label": "Adopting the rule",
                           "explain": f"Approves the ground rules for debating {target}. Not the vote on "
                                      f"{target} itself; that comes later."})
            if target and it["kind"] in ("procedural", "debate") and "previous question" in it["text"].lower():
                it["explain"] = it["explain"].replace("Not a vote on the bill.",
                                                      f"Not a vote on {target} itself.")
            if target:
                it["about"] = target
            mark_pending(it)
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items, latest

    # ---- Senate -----------------------------------------------------------
    def senate(self) -> tuple[list[dict], list[dict], str | None]:
        today = self.fixed_date or now_et().date()
        c, s = congress_session(today)
        items: list[dict] = []
        seen = self.state.setdefault("senate_seen", {})
        log_day = None

        # The press gallery page sometimes serves an older day, and senate.gov sometimes refuses
        # requests. So keep every entry ever seen, per day, and never move backwards.
        store = self.state.setdefault("senate_log", {})
        data = self.f.get(SENATE_LOG_URL, ttl=45)
        if data:
            for e in parse_senate_log(data):
                lst = store.setdefault(e["day"], [])
                if e["text"] not in lst:
                    lst.append(e["text"])
        for old in sorted(store)[:-10]:
            del store[old]
        if store:
            log_day = max(store)
            entries = [{"day": log_day, "text": t} for t in store[log_day]]
            prior = [d for d in sorted(store) if d < log_day]
            if prior and not any(classify("senate", t)["kind"] == "schedule" for t in store[log_day]):
                carry = []
                for t in store[prior[-1]]:
                    if classify("senate", t)["kind"] == "schedule" and "will convene" not in t.lower() \
                            or (carry and re.match(r"^(Motion|Adoption|Passage|Confirmation|Cloture)", t)):
                        carry.append(t)
                self.carryover = [{"day": prior[-1], "text": t} for t in carry]
            else:
                self.carryover = []
            first_poll = self.state.setdefault("senate_first_poll", {})
            prev = None
            for i, e in enumerate(entries):
                d = dt.date.fromisoformat(e["day"])
                first_poll.setdefault(e["day"], now_et().isoformat())
                key = f"s-log-{e['day']}-" + hashlib.sha1(e["text"].encode()).hexdigest()[:10]
                if key not in seen:
                    seen[key] = now_et().isoformat()
                ts = senate_time_from_text(e["text"], d)
                approx = ts is None
                if ts is None:
                    # The log has no timestamps. Entries already there when we first looked get
                    # slotted in page order; later ones get the time we first saw them.
                    first = dt.datetime.fromisoformat(seen[key])
                    backfill = first <= dt.datetime.fromisoformat(first_poll[e["day"]]) + dt.timedelta(seconds=90)
                    base = prev or dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=EASTERN)
                    ts = base + dt.timedelta(minutes=1) if (backfill or first.date() != d) else \
                        max(first, base + dt.timedelta(seconds=1))
                prev = ts
                it = {"id": key, "chamber": "senate", "ts": ts.isoformat(), "approx": approx,
                      "text": e["text"], "refs": bill_refs(e["text"]), "vote": vote_outcome(e["text"])}
                it.update(classify("senate", e["text"]))
                mark_pending(it)
                items.append(it)

        # Roll-call votes for the whole session (one file), detail for recent ones.
        votes = []
        menu = self.f.get(SENATE_MENU_URL.format(c=c, s=s), ttl=45)
        recent_cut = (today - dt.timedelta(days=max(7, self.days_back * 2))).isoformat()
        if menu:
            try:
                menu_votes = parse_senate_menu(menu)
            except ET.ParseError:
                menu_votes = []
            detail = {}
            recent = [v for v in menu_votes if v["day"] >= recent_cut]
            with cf.ThreadPoolExecutor(6) as ex:
                # A recorded vote never changes, so each one is downloaded once.
                futs = {ex.submit(self.f.get, SENATE_VOTE_URL.format(c=c, s=s, n=v["num"]), True): v["num"]
                        for v in recent}
                for fu in futs:
                    d = fu.result()
                    if d:
                        try:
                            detail[futs[fu]] = parse_senate_vote(d)
                        except ET.ParseError:
                            pass
            vote_days = [v["day"] for v in menu_votes if v["day"] <= today.isoformat()]
            show_day = max([d for d in (log_day, max(vote_days, default=None)) if d], default=today.isoformat())
            if log_day and show_day > log_day:
                items = []  # the log hasn't caught up to today's votes yet
            for v in menu_votes:
                det = detail.get(v["num"], {})
                text = v["title"] or v["question"]
                cls = classify("senate", f"{v['question']} {v['issue']}", det.get("question_text", ""))
                ts = det.get("ts") or f"{v['day']}T12:00:{v['num'] % 60:02d}-05:00"
                vote = {"result": v["result"], "yeas": v["yeas"], "nays": v["nays"], "num": v["num"],
                        "question": v["question"], "title": v["title"], "measure": v["issue"],
                        "party": det.get("party", {}), "majority": det.get("majority", ""),
                        "url": SENATE_VOTE_PAGE.format(c=c, s=s, n=v["num"])}
                row = {"id": f"s-vote-{c}-{s}-{v['num']}", "chamber": "senate", "ts": ts,
                       "approx": not det.get("ts"), "text": text, "refs": bill_refs(v["issue"]) or [v["issue"]],
                       "vote": vote, "is_roll": True, **cls}
                if det.get("majority") and det["majority"] not in ("1/2",) \
                        and not re.search(r"60|two-thirds", row["explain"]):
                    row["explain"] = (row["explain"] + f" Needs {det['majority']}.").strip()
                votes.append(row)
                if v["day"] >= show_day:
                    items.append(row)
            log_day = show_day
        self.state_path.write_text(json.dumps(self.state))
        tallies = {(r["ts"][:10], r["vote"]["yeas"], r["vote"]["nays"]) for r in votes}
        items = [i for i in items if i.get("is_roll") or not (
            i.get("vote") and (i["ts"][:10], i["vote"].get("yeas"), i["vote"].get("nays")) in tallies)]
        done_text = " ".join((i.get("text", "") + " " + ((i.get("vote") or {}).get("question") or "")).lower()
                             for i in items if i.get("vote"))
        steps = [("waive", r"waive"), ("adoption", r"adoption of|on the amendment|amdt"), ("cloture", r"cloture"),
                 ("passage", r"passage|on passage"), ("confirmation", r"confirm")]
        for i, e in enumerate(getattr(self, "carryover", [])):
            d = dt.date.fromisoformat(log_day) if log_day else today
            low = e["text"].lower()
            done = any(k in low[:40] and re.search(p, done_text) for k, p in steps) and not low.startswith("at ")
            it = {"id": "s-carry-" + hashlib.sha1(e["text"].encode()).hexdigest()[:10], "chamber": "senate",
                  "ts": dt.datetime(d.year, d.month, d.day, 0, 0, i, tzinfo=EASTERN).isoformat(),
                  "approx": True, "carry": True, "done": done, "text": e["text"], "refs": bill_refs(e["text"]), "vote": None,
                  "kind": "schedule", "badge": KINDS["schedule"], "label": "Expected today",
                  "explain": "From the end of the previous day's Senate log."}
            items.append(it)
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items, votes, log_day


    # ---- Bills ------------------------------------------------------------
    def build_bills(self, items: list[dict]) -> dict:
        today = self.fixed_date or now_et().date()
        c, _ = congress_session(today)
        recent_cut = (today - dt.timedelta(days=14)).isoformat()
        targets: dict[str, set] = {}
        for it in items:
            keys = {canon(r) for r in it.get("refs", [])}
            if (it.get("vote") or {}).get("measure"):
                keys.add(canon(it["vote"]["measure"]))
            if it.get("about"):
                keys.add(canon(it["about"]))
            keys = {k for k in keys if BILL_CANON.match(k)}
            it["bill_keys"] = sorted(keys)
            for k in keys:
                targets.setdefault(k, set())
                # Only floor items go into the bill's live timeline (the votes table has the whole session).
                if it["ts"][:10] >= recent_cut:
                    slim = {f: it[f] for f in SLIM if f in it}
                    self.bill_live.setdefault(k, {})[it["id"]] = slim
        for k in list(self.bill_live):
            if len(self.bill_live[k]) > 400:
                keep = sorted(self.bill_live[k].values(), key=lambda x: x["ts"])[-400:]
                self.bill_live[k] = {x["id"]: x for x in keep}
        self.live_path.write_text(json.dumps(self.bill_live))

        wanted = set(targets) | set(self.bill_live)
        infos: dict[str, dict] = {}

        def load(k):
            m = BILL_CANON.match(k)
            data = self.f.get(BILLSTATUS_URL.format(c=c, t=m.group(1).lower(), n=m.group(2)), ttl=3 * 3600)
            if not data:
                return k, {}
            try:
                return k, parse_billstatus(data)
            except ET.ParseError:
                return k, {}
        with cf.ThreadPoolExecutor(8) as ex:
            for k, info in ex.map(load, sorted(wanted)):
                infos[k] = info
        # The fetch errors for bills that aren't in GovInfo yet aren't worth showing.
        for url in [u for u in self.f.errors if "govinfo.gov" in u]:
            self.f.errors.pop(url, None)

        # Rules (H.Res.) point at the bill they set up; fold their history into that bill.
        rule_of: dict[str, str] = {}
        for k, info in infos.items():
            tgt = rule_target(info.get("official", "") or info.get("display", ""))
            if k.startswith("HRES") and tgt:
                rule_of[k] = canon(tgt)
        for k, tgt in list(self.rules.items()):
            rule_of.setdefault(k, canon(tgt))

        fnames = self.state.setdefault("floor_names", {})
        for it in items:
            for k, n in floor_names(it.get("text", "")).items():
                fnames[k] = n
        names, aka = {}, {}
        for k in wanted:
            info = infos.get(k, {})
            name = info.get("short") or plain_name(info.get("display") or info.get("official") or "")
            if fnames.get(k) and fnames[k].lower() != (name or "").lower():
                # The floor uses the name the bill goes by now (e.g. a shell bill carrying a new text).
                if name:
                    aka[k] = name
                name = fnames[k]
            if not name:
                live_titles = [x.get("title") for x in self.bill_live.get(k, {}).values() if x.get("title")]
                name = live_titles[-1] if live_titles else ""
            names[k] = name
        for k, tgt in rule_of.items():
            if k in names:
                names[k] = f"Rule for {bill_display(tgt)}" + (f" ({names[tgt]})" if names.get(tgt) else "")

        bills = {}
        for k in wanted:
            info = infos.get(k, {})
            live = list(self.bill_live.get(k, {}).values())
            for rk, tgt in rule_of.items():
                if tgt == k:
                    live += list(self.bill_live.get(rk, {}).values())
            live_keys = set()
            for x in live:
                v = x.get("vote") or {}
                if v.get("roll"):
                    live_keys.add(("H", v["roll"]))
                if v.get("num"):
                    live_keys.add(("S", v["num"]))
                if x["chamber"] == "house":
                    live_keys.add(("T", x["ts"][:19]))
            hist = billstatus_items(k, info)
            for rk, tgt in rule_of.items():
                if tgt == k and rk in infos:
                    hist += [dict(h, about=bill_display(rk) + " (the rule for this bill)")
                             for h in billstatus_items(rk, infos[rk]) if h["kind"] not in ("stage",)]
            for h in hist:
                if h.get("vote_key") in live_keys or ("T", h["ts"][:19]) in live_keys:
                    continue
                live.append(h)
            uniq = {x["id"]: x for x in live}
            timeline = sorted(uniq.values(), key=lambda x: x["ts"], reverse=True)
            for x in timeline:
                x.pop("vote_key", None)

            m = BILL_CANON.match(k)
            cg = f"https://www.congress.gov/bill/{c}th-congress/{CG_TYPE[m.group(1)]}/{m.group(2)}"
            links = [{"title": "Congress.gov: status, text, cosponsors", "url": cg, "kind": "Official"},
                     {"title": "Bill text", "url": cg + "/text", "kind": "Official"},
                     {"title": "GovTrack: plain-language overview and prognosis",
                      "url": f"https://www.govtrack.us/congress/bills/{c}/{k.lower()}", "kind": "Analysis"}]
            links += [dict(x) for x in info.get("cbo", [])]
            links += [r for r in (report_link(x) for x in info.get("reports", [])) if r]
            if k.startswith("HRES") and k in rule_of:
                links.append({"title": f"This is the rule for {bill_display(rule_of[k])}", "url": f"#bill-{rule_of[k].lower()}",
                              "kind": "Related"})

            news = []
            active = any(x["ts"][:10] >= recent_cut for x in timeline)
            if active and not k.startswith("HRES"):
                q = f'"{bill_display(k)}"'
                if names[k] and len(names[k]) < 90 and not names[k].lower().startswith(("to ", "a bill", "providing")):
                    q += f' OR "{names[k]}"'
                rss = self.f.get(NEWS_URL.format(q=urllib.parse.quote(q + " when:30d")), ttl=2 * 3600)
                news = parse_news(rss) if rss else []
                self.f.errors.pop(NEWS_URL.format(q=urllib.parse.quote(q + " when:30d")), None)

            bills[k] = {"key": k, "ref": bill_display(k), "name": names[k], "aka": aka.get(k, ""), "congress": c,
                        "official": info.get("official", ""), "sponsor": info.get("sponsor", ""),
                        "policy": info.get("policy", ""), "introduced": info.get("introduced", ""),
                        "summary": info.get("summary", "")[:1500], "links": links, "news": news,
                        "rule_for": bill_display(rule_of[k]) if k in rule_of else "",
                        "timeline": timeline, "history_loaded": bool(info)}
        self.bills = bills
        return {k: {"ref": b["ref"], "name": b["name"]} for k, b in bills.items()}

    # ---- Assemble ---------------------------------------------------------
    def now_line(self, items: list[dict]) -> dict:
        out = self._now(items)
        nxt = next((i for i in items[:12] if i["kind"] == "schedule"), None)
        if nxt and nxt["text"] not in out["text"] and nxt["ts"] >= out.get("ts", ""):
            out["next"] = nxt["text"]
        return out

    def _now(self, items: list[dict]) -> dict:
        if not items:
            return {"text": "No floor activity found yet.", "kind": "routine"}
        latest = items[0]
        low = latest["text"].lower()
        if re.search(r"do now recess|\brecessed\b|stands in recess", low):
            return {"text": "In recess. " + latest["text"], "kind": "routine", "ts": latest["ts"]}
        if re.search(r"\badjourn(ed)?\b", low) and "motion to adjourn" not in low:
            sched = next((i for i in items if i["kind"] == "schedule"), None)
            return {"text": "Adjourned. " + (sched["text"] if sched else latest["text"]), "kind": "routine"}
        for it in items[:15]:
            if it["kind"] in ("debate", "final", "procedural", "amendment"):
                return {"text": it["text"], "kind": it["kind"], "label": it["label"], "explain": it["explain"],
                        "about": it.get("about", ""), "ts": it["ts"], "bill_keys": it.get("bill_keys", []),
                        "vote": it.get("vote"), "pending": it.get("pending", False)}
        return {"text": latest["text"], "kind": latest["kind"]}

    def refresh(self):
        started = time.time()
        try:
            house, h_day = self.house()
        except Exception:
            traceback.print_exc()
            house, h_day = self.feed.get("house", []), None
        try:
            senate, s_votes, s_day = self.senate()
        except Exception:
            traceback.print_exc()
            senate, s_votes, s_day = self.feed.get("senate", []), [], None

        all_items = house + senate + s_votes
        for it in all_items:
            it["title"] = ((it.get("vote") or {}).get("title", "") or "") if it["chamber"] == "house" else ""
            for r in ([] if it["title"] else it.get("refs", [])):
                t = self.titles.get(canon(r))
                if t:
                    it["title"] = t
                    break
            if it["kind"] in ("final", "procedural", "amendment", "debate"):
                ctx = f"Measure title: {it['title']}\n" if it["title"] else ""
                if it.get("about"):
                    ctx += f"This is a procedural step about: {it['about']}\n"
                self.ai.request(it["id"], AI_PROMPT.format(chamber=it["chamber"].title(), text=it["text"],
                                                          context=ctx, label=it["label"],
                                                          explain=it["explain"]))
            it["ai"] = self.ai.get(it["id"])

        house_votes = [dict(i, is_roll=True) for i in house if i.get("vote") and i["vote"].get("roll")]
        votes = sorted(house_votes + s_votes, key=lambda x: x["ts"], reverse=True)
        try:
            bill_index = self.build_bills(house + senate + votes)
        except Exception:
            traceback.print_exc()
            bill_index = {}
        for it in votes:
            it.setdefault("bill_keys", sorted({canon(r) for r in it.get("refs", []) if BILL_CANON.match(canon(r))}))
        h_feed = [i for i in house if i["ts"][:10] == h_day] if h_day else []
        s_feed = senate
        status = {
            "updated": now_et().isoformat(), "took": round(time.time() - started, 1),
            "house_day": h_day, "senate_day": s_day,
            "errors": list(self.f.errors.items())[:6],
            "ai": "on" if self.ai.enabled else "off", "ai_error": self.ai.last_error,
            "now": {"house": self.now_line(h_feed), "senate": self.now_line(s_feed)},
        }
        with self.lock:
            self.feed = {"house": h_feed, "senate": s_feed, "votes": votes, "bills": bill_index, "status": status}

    def apply_ai(self):
        with self.lock:
            for key in ("house", "senate", "votes"):
                for it in self.feed.get(key, []):
                    it["ai"] = self.ai.get(it["id"]) or it.get("ai", "")

    def loop(self, every: int):
        while True:
            self.refresh()
            time.sleep(every)



# --------------------------------------------------------------------------
# Bills: names, history (GovInfo BILLSTATUS), links, news
# --------------------------------------------------------------------------
BILLSTATUS_URL = "https://www.govinfo.gov/bulkdata/BILLSTATUS/{c}/{t}/BILLSTATUS-{c}{t}{n}.xml"
NEWS_URL = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
BILL_CANON = re.compile(r"^(HR|S|HRES|SRES|HJRES|SJRES|HCONRES|SCONRES)(\d+)$")
CG_TYPE = {"HR": "house-bill", "S": "senate-bill", "HRES": "house-resolution", "SRES": "senate-resolution",
           "HJRES": "house-joint-resolution", "SJRES": "senate-joint-resolution",
           "HCONRES": "house-concurrent-resolution", "SCONRES": "senate-concurrent-resolution"}
DISPLAY = {"HR": "H.R.", "S": "S.", "HRES": "H.Res.", "SRES": "S.Res.", "HJRES": "H.J.Res.",
           "SJRES": "S.J.Res.", "HCONRES": "H.Con.Res.", "SCONRES": "S.Con.Res."}
STAGE = {"IntroReferral": ("INTRODUCED", "Introduced / sent to committee"),
         "Committee": ("COMMITTEE", "Committee action"),
         "Calendars": ("CALENDAR", "Placed on the calendar"),
         "President": ("PRESIDENT", "President"),
         "BecameLaw": ("LAW", "Became law")}
SLIM = ("id", "chamber", "ts", "approx", "text", "kind", "badge", "label", "explain", "ai", "vote", "title",
        "refs", "about", "pending", "is_roll", "dateonly", "source")


def bill_display(canon_ref: str) -> str:
    m = BILL_CANON.match(canon_ref)
    return f"{DISPLAY[m.group(1)]} {m.group(2)}" if m else canon_ref


def plain_name(title: str) -> str:
    """Turn long formal titles into something readable."""
    t = norm(title).rstrip(".")
    m = re.search(r"congressional disapproval .*?of the rule submitted by (the )?(.+?) relating to [\"“]?(.+?)[\"”]?$", t, re.I)
    if m:
        agency = re.sub(r"^(Department|Bureau|Office) of (the )?", "", m.group(2)).strip()
        return f"Overturn {agency} rule: " + re.sub(r'["“”]', "", m.group(3)).strip()
    t = re.sub(r",? and for other purposes$", "", t, flags=re.I)
    t = re.sub(r"^(A bill|A joint resolution|A resolution|A concurrent resolution)\s+(to\s+)?", "", t, flags=re.I)
    t = re.sub(r"^To\s+", "", t)
    t = t.replace("United States", "U.S.")
    return t[:1].upper() + t[1:] if t else t


def strip_html(h: str) -> str:
    return norm(re.sub(r"<[^>]+>", " ", h or ""))


def parse_billstatus(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)
    b = root.find("bill")
    if b is None:
        return {}
    g = lambda path: norm(b.findtext(path) or "")
    titles = []
    for it in b.findall("titles/item"):
        titles.append((norm(it.findtext("titleType") or ""), norm(it.findtext("title") or "")))
    short = ""
    for pref in ("Short Title(s) as Enacted", "Short Title(s) as Passed", "Short Title(s) as Reported",
                 "Short Title(s) as Introduced", "Short Title"):
        short = next((t for tt, t in titles if tt.startswith(pref) and t), "")
        if short:
            break
    display = next((t for tt, t in titles if tt == "Display Title" and t), "")
    official = g("title") or next((t for tt, t in titles if tt.startswith("Official Title") and t), "")
    sp = b.find("sponsors/item")
    sponsor = norm(sp.findtext("fullName") or "") if sp is not None else ""
    sums = b.findall("summaries/summary") or b.findall("summaries/item")
    summary = ""
    if sums:
        latest = max(sums, key=lambda x: x.findtext("actionDate") or "")
        summary = strip_html(latest.findtext("text") or "")
    actions = []
    for it in b.findall("actions/item"):
        src = it.find("sourceSystem")
        rv = []
        for v in it.findall("recordedVotes/recordedVote"):
            rv.append({"roll": int(v.findtext("rollNumber") or 0), "url": norm(v.findtext("url") or ""),
                       "chamber": norm(v.findtext("chamber") or "")})
        actions.append({"date": norm(it.findtext("actionDate") or ""), "time": norm(it.findtext("actionTime") or ""),
                        "text": norm(it.findtext("text") or ""), "type": norm(it.findtext("type") or ""),
                        "code": norm(it.findtext("actionCode") or ""),
                        "src_code": norm(src.findtext("code") or "") if src is not None else "",
                        "src": norm(src.findtext("name") or "") if src is not None else "", "votes": rv})
    cbo = [{"title": norm(c.findtext("title") or "CBO cost estimate"), "url": norm(c.findtext("url") or ""),
            "date": norm(c.findtext("pubDate") or "")[:10], "kind": "CBO cost estimate"}
           for c in b.findall("cboCostEstimates/item") if c.findtext("url")]
    reports = [norm(r.findtext("citation") or "") for r in b.findall("committeeReports/item")]
    return {"congress": g("congress"), "type": (g("type") or g("billType")).upper(), "number": g("number"),
            "short": short, "display": display, "official": official, "sponsor": sponsor,
            "policy": norm(b.findtext("policyArea/name") or ""), "introduced": g("introducedDate"),
            "summary": summary, "actions": actions, "cbo": cbo, "reports": [r for r in reports if r]}


def report_link(citation: str) -> dict | None:
    m = re.match(r"([HS])\. ?Rept\. ?(\d+)-(\d+)", citation)
    if not m:
        return None
    kind = "house-report" if m.group(1) == "H" else "senate-report"
    return {"title": f"Committee report: {citation}", "kind": "Committee report",
            "url": f"https://www.congress.gov/congressional-report/{m.group(2)}th-congress/{kind}/{m.group(3)}"}


def parse_news(rss: bytes, limit: int = 8) -> list[dict]:
    out = []
    try:
        root = ET.fromstring(rss)
    except ET.ParseError:
        return out
    for it in root.iter("item"):
        title = norm(it.findtext("title") or "")
        source = norm(it.findtext("source") or "")
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        date = ""
        try:
            date = dt.datetime.strptime(norm(it.findtext("pubDate") or ""), "%a, %d %b %Y %H:%M:%S %Z").date().isoformat()
        except ValueError:
            pass
        out.append({"title": title, "url": norm(it.findtext("link") or ""), "source": source, "date": date})
        if len(out) >= limit:
            break
    return out


def billstatus_items(canon_ref: str, info: dict, about: str = "") -> list[dict]:
    acts = info.get("actions", [])
    nonloc_floor_days = {a["date"] for a in acts if a["type"] in ("Floor", "ResolvingDifferences", "Veto")
                         and a["src_code"] != "9" and a["src"] != "Library of Congress"}
    seen, out = set(), []
    for i, a in enumerate(acts):
        loc = a["src_code"] == "9" or a["src"] == "Library of Congress"
        if loc and a["type"] in ("Floor", "ResolvingDifferences", "Veto") and a["date"] in nonloc_floor_days:
            continue
        if loc and a["type"] == "IntroReferral" and any(
                x["type"] == "IntroReferral" and x["date"] == a["date"] and x is not a for x in acts):
            continue
        key = (a["date"], a["text"][:80].lower())
        if key in seen or not a["date"]:
            continue
        seen.add(key)
        chamber = "house" if "House" in a["src"] or a["code"].startswith("H") else \
            "senate" if "Senate" in a["src"] else "congress"
        ts = f"{a['date']}T{a['time'] or '12:00:00'}-05:00"
        it = {"id": f"bs-{canon_ref}-{i}", "chamber": chamber, "ts": ts, "dateonly": not a["time"],
              "text": a["text"], "refs": [], "source": "Congress.gov"}
        if a["type"] in STAGE:
            badge, label = STAGE[a["type"]]
            kind = "final" if a["type"] == "BecameLaw" or "became public law" in a["text"].lower() else "stage"
            it.update({"kind": kind, "badge": "LAW" if kind == "final" else badge, "label": label, "explain": ""})
        else:
            it.update(classify("house" if chamber == "house" else "senate", a["text"]))
        out_v = vote_outcome(a["text"])
        if a["votes"]:
            v0 = a["votes"][0]
            out_v = out_v or {}
            out_v.update({"url": v0["url"], ("roll" if v0["chamber"] == "House" else "num"): v0["roll"]})
            it["vote_key"] = ("H" if v0["chamber"] == "House" else "S", v0["roll"])
        it["vote"] = out_v
        if about:
            it["about"] = about
        mark_pending(it)
        out.append(it)
    return out


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------
def make_handler(engine: Engine, page: bytes):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path.startswith("/api/bill/"):
                key = self.path.rsplit("/", 1)[-1].split("?")[0].upper()
                b = engine.bills.get(key)
                self._send(200 if b else 404, json.dumps(b or {}).encode(), "application/json")
            elif self.path.startswith("/api/feed"):
                with engine.lock:
                    body = json.dumps(engine.feed).encode()
                self._send(200, body, "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain")
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Live plain-English feed of the House and Senate floor.")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to open it from your phone on the same Wi-Fi")
    ap.add_argument("--every", type=int, default=60, help="seconds between refreshes (default 60)")
    ap.add_argument("--days", type=int, default=3, help="House legislative days to load for the votes table")
    ap.add_argument("--date", help="pretend today is YYYY-MM-DD (replay a past day)")
    ap.add_argument("--data", default=str(Path(__file__).with_name("floorwatch_data")))
    ap.add_argument("--once", action="store_true", help="refresh once, write feed.json to --out, exit")
    ap.add_argument("--out", default=".", help="folder for feed.json in --once mode")
    args = ap.parse_args(argv)

    fixed = dt.date.fromisoformat(args.date) if args.date else None
    engine = Engine(Path(args.data), args.days, fixed, background_ai=not args.once)
    if args.once:
        engine.refresh()
        engine.ai.drain()
        engine.apply_ai()
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "feed.json").write_text(json.dumps(engine.feed))
        bdir = out / "bills"
        bdir.mkdir(exist_ok=True)
        for old in bdir.glob("*.json"):
            if old.stem.upper() not in engine.bills:
                old.unlink()
        for k, b in engine.bills.items():
            (bdir / f"{k.lower()}.json").write_text(json.dumps(b))
        st = engine.feed["status"]
        print(f"house={st['house_day']} senate={st['senate_day']} items={len(engine.feed['house'])}+"
              f"{len(engine.feed['senate'])} votes={len(engine.feed['votes'])} errors={st['errors']}")
        return 0
    here = Path(__file__).parent
    page_path = here / "docs" / "index.html"
    page = (page_path if page_path.exists() else here / "floorwatch.html").read_bytes()
    print("FloorWatch: loading the House and Senate feeds (first load takes ~10-30s)...")
    engine.refresh()
    threading.Thread(target=engine.loop, args=(args.every,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(engine, page))
    st = engine.feed["status"]
    print(f"  House day: {st['house_day']}   Senate day: {st['senate_day']}   AI: {st['ai']}")
    for url, err in st["errors"]:
        print(f"  ! {err}  {url}")
    print(f"Open http://localhost:{args.port}   (Ctrl+C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    sys.exit(main())
