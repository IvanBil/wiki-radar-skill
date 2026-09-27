"""Offline checks for the Claude Desktop pageviews skill."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCRIPT = ROOT / "wiki_radar.py"
FIX = ROOT / "fixtures"
FAILS: list[str] = []


def run(name: str, args: list[str], work: Path) -> tuple[int, dict, str]:
    out = work / name
    out.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(SCRIPT), *args, "--out", str(out)]
    if "--cache" not in args:
        command.extend(["--cache", str(out / "cache.sqlite")])
    completed = subprocess.run(
        command,
        cwd=work,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        FAILS.append(f"{name}: stdout is not JSON\n{completed.stdout}\n{completed.stderr}")
        payload = {}
    return completed.returncode, payload, completed.stdout


def fail(name: str, why: str) -> None:
    FAILS.append(f"{name}: {why}")
    print("FAIL", name, why)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="wiki-radar-py-") as tmp:
        work = Path(tmp)
        code, enc, _text = run("enc", ["fetch", "--dry-run", "--article", "Jupiter (Planet)", "--lang", "en", "--today", "2026-09-27", "--months", "24"], work)
        url = ((enc.get("request") or {}).get("url")) or ""
        if code != 0 or "Jupiter_%28Planet%29" not in url or "/user/" not in url or "all-agents" in url:
            fail("T-enc", url)
        elif enc.get("start") != "2024090100" or enc.get("end") != "2026083100":
            fail("T-dates", f"{enc.get('start')} {enc.get('end')}")
        else:
            print("PASS T-enc")
        czech = "P\u0159eru\u0161ovan\u00fd p\u016fst"
        _code, dia, _text = run("dia", ["fetch", "--dry-run", "--article", czech, "--lang", "cs", "--today", "2026-09-27", "--months", "24"], work)
        if "P%C5%99eru%C5%A1ovan%C3%BD_p%C5%AFst" not in ((dia.get("request") or {}).get("url") or ""):
            fail("T-dia", str((dia.get("request") or {}).get("url")))
        else:
            print("PASS T-dia")
        ua = ((enc.get("request") or {}).get("user_agent")) or ""
        if "WikiRadar-Py" not in ua or "https://" not in ua:
            fail("T-agent", ua)
        else:
            print("PASS T-agent")

        code, spike, text = run("spike", ["analyze", "--fixture", str(FIX / "spike-series.json")], work)
        series = (spike.get("series") or [{}])[0]
        if code != 0 or series.get("confidence") != "low" or not series.get("spike_ratio") or series["spike_ratio"] <= 0.2:
            fail("T-spike", text[:400])
        else:
            print("PASS T-spike")

        code, empty, text = run("empty", ["analyze", "--fixture", str(FIX / "empty-series.json")], work)
        if code != 0 or (empty.get("verdict") or {}).get("confidence") != "insufficient" or "NaN" in text:
            fail("T-empty", text[:400])
        else:
            print("PASS T-empty")

        code, norm, text = run("norm", ["analyze", "--fixture", str(FIX / "norm-pair.json")], work)
        comparison = norm.get("comparison") or {}
        if code != 0 or comparison.get("basis") != "index_and_share" or comparison.get("raw_views_used_for_ranking") or comparison.get("winner") != "pl" or comparison.get("leader_by_raw_views") != "en":
            fail("T-norm", json.dumps(comparison, ensure_ascii=False))
        else:
            print("PASS T-norm")

        code, bad, _text = run("bad", ["bogus"], work)
        if code == 0 or bad.get("ok") or not bad.get("code"):
            fail("T-json", str(bad))
        else:
            print("PASS T-json")

        code, blank, _text = run("ua", ["fetch", "--article", "X", "--lang", "uk", "--start", "2026010100", "--end", "2026022800", "--user-agent="], work)
        if code == 0 or blank.get("code") != "missing-user-agent":
            fail("T-ua", str(blank))
        else:
            print("PASS T-ua")

        cache = work / "shared.sqlite"
        code, first, _text = run("fetch1", ["fetch", "--lang", "uk", "--article", "Test", "--start", "2026010100", "--end", "2026022800", "--cache", str(cache), "--fixture", str(FIX / "sample-article.json"), "--aggregate-fixture", str(FIX / "sample-aggregate.json")], work)
        code2, second, text = run("fetch2", ["fetch", "--lang", "uk", "--article", "Test", "--start", "2026010100", "--end", "2026022800", "--cache", str(cache)], work)
        if code2 != 0 or second.get("cache") != "hit" or second.get("http_requests") != 0 or (first.get("items") or [{}])[0].get("views") != 10:
            fail("T-cache", text[:400])
        else:
            print("PASS T-cache")

        topic = (FIX / "long-topic.txt").read_text(encoding="utf-8").strip()
        code, report, text = run("pdf", ["report", "--from", str(work / "norm" / "analyze.json"), "--topic", topic], work)
        pdf = work / "pdf" / "report.pdf"
        if code != 0 or not pdf.exists():
            fail("T-pdf", text[:500])
        else:
            raw = pdf.read_bytes()
            if b"/Count 1" not in raw or b"pageviews" not in raw:
                fail("T-pdf", "missing page count or pageviews")
            elif b"0456043D0442" not in raw:
                fail("T-pdf", "missing Cyrillic text")
            else:
                print("PASS T-pdf")

    if FAILS:
        print(f"FAILED {len(FAILS)}")
        print("\n".join(FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
