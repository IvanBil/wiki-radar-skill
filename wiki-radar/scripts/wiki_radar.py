#!/usr/bin/env python3
"""Wikipedia pageviews tool for Claude Desktop. Python 3 standard library only."""

from __future__ import annotations

import argparse
import calendar
import gzip
import hashlib
import json
import math
import re
import sqlite3
import ssl
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from pathlib import Path

from pdf_page import Font, write_pdf

ROOT = Path(__file__).resolve().parent.parent
FONT_REGULAR = ROOT / "assets" / "fonts" / "NotoSans-Regular.ttf"
FONT_BOLD = ROOT / "assets" / "fonts" / "NotoSans-Bold.ttf"
DEFAULT_UA = "WikiRadar-Py/1.0 (https://www.mediawiki.org/wiki/API:Etiquette; wiki-radar@example.com) python/3"
CAVEAT = (
    "Перегляди сторінок (pageviews) Вікіпедії фіксують інтерес читачів енциклопедії, "
    "а не попит і не готовність платити. Сирі перегляди різних мовних розділів непорівнянні "
    "через різний розмір вікі. Порівняння спирається на індекс до першого місяця зі значенням 100 "
    "і на частку від сукупних pageviews того самого проєкту."
)
SPIKE_LOW = 0.20
SPIKE_MED = 0.15
PALETTE = [(0.12, 0.31, 0.47), (0.77, 0.35, 0.07), (0.33, 0.51, 0.21), (0.44, 0.19, 0.63), (0.05, 0.45, 0.47), (0.75, 0.0, 0.0)]


class ToolError(Exception):
    def __init__(self, code: str, message: str, hint: str, exit_code: int = 1, **extra):
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.exit_code = exit_code
        self.extra = extra


def emit(payload: dict, exit_code: int = 0) -> int:
    text = json.dumps(_clean(payload), ensure_ascii=False, indent=2, allow_nan=False)
    sys.stdout.write(text + "\n")
    return exit_code


def _clean(value):
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ToolError("internal", "A metric was NaN.", "Retry the command.")
        return value
    return value


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_clean(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def round_away(value: float, digits: int) -> float:
    factor = 10 ** digits
    return math.floor(value * factor + 0.5) / factor


def median(values: list[float]) -> float:
    ordered = sorted(values)
    count = len(ordered)
    if count == 0:
        return 0.0
    if count % 2:
        return ordered[count // 2]
    return (ordered[count // 2 - 1] + ordered[count // 2]) / 2


def encode_title(title: str) -> str:
    collapsed = re.sub(r"\s+", " ", title.strip().replace("\u00a0", " "))
    return urllib.parse.quote(collapsed.replace(" ", "_"), safe="")


def fold_key(value: str) -> str:
    folded = unicodedata.normalize("NFC", value.strip()).casefold()
    return "".join(ch for ch in folded if not ch.isspace() and ch != "_")


def same_title(left: str, right: str) -> bool:
    return fold_key(left) == fold_key(right)


def stems(text: str) -> list[str]:
    parts = re.split(r"[^\w]+", unicodedata.normalize("NFC", text).casefold(), flags=re.UNICODE)
    result = []
    for part in parts:
        if len(part) < 3:
            continue
        if len(part) >= 7:
            stem = part[:-2]
        elif len(part) >= 5:
            stem = part[:-1]
        else:
            stem = part
        if stem not in result:
            result.append(stem)
    return result


def covers(haystack: str, topic: str) -> bool:
    needed = stems(topic)
    if not needed:
        return False
    hay = "".join(ch for ch in unicodedata.normalize("NFC", haystack).casefold() if ch.isalnum())
    return all(stem in hay for stem in needed)


DEMOTE = ("корпус", "corpus", "школа", "school", "телеканал", "podcast", "діалект", "dialect", "австралій", "британськ", "американськ", "канадськ")
LEARNING = ("вивчен", "навчан", "learning", "learn", "study", "esl", "tefl", "tesol", "іноземн", "as a second", "as a foreign", "cizí", "drugi język")
DISAMBIG = ("(disambiguation)", "(значення)", "(ujednoznacznienie)", "(rozcestník)", "(rozlišovacia stránka)", "(begriffsklärung)")


def is_demoted(title: str, topic: str) -> bool:
    hay = title.casefold()
    query = topic.casefold()
    return any(needle in hay and needle not in query for needle in DEMOTE)


def is_learning(topic: str) -> bool:
    query = topic.casefold()
    return any(needle in query for needle in LEARNING)


def concept_of(title: str) -> str:
    folded = title.casefold()
    if any(needle in folded for needle in LEARNING):
        return "learning"
    if any(needle in folded for needle in ("мова", "language", "język", "jazyk")):
        return "language"
    return "other"


def note_for(title: str) -> str:
    concept = concept_of(title)
    if concept == "language":
        return "Стаття про мову, не про вивчення."
    if concept == "learning":
        return "Стаття про вивчення або методи, не про мову як таку."
    return "Окремий кандидат пошуку. Не змішуйте його з іншими."


def parse_langs(raw: str | None) -> list[str]:
    if not raw or not raw.strip():
        raise ToolError("invalid-args", "--langs is required.", "Example: --langs uk,pl,cs.")
    langs = []
    for part in raw.split(","):
        code = part.strip().lower()
        if code and code not in langs:
            langs.append(code)
    if not langs or any(not re.fullmatch(r"[a-z]{2,12}", code) for code in langs):
        raise ToolError("invalid-args", "--langs must be comma-separated language codes.", "Example: --langs uk,pl,cs.")
    return langs


def detect_base(topic: str) -> str:
    if any(ch in topic for ch in "іїєґІЇЄҐ"):
        return "uk"
    if any(ch in topic for ch in "ыэёъЫЭЁЪ"):
        return "ru"
    if any(ch in topic for ch in "řůěŘŮĚ"):
        return "cs"
    if any(ch in topic for ch in "ąęćłńśźżĄĘĆŁŃŚŹŻ"):
        return "pl"
    if any(ch in topic for ch in "ľŕĺĽŔĹ"):
        return "sk"
    return "en"


def project_of(lang: str) -> str:
    return f"{lang}.wikipedia.org"


def parse_stamp(stamp: str) -> datetime:
    return datetime.strptime(stamp[:8], "%Y%m%d")


def month_starts(start: str, end: str) -> list[str]:
    cursor = parse_stamp(start).replace(day=1)
    last = parse_stamp(end).replace(day=1)
    stamps = []
    while cursor <= last:
        stamps.append(cursor.strftime("%Y%m") + "0100")
        year = cursor.year + (1 if cursor.month == 12 else 0)
        month = 1 if cursor.month == 12 else cursor.month + 1
        cursor = cursor.replace(year=year, month=month)
    return stamps


def window(months: int, today: datetime | None, start: str | None, end: str | None) -> tuple[str, str]:
    if start or end:
        if not start or not end or not re.fullmatch(r"\d{10}", start) or not re.fullmatch(r"\d{10}", end):
            raise ToolError("invalid-args", "Pass both --start and --end as yyyyMMdd00.", "Use the first day for --start and the last day of the month for --end.")
        return start, end
    if months < 1 or months > 120:
        raise ToolError("invalid-args", "--months must be from 1 to 120.", "The default is 24.")
    today = today or datetime.now(timezone.utc)
    end_month = today.month - (2 if today.day < 3 else 1)
    end_year = today.year
    while end_month <= 0:
        end_month += 12
        end_year -= 1
    start_index = end_year * 12 + end_month - 1 - (months - 1)
    start_year, start_month = divmod(start_index, 12)
    start_month += 1
    end_day = calendar.monthrange(end_year, end_month)[1]
    return f"{start_year:04d}{start_month:02d}0100", f"{end_year:04d}{end_month:02d}{end_day:02d}00"


class Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS responses (url TEXT PRIMARY KEY, status INTEGER NOT NULL, body TEXT NOT NULL, stored_at TEXT NOT NULL)")

    def get(self, url: str) -> tuple[int, str] | None:
        row = self.conn.execute("SELECT status, body FROM responses WHERE url = ?", (url,)).fetchone()
        return None if row is None else (row[0], row[1])

    def put(self, url: str, status: int, body: str) -> None:
        self.conn.execute(
            "INSERT INTO responses(url, status, body, stored_at) VALUES (?, ?, ?, ?) ON CONFLICT(url) DO UPDATE SET status=excluded.status, body=excluded.body, stored_at=excluded.stored_at",
            (url, status, body, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, cache: Cache, user_agent: str, refresh: bool):
        self.cache = cache
        self.user_agent = user_agent
        self.refresh = refresh
        self.http = 0
        self.cache_hits = 0
        self.search = 0
        self.langlinks = 0
        self.pageviews = 0

    def get(self, url: str, kind: str, fixture: str | None = None, follow: bool = True) -> dict:
        if fixture:
            body = Path(fixture).read_text(encoding="utf-8")
            self.cache.put(url, 200, body)
            return {"status": 200, "body": body, "cache": "fixture", "url": url}
        if not self.refresh:
            hit = self.cache.get(url)
            if hit is not None:
                self.cache_hits += 1
                return {"status": hit[0], "body": hit[1], "cache": "hit", "url": url}
        attempts = 4
        last_error = "Network error while calling Wikimedia."
        for attempt in range(1, attempts + 1):
            try:
                self.http += 1
                if kind == "search":
                    self.search += 1
                elif kind == "langlinks":
                    self.langlinks += 1
                elif kind == "pageviews":
                    self.pageviews += 1
                request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json", "Api-User-Agent": self.user_agent})
                https = urllib.request.HTTPSHandler(context=ssl.create_default_context())
                opener = urllib.request.build_opener(https) if follow else urllib.request.build_opener(_NoRedirect, https)
                with opener.open(request, timeout=45) as response:
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    body = raw.decode("utf-8", errors="replace")
                    self.cache.put(url, 200, body)
                    time.sleep(0.12)
                    return {"status": 200, "body": body, "cache": "miss", "url": url, "location": None}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                code = exc.code
                location = exc.headers.get("Location") if exc.headers else None
                if code == 403 and "user-agent" in body.lower():
                    raise ToolError("missing-user-agent", "Wikimedia rejected the User-Agent.", "Set a descriptive User-Agent with contact information.") from exc
                if code == 403 or code in (301, 302, 303, 307, 308):
                    return {"status": code, "body": body, "cache": "miss", "url": url, "location": location}
                limit = attempts
                if code == 429 or code >= 500:
                    if attempt >= limit:
                        if code == 429:
                            raise ToolError("rate-limited", "Wikimedia kept returning 429.", "Wait and retry with run --resume STUDY_ID.", 4)
                        return {"status": code, "body": body, "cache": "miss", "url": url, "location": location}
                    time.sleep(min(2 ** (attempt - 1), 8))
                    continue
                return {"status": code, "body": body, "cache": "miss", "url": url, "location": location}
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = str(exc.reason if isinstance(exc, urllib.error.URLError) else exc)
                if attempt == attempts:
                    break
                time.sleep(2 ** (attempt - 1))
        raise ToolError("api-error", last_error, "Retry later.")

    def stats(self) -> dict:
        return {"http": self.http, "cache_hits": self.cache_hits, "search": self.search, "langlinks": self.langlinks, "pageviews": self.pageviews}


def article_url(project: str, access: str, agent: str, granularity: str, title: str, start: str, end: str) -> str:
    return f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/{access}/{agent}/{encode_title(title)}/{granularity}/{start}/{end}"


def aggregate_url(project: str, access: str, agent: str, granularity: str, start: str, end: str) -> str:
    return f"https://wikimedia.org/api/rest_v1/metrics/pageviews/aggregate/{project}/{access}/{agent}/{granularity}/{start}/{end}"


def rest_url(lang: str, path: str, query: list[tuple[str, str]] | None = None) -> str:
    url = f"https://api.wikimedia.org/core/v1/wikipedia/{lang}/{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return url


def failure_detail(body: str) -> str:
    text = re.sub(r"<[^>]+>", " ", body)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:180]


def loads(body: str) -> dict:
    return json.loads(body)


def view_items(body: str) -> list[dict]:
    data = loads(body)
    items = []
    for item in data.get("items") or []:
        if "timestamp" in item and "views" in item:
            items.append({"timestamp": item["timestamp"], "views": int(item["views"])})
    return items


def explain_http(status: int, body: str, what: str) -> ToolError:
    detail = failure_detail(body)
    message = f"{what} HTTP {status}."
    if detail:
        message = f"{message} {detail}"
    if status == 429:
        return ToolError("rate-limited", message, "Wait and retry with run --resume STUDY_ID.", 4)
    if status == 403 and "allowlist" in body.lower():
        return ToolError(
            "api-error",
            message,
            "Claude's sandbox blocked the host. In Settings, Capabilities, Code execution, Allow network egress, add api.wikimedia.org and wikimedia.org, then start a new chat. Do not invent pageviews.",
        )
    if status == 403:
        return ToolError(
            "api-error",
            message,
            "Wikimedia returned 403 to this network. A successful run on your own PC does not carry over: Claude Desktop runs the script in its sandbox, not on that PC. Do not invent pageviews.",
        )
    return ToolError("api-error", message, "Retry later.")


def rest_page(body: str) -> dict:
    data = loads(body)
    title = data.get("title") or ""
    return {"title": title, "pageid": data.get("id"), "description": data.get("description") or "", "snippet": "", "missing": False, "disambiguation": any(suffix in title.casefold() for suffix in DISAMBIG)}


def rest_search(body: str) -> list[dict]:
    hits = []
    for hit in loads(body).get("pages") or []:
        title = hit.get("title") or ""
        if any(suffix in title.casefold() for suffix in DISAMBIG):
            continue
        excerpt = re.sub(r"<[^>]+>", " ", hit.get("excerpt") or "")
        hits.append({"title": title, "pageid": hit.get("id"), "description": hit.get("description") or "", "snippet": excerpt, "missing": False, "disambiguation": False})
    return hits


def rest_langs(body: str) -> dict[str, str]:
    data = loads(body)
    if isinstance(data, dict):
        return {}
    found = {}
    for item in data:
        code = item.get("code")
        title = item.get("title") or str(item.get("key") or "").replace("_", " ")
        if code and title:
            found[code] = title
    return found


def title_from_location(location: str) -> str:
    parts = [urllib.parse.unquote(part) for part in urllib.parse.urlparse(location).path.split("/") if part]
    if len(parts) >= 2 and parts[-1] in ("bare", "language"):
        return parts[-2].replace("_", " ")
    return ""


def rest_fetch(client: Client, lang: str, path: str, kind: str, query: list[tuple[str, str]] | None = None) -> dict:
    fetched = client.get(rest_url(lang, path, query), kind, follow=False)
    if fetched["status"] in (301, 302, 303, 307, 308):
        renamed = title_from_location(fetched.get("location") or "")
        suffix = "links/language" if path.endswith("links/language") else "bare"
        if renamed and f"page/{encode_title(renamed)}/" not in path:
            fetched = client.get(rest_url(lang, f"page/{encode_title(renamed)}/{suffix}"), kind, follow=False)
    return fetched


def lookup(client: Client, lang: str, title: str) -> dict | None:
    fetched = rest_fetch(client, lang, f"page/{encode_title(title)}/bare", "lookup")
    if fetched["status"] == 404:
        return {"title": title, "pageid": None, "description": "", "snippet": "", "missing": True, "disambiguation": False}
    if fetched["status"] >= 400:
        raise explain_http(fetched["status"], fetched["body"], "Wikimedia page")
    if not fetched["body"].lstrip().startswith("{") and not fetched["body"].lstrip().startswith("["):
        raise ToolError("api-error", "Wikimedia page returned a non-JSON response.", "Retry later.")
    return rest_page(fetched["body"])


def search(client: Client, lang: str, topic: str) -> list[dict]:
    fetched = rest_fetch(client, lang, "search/page", "search", [("q", topic), ("limit", "8")])
    if fetched["status"] >= 400:
        raise explain_http(fetched["status"], fetched["body"], "Wikimedia search")
    if not fetched["body"].lstrip().startswith("{"):
        raise ToolError("api-error", "Wikimedia search returned a non-JSON response.", "Retry later.")
    return rest_search(fetched["body"])


def langlinks(client: Client, lang: str, title: str) -> dict[str, str]:
    fetched = rest_fetch(client, lang, f"page/{encode_title(title)}/links/language", "langlinks")
    if fetched["status"] == 404:
        return {}
    if fetched["status"] >= 400:
        raise explain_http(fetched["status"], fetched["body"], "Wikimedia language links")
    if not fetched["body"].lstrip().startswith("{" ) and not fetched["body"].lstrip().startswith("["):
        raise ToolError("api-error", "Wikimedia language links returned a non-JSON response.", "Retry later.")
    return rest_langs(fetched["body"])


def score_hit(hit: dict, index: int, topic: str) -> dict:
    full = covers(hit["title"], topic)
    demoted = is_demoted(hit["title"], topic)
    score = max(0, 8 - index)
    if same_title(hit["title"], topic):
        score += 100
    if full:
        score += 40
    elif any(stem in hit["title"].casefold() for stem in stems(topic)):
        score += 15
    if covers(hit["snippet"] + " " + hit["description"], topic):
        score += 10
    if demoted:
        score -= 60
    return {"hit": hit, "score": score, "full": full, "demoted": demoted}


def confidence_of(exact: bool, resolved: int, missing: int) -> str:
    if resolved == 0:
        return "insufficient"
    level = "high" if exact else "medium"
    if missing:
        level = "medium" if level == "high" else "low"
    return level


def expand(client: Client, topic: str, base: str, page: dict, via: str, langs: list[str], note: str | None) -> dict:
    links = langlinks(client, base, page["title"]) if any(lang != base for lang in langs) else {}
    matches = []
    for lang in langs:
        if lang == base:
            matches.append({"lang": lang, "title": page["title"], "page_id": page.get("pageid"), "matched_via": via, "status": "resolved", "project": project_of(lang)})
        elif lang in links:
            matches.append({"lang": lang, "title": links[lang], "page_id": None, "matched_via": "langlinks", "status": "resolved", "project": project_of(lang)})
        else:
            matches.append({"lang": lang, "title": None, "page_id": None, "matched_via": "langlinks", "status": "missing_in_language", "project": project_of(lang)})
    resolved = sum(1 for match in matches if match["status"] == "resolved")
    absent = sum(1 for match in matches if match["status"] == "missing_in_language")
    if resolved == 0:
        raise ToolError("title-not-found", "The topic has no article in the requested languages.", "Title немає. Не вигадуйте його.", 3)
    return {"topic": topic, "base_lang": base, "base_title": page["title"], "base_page_id": page.get("pageid"), "base_matched_via": via, "confidence": confidence_of(via == "exact", resolved, absent), "ambiguous": False, "selection_note": note, "matches": matches, "candidates": [], "next_checks": []}


def ambiguous(topic: str, base: str, pool: list[dict]) -> dict:
    candidates = []
    for item in pool[:4]:
        hit = item["hit"]
        candidates.append({"lang": base, "title": hit["title"], "page_id": hit.get("pageid"), "matched_via": "search", "concept": concept_of(hit["title"]), "note": note_for(hit["title"]), "score": item["score"]})
    checks = [f"Окремо виміряйте «{item['title']}» ({item['lang']}): {item['note']}" for item in candidates[:3]]
    if len(checks) < 3:
        checks.append("Уточніть тему і повторіть run з --pick TITLE. Не змішуйте кандидатів в один ряд.")
    return {"topic": topic, "base_lang": base, "base_title": "", "base_page_id": None, "base_matched_via": "", "confidence": "low", "ambiguous": True, "selection_note": None, "matches": [], "candidates": candidates, "next_checks": checks[:3]}


def resolve_topic(client: Client, topic: str, langs_raw: str | None, base_lang: str | None, pick: str | None) -> dict:
    langs = parse_langs(langs_raw)
    base = (base_lang or detect_base(topic)).lower()
    if pick:
        forced = lookup(client, base, pick)
        if not forced or forced["missing"]:
            raise ToolError("title-not-found", f"No article titled {pick} in {base}.wikipedia.", "Title немає. Не вигадуйте його.", 3)
        return expand(client, topic, base, forced, "exact", langs, "Обрано явним --pick.")
    exact = lookup(client, base, topic)
    if exact and not exact["missing"] and not exact["disambiguation"] and same_title(exact["title"], topic):
        return expand(client, topic, base, exact, "exact", langs, None)
    hits = search(client, base, topic)
    if not hits and (not exact or exact["missing"]):
        raise ToolError("title-not-found", f"No Wikipedia article matched the topic in {base}.wikipedia.", "Title немає. Не вигадуйте його.", 3)
    ranked = sorted((score_hit(hit, index, topic) for index, hit in enumerate(hits)), key=lambda item: item["score"], reverse=True)
    viable = [item for item in ranked if not item["demoted"] and item["score"] >= 15]
    strong = [item for item in viable if item["full"]]
    if len(strong) == 1 or (len(strong) > 1 and strong[0]["score"] >= strong[1]["score"] + 15):
        page = lookup(client, base, strong[0]["hit"]["title"])
        if page and not page["missing"] and not page["disambiguation"]:
            return expand(client, topic, base, page, "search", langs, None)
    pool = viable or [item for item in ranked if item["score"] > 0]
    if len(pool) >= 2 and not strong:
        return ambiguous(topic, base, pool)
    if len(pool) == 1:
        page = lookup(client, base, pool[0]["hit"]["title"])
        if page and not page["missing"] and not page["disambiguation"]:
            return expand(client, topic, base, page, "search", langs, None)
    if len(pool) >= 2:
        return ambiguous(topic, base, pool)
    raise ToolError("title-not-found", f"No Wikipedia article matched the topic in {base}.wikipedia.", "Title немає. Не вигадуйте його.", 3)


def extend_study(client: Client, study: dict, add: list[str]) -> dict:
    langs = list(study["langs"])
    for lang in add:
        if lang not in langs:
            langs.append(lang)
    matches = [dict(match) for match in study["matches"]]
    missing = [lang for lang in langs if all(match["lang"] != lang for match in matches)]
    links = langlinks(client, study["base_lang"], study["base_title"]) if any(lang != study["base_lang"] for lang in missing) else {}
    for lang in missing:
        if lang == study["base_lang"]:
            matches.append({"lang": lang, "title": study["base_title"], "page_id": study.get("base_page_id"), "matched_via": study.get("base_matched_via") or "exact", "status": "resolved", "project": project_of(lang)})
        elif lang in links:
            matches.append({"lang": lang, "title": links[lang], "page_id": None, "matched_via": "langlinks", "status": "resolved", "project": project_of(lang)})
        else:
            matches.append({"lang": lang, "title": None, "page_id": None, "matched_via": "langlinks", "status": "missing_in_language", "project": project_of(lang)})
    resolved = sum(1 for match in matches if match["status"] == "resolved")
    absent = sum(1 for match in matches if match["status"] == "missing_in_language")
    return {"topic": study["topic"], "base_lang": study["base_lang"], "base_title": study["base_title"], "base_page_id": study.get("base_page_id"), "base_matched_via": study.get("base_matched_via"), "confidence": confidence_of(study.get("base_matched_via") == "exact", resolved, absent), "ambiguous": False, "selection_note": "Продовжено з study без нового пошуку.", "matches": matches, "candidates": [], "next_checks": []}


def cap(current: str, limit: str) -> str:
    order = ["insufficient", "low", "medium", "high"]
    return limit if order.index(current) > order.index(limit) else current


def worse(left: str, right: str) -> str:
    order = ["insufficient", "low", "medium", "high"]
    return left if order.index(left) <= order.index(right) else right


def measure(series: dict, start: str, end: str) -> dict:
    api = {item["timestamp"][:6]: int(item["views"]) for item in series.get("items") or [] if len(item.get("timestamp", "")) >= 8}
    aggregate = {item["timestamp"][:6]: int(item["views"]) for item in series.get("aggregate") or [] if len(item.get("timestamp", "")) >= 8}
    grid = month_starts(start, end) if start and end else [key + "0100" for key in sorted(api)]
    if api:
        first = min(api)
        grid = [stamp for stamp in grid if stamp[:6] >= first]
    points = []
    for stamp in grid:
        present = stamp[:6] in api
        points.append({"timestamp": stamp, "views": api.get(stamp[:6], 0), "index": None, "share": None, "gap": bool(api) and not present})
    row = {"lang": series.get("lang", ""), "project": series.get("project", ""), "title": series.get("title", ""), "matched_via": series.get("matched_via"), "points": len(points), "positive_months": sum(1 for point in points if point["views"] > 0), "gap_months": sum(1 for point in points if point["gap"]), "index_last": None, "recent_to_prior_ratio": None, "share_of_project_median": None, "share_change_ratio": None, "spike_ratio": None, "peak_month": None, "peak_views": None, "raw_total": sum(point["views"] for point in points), "confidence": "insufficient", "reasons": [], "series_points": points}
    if row["points"] == 0 or row["positive_months"] < 3:
        row["reasons"].append("Порожній ряд або всі місяці нульові." if row["positive_months"] == 0 else "Менше ніж 3 місяці з переглядами.")
        return row
    base = next(point for point in points if point["views"] > 0)
    for point in points:
        point["index"] = round_away(point["views"] * 100 / base["views"], 2)
    row["index_last"] = points[-1]["index"]
    if points[0]["views"] == 0:
        row["reasons"].append("Перший місяць вікна нульовий, базу індексу зсунуто на перший ненульовий місяць.")
    if len(points) >= 12:
        recent = [float(point["views"]) for point in points[-6:]]
        prior = [float(point["views"]) for point in points[-12:-6]]
        prior_median = median(prior)
        row["recent_to_prior_ratio"] = round_away(median(recent) / prior_median, 4) if prior_median else None
    shares: list[float | None] = []
    for point in points:
        total = aggregate.get(point["timestamp"][:6], 0)
        if total > 0:
            point["share"] = point["views"] / total
            shares.append(point["share"])
        else:
            shares.append(None)
    known = [share for share in shares if share is not None]
    if known:
        row["share_of_project_median"] = round_away(median(known), 8)
    if len(points) >= 12:
        recent_share = [share for share in shares[-6:] if share is not None]
        prior_share = [share for share in shares[-12:-6] if share is not None]
        if len(recent_share) >= 4 and len(prior_share) >= 4 and median(prior_share) > 0:
            row["share_change_ratio"] = round_away(median(recent_share) / median(prior_share), 4)
    total_views = sum(point["views"] for point in points)
    peak = max(points, key=lambda point: point["views"])
    row["peak_month"] = peak["timestamp"]
    row["peak_views"] = peak["views"]
    row["spike_ratio"] = round_away(peak["views"] / total_views, 4) if total_views else None
    level = "high" if row["points"] >= 12 and row["gap_months"] <= 1 else "medium" if row["points"] >= 6 else "low"
    row["reasons"].append(f"{row['points']} місяців у вікні, прогалин {row['gap_months']}." if level == "high" else f"Коротке вікно або прогалини: місяців {row['points']}, прогалин {row['gap_months']}.")
    if row["spike_ratio"] is not None and row["spike_ratio"] > SPIKE_LOW:
        level = "low"
        row["reasons"].append(f"spike_ratio {row['spike_ratio']:.4f} вищий за поріг {SPIKE_LOW:.2f}. Пік {row['peak_month']} містить понад 20% обсягу.")
    elif row["spike_ratio"] is not None and row["spike_ratio"] > SPIKE_MED:
        level = cap(level, "medium")
        row["reasons"].append(f"spike_ratio {row['spike_ratio']:.4f} вищий за {SPIKE_MED:.2f}. Пік {row['peak_month']} треба назвати окремо.")
    if row["recent_to_prior_ratio"] is None:
        level = cap(level, "medium")
        row["reasons"].append("Немає двох сусідніх вікон по 6 місяців, відношення медіан не пораховано.")
    if row["index_last"] is not None and row["share_change_ratio"] is not None:
        diverge = (row["index_last"] > 105 and row["share_change_ratio"] < 0.95) or (row["index_last"] < 95 and row["share_change_ratio"] > 1.05)
        if diverge:
            level = cap(level, "medium")
            row["reasons"].append("Індекс і частка від проєкту розходяться за напрямком.")
    elif row["share_change_ratio"] is None:
        level = cap(level, "medium")
        row["reasons"].append("Частку від aggregate не пораховано.")
    if row["peak_month"] and row["peak_month"][4:6] == "09" and row["spike_ratio"] is not None and row["spike_ratio"] > SPIKE_MED:
        row["reasons"].append("Піковий місяць — вересень. Це часто сезон навчального року, а не сталий зсув інтересу.")
    row["confidence"] = level
    return row


def finish(document: dict) -> dict:
    usable = [series for series in document["series"] if series["confidence"] != "insufficient" and series["index_last"] is not None]
    comparison = {"basis": "index_and_share", "raw_views_used_for_ranking": False, "leader_by_index": None, "leader_by_raw_views": None, "winner": None}
    if usable:
        comparison["leader_by_index"] = max(usable, key=lambda series: series["index_last"])["lang"]
        comparison["leader_by_raw_views"] = max(document["series"], key=lambda series: series["raw_total"])["lang"]
        comparison["winner"] = comparison["leader_by_index"]
    levels = [series["confidence"] for series in document["series"]]
    confidence = "insufficient" if not levels else levels[0]
    for level in levels[1:]:
        confidence = worse(confidence, level)
    if document["missing"] and confidence != "insufficient":
        confidence = cap(confidence, "medium")
    parts = []
    for series in document["series"]:
        if series["confidence"] == "insufficient":
            continue
        ratio = series["recent_to_prior_ratio"]
        if ratio is None:
            direction = "вікно коротше за два блоки по 6 місяців"
        elif ratio > 1.05:
            direction = "медіана останніх 6 місяців вища за попередні 6"
        elif ratio < 0.95:
            direction = "медіана останніх 6 місяців нижча за попередні 6"
        else:
            direction = "медіана останніх 6 місяців близька до попередніх 6"
        parts.append(f"{series['lang']} «{series['title']}»: індекс {series['index_last']:.2f} (перший місяць = 100), {direction}.")
    if document["series"] and all(series["confidence"] == "insufficient" for series in document["series"]):
        parts.append("Даних недостатньо для висновку про зростання інтересу.")
    if comparison["leader_by_index"] and comparison["leader_by_raw_views"] and comparison["leader_by_index"] != comparison["leader_by_raw_views"]:
        parts.append(f"Лідер за індексом — {comparison['leader_by_index']}. Сума сирих переглядів більша в {comparison['leader_by_raw_views']}, і це не вердикт, бо розмір вікі різний.")
    elif comparison["winner"] and len(document["series"]) > 1:
        parts.append(f"Лідер за індексом — {comparison['winner']}.")
    for missing in document["missing"]:
        parts.append(f"{missing['lang']}: локальної статті немає, ряд не побудовано.")
    reasons = [f"{series['lang']}: {reason}" for series in document["series"] for reason in series["reasons"]]
    if document["missing"]:
        reasons.append("Не для всіх запитаних мов є локальна стаття.")
    checks = []
    if document["missing"]:
        checks.append(f"{document['missing'][0]['lang']}: статті немає. Не підставляйте назву з іншої мови і не ставте нуль замість відсутнього ряду.")
    spiked = [series for series in document["series"] if series["spike_ratio"] is not None and series["spike_ratio"] > SPIKE_MED]
    if spiked:
        top = max(spiked, key=lambda series: series["spike_ratio"])
        checks.append(f"Пік {top['lang']} у {top['peak_month']}: spike_ratio {top['spike_ratio']:.4f}. Перевірте, чи це не разова подія.")
    weak = [series["lang"] for series in document["series"] if series["confidence"] in ("low", "insufficient")]
    if weak:
        checks.append("Низька довіра для " + ", ".join(weak) + ". Причини в reasons. Не округлюйте їх до поради запускати продукт.")
    ranked = [series for series in document["series"] if series["index_last"] is not None]
    if ranked:
        best = max(ranked, key=lambda series: series["index_last"])
        checks.append(f"Найвищий індекс у {best['lang']} («{best['title']}»): {best['index_last']:.2f}. Суміжну тему порівнюйте тим самим періодом {document['start']}–{document['end']}.")
    if len(checks) < 2:
        checks.append("Порівняйте другу тему за той самий період через run --same-period-as STUDY_ID.")
    if len(checks) < 3:
        checks.append("Сирі перегляди різних вікі непорівнянні. Дивіться на індекс і на частку від aggregate.")
    document["comparison"] = comparison
    document["verdict"] = {"text": " ".join(parts), "confidence": confidence, "reasons": reasons, "caveat": CAVEAT}
    document["next_checks"] = checks[:3]
    document["caveat"] = CAVEAT
    return document


def analyze_doc(doc: dict) -> dict:
    start = doc.get("start") or ""
    end = doc.get("end") or ""
    if not start or not end:
        stamps = sorted(item["timestamp"] for series in doc.get("series") or [] for item in series.get("items") or [] if len(item.get("timestamp", "")) >= 8)
        if stamps:
            start = start or stamps[0]
            last = parse_stamp(stamps[-1])
            end = end or f"{last.year:04d}{last.month:02d}{calendar.monthrange(last.year, last.month)[1]:02d}00"
    document = {"ok": True, "command": "analyze", "topic": doc.get("topic") or "", "start": start, "end": end, "caveat": CAVEAT, "series": [measure(series, start, end) for series in doc.get("series") or []], "missing": doc.get("missing") or []}
    return finish(document)


def svg_trend(document: dict) -> str:
    series = [item for item in document["series"] if any(point.get("index") is not None for point in item["series_points"])]
    if not series:
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="720" height="360"><rect width="720" height="360" fill="#fff"/><text x="16" y="40" font-family="Noto Sans" font-size="16">{_xml(document.get("topic") or "")}</text><text x="16" y="72" font-family="Noto Sans" font-size="14">Немає індексу для графіка pageviews.</text></svg>'
    stamps = sorted({point["timestamp"] for item in series for point in item["series_points"]})
    peak = max(point.get("index") or 0 for item in series for point in item["series_points"])
    y_max = max(120, peak * 1.15)
    left, right, top, bottom = 48, 700, 64, 250
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="720" height="360" viewBox="0 0 720 360">', '<rect width="720" height="360" fill="#ffffff"/>', f'<text x="16" y="28" font-family="Noto Sans" font-size="16" font-weight="700">{_xml(document.get("topic") or "")}</text>', '<text x="16" y="48" font-family="Noto Sans" font-size="12" fill="#444">Індекс pageviews, перший ненульовий місяць = 100</text>']
    for tick in range(5):
        value = y_max * tick / 4
        y = bottom - (bottom - top) * (value / y_max)
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="#e6e6e6"/>')
        parts.append(f'<text x="8" y="{y + 4:.1f}" font-family="Noto Sans" font-size="10" fill="#666">{value:.0f}</text>')
    for index, item in enumerate(series):
        color = "#%02x%02x%02x" % tuple(int(channel * 255) for channel in PALETTE[index % len(PALETTE)])
        coords = []
        usable = [point for point in item["series_points"] if point.get("index") is not None]
        for point in usable:
            x = (left + right) / 2 if len(stamps) == 1 else left + (right - left) * stamps.index(point["timestamp"]) / (len(stamps) - 1)
            y = bottom - (bottom - top) * (point["index"] / y_max)
            coords.append(f"{x:.1f},{y:.1f}")
        parts.append(f'<polyline fill="none" stroke="{color}" stroke-width="2.5" points="{" ".join(coords)}"/>')
        parts.append(f'<rect x="16" y="{268 + index * 18}" width="12" height="12" fill="{color}"/>')
        parts.append(f'<text x="34" y="{278 + index * 18}" font-family="Noto Sans" font-size="12">{_xml(item["lang"] + ": " + item["title"])}</text>')
    if stamps:
        parts.append(f'<text x="{left}" y="346" font-family="Noto Sans" font-size="10" fill="#666">{stamps[0][:6]}</text>')
        parts.append(f'<text x="640" y="346" font-family="Noto Sans" font-size="10" fill="#666">{stamps[-1][:6]}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_candidates(topic: str, candidates: list[dict]) -> str:
    rows = [f'<svg xmlns="http://www.w3.org/2000/svg" width="720" height="360" viewBox="0 0 720 360">', '<rect width="720" height="360" fill="#fff"/>', f'<text x="16" y="32" font-family="Noto Sans" font-size="16" font-weight="700">{_xml(topic)}</text>', '<text x="16" y="56" font-family="Noto Sans" font-size="13" fill="#8a3b00">Це не графік pageviews. Тему не зведено до однієї статті.</text>']
    y = 92
    for candidate in candidates[:6]:
        rows.append(f'<text x="16" y="{y}" font-family="Noto Sans" font-size="14">{_xml(candidate["lang"] + " · " + candidate["title"])}</text>')
        rows.append(f'<text x="16" y="{y + 18}" font-family="Noto Sans" font-size="12" fill="#555">{_xml(candidate["note"])}</text>')
        y += 44
    rows.append("</svg>")
    return "".join(rows)


def _xml(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def write_png(path: Path, document: dict) -> None:
    width, height = 720, 360
    buffer = bytearray([255, 255, 255] * width * height)

    def put(x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < width and 0 <= y < height:
            index = (y * width + x) * 3
            buffer[index:index + 3] = bytes(color)

    def line(x1: int, y1: int, x2: int, y2: int, color: tuple[int, int, int]) -> None:
        steps = max(abs(x2 - x1), abs(y2 - y1), 1)
        for step in range(steps + 1):
            put(round(x1 + (x2 - x1) * step / steps), round(y1 + (y2 - y1) * step / steps), color)

    series = [item for item in document.get("series") or [] if any(point.get("index") is not None for point in item.get("series_points") or [])]
    stamps = sorted({point["timestamp"] for item in series for point in item["series_points"]})
    peak = max((point.get("index") or 0 for item in series for point in item["series_points"]), default=100)
    y_max = max(120, peak * 1.15)
    left, right, top, bottom = 48, 700, 64, 250
    line(left, top, left, bottom, (180, 180, 180))
    line(left, bottom, right, bottom, (180, 180, 180))
    for index, item in enumerate(series):
        color = tuple(int(channel * 255) for channel in PALETTE[index % len(PALETTE)])
        usable = [point for point in item["series_points"] if point.get("index") is not None]
        coords = []
        for point in usable:
            x = (left + right) / 2 if len(stamps) < 2 else left + (right - left) * stamps.index(point["timestamp"]) / (len(stamps) - 1)
            y = bottom - (bottom - top) * (point["index"] / y_max)
            coords.append((int(x), int(y)))
        for start, end in zip(coords, coords[1:]):
            line(start[0], start[1], end[0], end[1], color)
        _label(buffer, width, 16, 280 + index * 16, item["lang"], color)
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw.extend(buffer[y * width * 3:(y + 1) * width * 3])
    path.write_bytes(_png(width, height, zlib.compress(bytes(raw), 9)))


GLYPHS = {
    "0": ["01110", "10001", "10011", "10101", "11001", "10001", "01110"],
    "1": ["00100", "01100", "00100", "00100", "00100", "00100", "01110"],
    "2": ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    "3": ["11110", "00001", "00001", "01110", "00001", "00001", "11110"],
    "4": ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
    "5": ["11111", "10000", "11110", "00001", "00001", "10001", "01110"],
    "6": ["00110", "01000", "10000", "11110", "10001", "10001", "01110"],
    "7": ["11111", "00001", "00010", "00100", "01000", "01000", "01000"],
    "8": ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
    "9": ["01110", "10001", "10001", "01111", "00001", "00010", "01100"],
    "a": ["00000", "00000", "01110", "00001", "01111", "10001", "01111"],
    "c": ["00000", "00000", "01110", "10000", "10000", "10001", "01110"],
    "e": ["00000", "00000", "01110", "10001", "11111", "10000", "01110"],
    "k": ["10000", "10000", "10010", "10100", "11000", "10100", "10010"],
    "l": ["01100", "00100", "00100", "00100", "00100", "00100", "01110"],
    "n": ["00000", "00000", "10110", "11001", "10001", "10001", "10001"],
    "p": ["00000", "00000", "11110", "10001", "11110", "10000", "10000"],
    "s": ["00000", "00000", "01111", "10000", "01110", "00001", "11110"],
    "u": ["00000", "00000", "10001", "10001", "10001", "10011", "01101"],
    ":": ["00000", "00100", "00100", "00000", "00100", "00100", "00000"],
    "-": ["00000", "00000", "00000", "11111", "00000", "00000", "00000"],
}


def _label(buffer: bytearray, width: int, x: int, y: int, text: str, color: tuple[int, int, int]) -> None:
    cursor = x
    for ch in text.lower():
        glyph = GLYPHS.get(ch)
        if glyph:
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        px, py = cursor + col, y + row
                        if 0 <= px < width and 0 <= py < 360:
                            index = (py * width + px) * 3
                            buffer[index:index + 3] = bytes(color)
        cursor += 6


def _png(width: int, height: int, data: bytes) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return struct_pack(len(payload)) + tag + payload + struct_pack(zlib.crc32(tag + payload) & 0xFFFFFFFF)

    def struct_pack(value: int) -> bytes:
        return value.to_bytes(4, "big")

    ihdr = width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00"
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", data) + chunk(b"IEND", b"")


def write_chart(out: Path, svg: str, document: dict) -> tuple[str, str]:
    svg_path = out / "chart.svg"
    png_path = out / "chart.png"
    svg_path.write_text(svg, encoding="utf-8")
    write_png(png_path, document)
    return str(svg_path), str(png_path)


def render_report(path: Path, topic: str, document: dict, trend: bool) -> None:
    def draw(page) -> None:
        page.text(topic, 14, bold=True)
        page.text(f"confidence {document['verdict']['confidence']} · {document.get('start', '')}–{document.get('end', '')} · agent=user", 9, color=(0.3, 0.3, 0.3))
        page.text(document["verdict"]["text"] if trend else "Тему не зведено до однієї статті. Нижче кандидати, а не тренд pageviews.", 9)
        headers = ["Мова", "Стаття", "Індекс", "6/6", "Частка", "Довіра"]
        rows = []
        for series in document["series"][:6]:
            rows.append([series.get("lang") or "", series.get("title") or "", _num(series.get("index_last")), _num(series.get("recent_to_prior_ratio")), _num(series.get("share_change_ratio")), series.get("confidence") or ""])
        for missing in document.get("missing") or []:
            rows.append([missing["lang"], "немає локальної статті", "—", "—", "—", missing["status"]])
        top = page.y
        row_h = 16
        page.rect(34, top - row_h * (len(rows) + 1) - 4, 527, row_h, (0.12, 0.31, 0.47))
        page.y = top - 12
        page.text("  ".join(headers), 8, color=(1, 1, 1))
        for row in rows:
            page.text("  ".join(row), 8)
        chart_top = page.y - 8
        chart_bottom = max(150, chart_top - 150)
        _draw_chart(page, document, 48, chart_bottom, 540, chart_top)
        page.y = chart_bottom - 12
        page.text("Що перевірити далі", 10, bold=True)
        for check in (document.get("next_checks") or [])[:3]:
            page.text("• " + check, 8)
        page.ascii_line("Source: Wikimedia pageviews, access=all-access, agent=user, granularity=monthly.")
        page.text(CAVEAT, 8, color=(0.3, 0.3, 0.3))

    write_pdf(path, FONT_REGULAR, FONT_BOLD, draw)


def _num(value) -> str:
    if value is None:
        return "—"
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _draw_chart(page, document: dict, left: float, bottom: float, right: float, top: float) -> None:
    page.line(left, bottom, right, bottom, (0.7, 0.7, 0.7), 0.6)
    page.line(left, bottom, left, top, (0.7, 0.7, 0.7), 0.6)
    series = [item for item in document.get("series") or [] if any(point.get("index") is not None for point in item.get("series_points") or [])]
    if not series:
        return
    stamps = sorted({point["timestamp"] for item in series for point in item["series_points"]})
    peak = max(point.get("index") or 0 for item in series for point in item["series_points"]) or 100
    y_max = max(120, peak * 1.15)
    for index, item in enumerate(series):
        color = PALETTE[index % len(PALETTE)]
        coords = []
        usable = [point for point in item["series_points"] if point.get("index") is not None]
        for point in usable:
            x = (left + right) / 2 if len(stamps) < 2 else left + (right - left) * stamps.index(point["timestamp"]) / (len(stamps) - 1)
            y = bottom + (top - bottom) * (point["index"] / y_max)
            coords.append((x, y))
        page.polyline(coords, color)


def studies_dir(cache_path: Path) -> Path:
    path = cache_path.parent / "studies"
    path.mkdir(parents=True, exist_ok=True)
    return path


def new_id(topic: str) -> str:
    slug = re.sub(r"[^\w]+", "-", topic.casefold(), flags=re.UNICODE).strip("-")[:28] or "study"
    digest = hashlib.sha256(f"{topic}{time.time_ns()}".encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}"


def load_study(cache_path: Path, ident: str) -> dict:
    path = Path(ident) if Path(ident).exists() else studies_dir(cache_path) / f"{ident}.json"
    if not path.exists():
        raise ToolError("invalid-args", f"Study not found: {ident}.", "Use the study_id from an earlier run and the same --cache.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_study(cache_path: Path, out: Path, study: dict) -> None:
    write_json(studies_dir(cache_path) / f"{study['study_id']}.json", study)
    write_json(out / "study.json", study)


def user_agent(value: str | None) -> str:
    if value is None:
        return DEFAULT_UA
    if not value.strip():
        raise ToolError("missing-user-agent", "User-Agent is empty.", "Set a descriptive User-Agent with contact information.")
    return value


def open_cache(path: str | None) -> Cache:
    return Cache(Path(path) if path else Path.cwd() / ".wiki-radar" / "cache.sqlite")


def need_out(path: str | None) -> Path:
    if not path:
        raise ToolError("invalid-args", "--out is required.", "Pass an empty directory in --out.")
    out = Path(path)
    out.mkdir(parents=True, exist_ok=True)
    return out


def today_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def resolve_payload(outcome: dict, client: Client) -> dict:
    return {"ok": True, "command": "resolve", "topic": outcome["topic"], "base_lang": outcome["base_lang"], "base_title": outcome["base_title"], "base_page_id": outcome["base_page_id"], "base_matched_via": outcome["base_matched_via"], "confidence": outcome["confidence"], "ambiguous": False, "selection_note": outcome["selection_note"], "matches": outcome["matches"], "requests": client.stats()}


def ambiguous_payload(outcome: dict, study_id: str | None = None, pdf: str | None = None, png: str | None = None, svg: str | None = None) -> dict:
    return {"ok": False, "code": "ambiguous-topic", "message": "The topic matches more than one Wikipedia article.", "hint": "Show the candidates and stop. Do not invent a title and do not describe a pageviews trend. Re-run with --pick TITLE only after the user chooses.", "confidence": "low", "trend": False, "study_id": study_id, "candidates": outcome["candidates"], "next_checks": outcome["next_checks"], "pdf": pdf, "png": png, "svg": svg}


def cmd_resolve(args) -> tuple[dict, int]:
    if not args.topic:
        raise ToolError("invalid-args", "--topic is required.", "Pass the topic in the user's words.")
    out = need_out(args.out)
    client = Client(open_cache(args.cache), user_agent(args.user_agent), args.refresh)
    outcome = resolve_topic(client, args.topic, args.langs, args.base, args.pick)
    if outcome["ambiguous"]:
        raise ToolError("ambiguous-topic", "The topic matches more than one Wikipedia article.", "Show the candidates and stop. Title немає, доки користувач не обере --pick.", 2, candidates=outcome["candidates"], next_checks=outcome["next_checks"], confidence="low", trend=False)
    payload = resolve_payload(outcome, client)
    write_json(out / "resolve.json", payload)
    return payload, 0


def cmd_fetch(args) -> tuple[dict, int]:
    if args.granularity != "monthly":
        raise ToolError("invalid-args", "Only monthly granularity is supported.", "Drop --granularity or pass monthly.")
    if args.agent not in ("user", "automated", "spider", "all-agents"):
        raise ToolError("invalid-args", "Unknown --agent.", "Use user, automated, spider, or all-agents.")
    if not args.article:
        raise ToolError("invalid-args", "--article is required for fetch.", "Pass the local Wikipedia title.")
    lang = (args.lang or "en").lower()
    project = args.project or project_of(lang)
    start, end = window(args.months, today_of(args.today), args.start, args.end)
    agent = user_agent(args.user_agent)
    url = article_url(project, args.access, args.agent, args.granularity, args.article, start, end)
    agg = aggregate_url(project, args.access, args.agent, args.granularity, start, end)
    if args.dry_run:
        return {"ok": True, "command": "fetch", "dry_run": True, "agent": args.agent, "access": args.access, "granularity": args.granularity, "project": project, "article": args.article, "start": start, "end": end, "request": {"url": url, "aggregate_url": agg, "user_agent": agent}, "http_requests": 0}, 0
    out = need_out(args.out)
    client = Client(open_cache(args.cache), agent, args.refresh)
    article = client.get(url, "pageviews", args.fixture)
    if article["status"] == 404:
        raise ToolError("title-not-found", "Pageviews returned 404 for this title.", "Title немає. Не вигадуйте його.", 3)
    if article["status"] >= 400:
        raise ToolError("rate-limited" if article["status"] == 429 else "api-error", f"Pageviews HTTP {article['status']}.", "Retry later.", 4 if article["status"] == 429 else 1)
    aggregate = client.get(agg, "pageviews", args.aggregate_fixture)
    items = view_items(article["body"])
    aggregate_items = view_items(aggregate["body"]) if aggregate["status"] == 200 else []
    caches = [article["cache"], aggregate["cache"] if aggregate["status"] == 200 else article["cache"]]
    cache_state = "miss" if "miss" in caches else "hit" if caches == ["hit", "hit"] else caches[0]
    payload = {"ok": True, "command": "fetch", "cache": cache_state, "http_requests": client.http, "project": project, "article": args.article, "agent": args.agent, "access": args.access, "granularity": args.granularity, "start": start, "end": end, "request": {"url": url, "aggregate_url": agg, "user_agent": agent}, "items": items, "aggregate": aggregate_items}
    write_json(out / "fetch.json", payload)
    return payload, 0


def cmd_analyze(args) -> tuple[dict, int]:
    source = args.fixture or args.from_file
    if not source:
        raise ToolError("invalid-args", "analyze needs --fixture or --from.", "Pass a JSON file with series.")
    doc = json.loads(Path(source).read_text(encoding="utf-8"))
    if args.topic:
        doc["topic"] = args.topic
    result = analyze_doc(doc)
    write_json(need_out(args.out) / "analyze.json", result)
    return result, 0


def cmd_chart(args) -> tuple[dict, int]:
    if not args.from_file:
        raise ToolError("invalid-args", "--from is required.", "Pass the analyze.json path.")
    document = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    out = need_out(args.out)
    svg, png = write_chart(out, svg_trend(document), document)
    return {"ok": True, "command": "chart", "svg": svg, "png": png, "trend": True}, 0


def cmd_report(args) -> tuple[dict, int]:
    if not args.from_file:
        raise ToolError("invalid-args", "--from is required.", "Pass the analyze.json path.")
    document = json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    out = need_out(args.out)
    svg, png = write_chart(out, svg_trend(document), document)
    topic = args.topic or document.get("topic") or ""
    pdf = out / "report.pdf"
    render_report(pdf, topic, document, True)
    return {"ok": True, "command": "report", "pdf": str(pdf), "png": png, "svg": svg, "pages": 1, "trend": True, "topic": topic}, 0


def cmd_run(args) -> tuple[dict, int]:
    out = need_out(args.out)
    cache = open_cache(args.cache)
    client = Client(cache, user_agent(args.user_agent), args.refresh)
    resumed_study = load_study(cache.path, args.resume) if args.resume else None
    period = load_study(cache.path, args.same_period_as) if args.same_period_as else resumed_study
    start, end = (args.start, args.end) if args.start or args.end else ((period["start"], period["end"]) if period else window(args.months, today_of(args.today), None, None))
    if args.start or args.end:
        start, end = window(args.months, today_of(args.today), args.start, args.end)
    resumed_flag = False
    if resumed_study and not args.same_period_as:
        if args.topic and not same_title(args.topic, resumed_study["topic"]):
            raise ToolError("invalid-args", "Resume keeps the original topic.", "For a second topic use --same-period-as.")
        resumed_flag = True
        if args.pick:
            outcome = resolve_topic(client, resumed_study["topic"], args.langs or ",".join(resumed_study["langs"]), resumed_study["base_lang"], args.pick)
        elif resumed_study.get("ambiguous") and not args.add_langs:
            outcome = {"topic": resumed_study["topic"], "base_lang": resumed_study["base_lang"], "base_title": "", "base_page_id": None, "base_matched_via": "", "confidence": "low", "ambiguous": True, "selection_note": None, "matches": [], "candidates": resumed_study.get("candidates") or [], "next_checks": [f"Окремо виміряйте «{item['title']}» ({item['lang']}): {item['note']}" for item in (resumed_study.get("candidates") or [])[:3]]}
        else:
            extra = parse_langs(args.add_langs) if args.add_langs else []
            outcome = extend_study(client, resumed_study, [lang for lang in extra if lang not in resumed_study["langs"]])
    else:
        if not args.topic:
            raise ToolError("invalid-args", "--topic is required.", "Pass the topic in the user's words.")
        outcome = resolve_topic(client, args.topic, args.langs, args.base, args.pick)
    study = {"study_id": resumed_study["study_id"] if resumed_study and not args.same_period_as else new_id(outcome["topic"]), "topic": outcome["topic"], "base_lang": outcome["base_lang"], "base_title": outcome["base_title"], "base_page_id": outcome["base_page_id"], "base_matched_via": outcome["base_matched_via"], "langs": [match["lang"] for match in outcome["matches"]] or parse_langs(args.langs or ""), "start": start, "end": end, "months": args.months, "created_at": datetime.now(timezone.utc).isoformat(), "ambiguous": outcome["ambiguous"], "matches": outcome["matches"], "candidates": outcome["candidates"]}
    if outcome["ambiguous"]:
        save_study(cache.path, out, study)
        svg_text = svg_candidates(outcome["topic"], outcome["candidates"])
        brief = {"ok": True, "command": "analyze", "topic": outcome["topic"], "start": start, "end": end, "series": [{"lang": item["lang"], "title": item["title"], "confidence": "insufficient", "reasons": [item["note"]], "series_points": [], "index_last": None, "recent_to_prior_ratio": None, "share_change_ratio": None, "raw_total": 0} for item in outcome["candidates"][:6]], "missing": [], "verdict": {"text": "Тему не зведено до однієї статті.", "confidence": "low", "reasons": [], "caveat": CAVEAT}, "next_checks": outcome["next_checks"], "caveat": CAVEAT}
        svg, png = write_chart(out, svg_text, brief)
        pdf = out / "report.pdf"
        render_report(pdf, outcome["topic"], brief, False)
        payload = ambiguous_payload(outcome, study["study_id"], str(pdf), png, svg)
        return payload, 2
    doc = {"topic": outcome["topic"], "start": start, "end": end, "series": [], "missing": []}
    for match in outcome["matches"]:
        if match["status"] != "resolved" or not match.get("title"):
            doc["missing"].append({"lang": match["lang"], "status": "missing_in_language"})
            continue
        url = article_url(match["project"], "all-access", "user", "monthly", match["title"], start, end)
        agg = aggregate_url(match["project"], "all-access", "user", "monthly", start, end)
        article = client.get(url, "pageviews")
        if article["status"] == 404:
            doc["missing"].append({"lang": match["lang"], "status": "missing_in_language"})
            continue
        if article["status"] >= 400:
            raise ToolError("rate-limited" if article["status"] == 429 else "api-error", f"Pageviews HTTP {article['status']} for {match['lang']}.", "Wait and retry with run --resume STUDY_ID.", 4 if article["status"] == 429 else 1, study_id=study["study_id"])
        aggregate = client.get(agg, "pageviews")
        doc["series"].append({"lang": match["lang"], "project": match["project"], "title": match["title"], "matched_via": match["matched_via"], "items": view_items(article["body"]), "aggregate": view_items(aggregate["body"]) if aggregate["status"] == 200 else []})
    if not doc["series"]:
        raise ToolError("title-not-found", "None of the requested languages have a pageviews series.", "Title немає. Не вигадуйте його.", 3, study_id=study["study_id"])
    analysis = analyze_doc(doc)
    save_study(cache.path, out, study)
    write_json(out / "resolve.json", resolve_payload(outcome, client))
    write_json(out / "analyze.json", analysis)
    svg, png = write_chart(out, svg_trend(analysis), analysis)
    pdf = out / "report.pdf"
    render_report(pdf, outcome["topic"], analysis, True)
    return {"ok": True, "command": "run", "study_id": study["study_id"], "topic": outcome["topic"], "base_lang": outcome["base_lang"], "base_title": outcome["base_title"], "base_matched_via": outcome["base_matched_via"], "selection_note": outcome["selection_note"], "start": start, "end": end, "resumed": resumed_flag, "confidence": analysis["verdict"]["confidence"], "caveat": CAVEAT, "verdict": analysis["verdict"], "comparison": analysis["comparison"], "series": analysis["series"], "missing": analysis["missing"], "next_checks": analysis["next_checks"], "trend": True, "requests": client.stats(), "pdf": str(pdf), "png": png, "svg": svg, "pages": 1}, 0


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="wiki_radar.py", add_help=False)
    cli.add_argument("command", nargs="?", default="")
    cli.add_argument("--topic")
    cli.add_argument("--langs")
    cli.add_argument("--base")
    cli.add_argument("--months", type=int, default=24)
    cli.add_argument("--out")
    cli.add_argument("--resume")
    cli.add_argument("--add-langs")
    cli.add_argument("--same-period-as")
    cli.add_argument("--pick")
    cli.add_argument("--cache")
    cli.add_argument("--user-agent", nargs="?", const="", default=None)
    cli.add_argument("--refresh", action="store_true")
    cli.add_argument("--dry-run", action="store_true")
    cli.add_argument("--fixture")
    cli.add_argument("--aggregate-fixture")
    cli.add_argument("--from", dest="from_file")
    cli.add_argument("--start")
    cli.add_argument("--end")
    cli.add_argument("--lang")
    cli.add_argument("--article")
    cli.add_argument("--project")
    cli.add_argument("--agent", default="user")
    cli.add_argument("--access", default="all-access")
    cli.add_argument("--granularity", default="monthly")
    cli.add_argument("--today")
    return cli


def main(argv: list[str] | None = None) -> int:
    try:
        args, unknown = parser().parse_known_args(argv)
        if unknown:
            raise ToolError("invalid-args", f"Unknown flag {unknown[0]}.", "See the flag table in SKILL.md.")
        commands = {"resolve": cmd_resolve, "fetch": cmd_fetch, "analyze": cmd_analyze, "chart": cmd_chart, "report": cmd_report, "run": cmd_run}
        if args.command not in commands:
            raise ToolError("invalid-args", "Name a command: resolve, fetch, analyze, chart, report, or run." if not args.command else f"Unknown command {args.command}.", "Use resolve, fetch, analyze, chart, report, or run.")
        payload, code = commands[args.command](args)
        if args.out:
            write_json(Path(args.out) / "result.json", payload)
        return emit(payload, code)
    except ToolError as exc:
        payload = {"ok": False, "code": exc.code, "message": str(exc), "hint": exc.hint, **exc.extra}
        return emit(payload, exc.exit_code)
    except Exception as exc:  # noqa: BLE001
        print(exc, file=sys.stderr)
        return emit({"ok": False, "code": "internal", "message": str(exc), "hint": "Retry the command. The message is the reason it stopped."}, 1)


if __name__ == "__main__":
    sys.exit(main())
