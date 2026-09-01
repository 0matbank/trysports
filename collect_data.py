import argparse
import asyncio
import json
import os
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
REFERER_HEADER = "https://embed.st/"
ORIGIN_HEADER = "https://embed.st"
DEFAULT_BASE_URL = "https://streamed.pk"
BD_TIMEZONE = ZoneInfo("Asia/Dhaka")
SUPPORTED_CATEGORIES = ("cricket", "football")
DIRECT_MEDIA_MARKERS = (".m3u8", ".mpd", "playlist", "chunklist")
TRACKER_HOST_SUFFIXES = (
    "doubleclick.net",
    "google-analytics.com",
    "googletagmanager.com",
)


class CollectorError(RuntimeError):
    """Raised when a run is unsafe to publish."""


def resolve_base_url():
    candidate = (
        os.environ.get("STREAM_SITE_URL")
        or os.environ.get("BASE_URL")
        or DEFAULT_BASE_URL
    )
    candidate = candidate.strip().rstrip("/")
    parsed = urllib.parse.urlparse(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CollectorError("STREAM_SITE_URL/BASE_URL must be an absolute HTTP(S) URL")
    return candidate


BASE_URL = resolve_base_url()
CRICKET_DEFAULT_LOGO = (
    "https://streamed.pk/api/images/proxy/"
    "GwZg7AZpYEZgHCAjAJgCzuAQ2C4+cBjYAUwRQFYxi8xg61rLoATdMFSCiUME-"
    "YAE5gFbLnrlGUGtB5lBFRvPqVgSWMEJ58fBJCGlxo+BP5zgEIA.webp"
)
FOOTBALL_DEFAULT_LOGO = "https://streamed.pk/favicon.ico"


def fetch_json(url, referer=None, attempts=3, timeout=12, required=False):
    referer = referer or f"{BASE_URL}/"
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    last_error = None

    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, TimeoutError, UnicodeError, ValueError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(min(attempt, 2))

    message = f"Fetch failed after {attempts} attempt(s) for {url}: {last_error}"
    if required:
        raise CollectorError(message) from last_error
    print(f"[-] {message}")
    return None


def validate_match_payload(name, payload, require_nonempty=False):
    if not isinstance(payload, list):
        raise CollectorError(
            f"{name} API returned {type(payload).__name__}, expected list"
        )
    if require_nonempty and not payload:
        raise CollectorError(
            f"{name} API returned an empty schedule; preserving current outputs"
        )

    for index, match in enumerate(payload):
        if not isinstance(match, dict):
            raise CollectorError(f"{name}[{index}] is not an object")
        if not isinstance(match.get("id"), str) or not match["id"].strip():
            raise CollectorError(f"{name}[{index}] has no valid id")
        if not isinstance(match.get("category"), str):
            raise CollectorError(f"{name}[{index}] has no valid category")


def normalise_date_ms(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CollectorError(f"Invalid match date value: {value!r}")
    return int(value)


def classify_match(match, live_ids, now_ms):
    date_ms = normalise_date_ms(match.get("date", 0))
    match_id = match.get("id", "")

    if match_id in live_ids or date_ms == 0:
        return "live"
    if date_ms < 0 or date_ms > now_ms:
        return "upcoming"
    return "ended"


def split_matches(all_data, live_ids, now_ms):
    grouped = {
        "cricket": {"live": [], "upcoming": [], "ended": []},
        "football": {"live": [], "upcoming": [], "ended": []},
    }

    for match in all_data:
        category = match.get("category", "").lower()
        if category not in grouped:
            continue
        status = classify_match(match, live_ids, now_ms)
        grouped[category][status].append(match)

    return grouped


def format_bd_time(date_ms, live=False):
    date_ms = normalise_date_ms(date_ms)
    if date_ms == 0:
        return "24/7 Live Channel" if live else "Upcoming"
    if date_ms < 0:
        return "Upcoming"
    bd_datetime = datetime.fromtimestamp(date_ms / 1000.0, tz=timezone.utc).astimezone(
        BD_TIMEZONE
    )
    return bd_datetime.strftime("%d %b %Y, %I:%M %p (BD Time)")


def resolve_single_poster(match):
    poster = match.get("poster")
    if isinstance(poster, str) and poster.strip():
        poster = poster.strip()
        if poster.startswith(("http://", "https://")):
            return poster
        if poster.startswith("/"):
            suffix = "" if poster.endswith(".webp") else ".webp"
            return f"{BASE_URL}{poster}{suffix}"
        return f"{BASE_URL}/api/images/proxy/{poster}.webp"

    teams = match.get("teams")
    if isinstance(teams, dict):
        home = teams.get("home") if isinstance(teams.get("home"), dict) else {}
        away = teams.get("away") if isinstance(teams.get("away"), dict) else {}
        home_badge = home.get("badge", "")
        away_badge = away.get("badge", "")
        home_badge = home_badge.strip() if isinstance(home_badge, str) else ""
        away_badge = away_badge.strip() if isinstance(away_badge, str) else ""

        if home_badge and away_badge:
            return f"{BASE_URL}/api/images/poster/{home_badge}/{away_badge}.webp"
        if home_badge:
            return f"{BASE_URL}/api/images/proxy/{home_badge}.webp"
        if away_badge:
            return f"{BASE_URL}/api/images/proxy/{away_badge}.webp"

    category = match.get("category", "").lower()
    return CRICKET_DEFAULT_LOGO if category == "cricket" else FOOTBALL_DEFAULT_LOGO


def get_channel_specific_poster(channel_name, match_poster):
    if "willow" in channel_name.lower():
        return CRICKET_DEFAULT_LOGO
    return match_poster


def is_direct_media_url(url):
    if not isinstance(url, str) or not url.strip():
        return False
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    candidate = url.lower()
    path = parsed.path.lower()
    if "/embed/" in path and not any(
        extension in candidate for extension in (".m3u8", ".mpd")
    ):
        return False
    return any(marker in candidate for marker in DIRECT_MEDIA_MARKERS)


def is_tracking_url(url):
    try:
        host = urllib.parse.urlparse(url).hostname or ""
    except ValueError:
        return True
    host = host.lower()
    return any(
        host == suffix or host.endswith(f".{suffix}")
        for suffix in TRACKER_HOST_SUFFIXES
    )


async def extract_direct_stream(browser_context, embed_url, semaphore):
    if not isinstance(embed_url, str) or not embed_url.startswith(
        ("http://", "https://")
    ):
        return None

    captured_links = []
    async with semaphore:
        page = await browser_context.new_page()

        def on_request(request):
            url = request.url
            if (
                is_direct_media_url(url)
                and not is_tracking_url(url)
                and url not in captured_links
            ):
                captured_links.append(url)

        page.on("request", on_request)
        try:
            await page.goto(
                embed_url,
                referer=f"{BASE_URL}/",
                timeout=15000,
                wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(3500)
            if not captured_links:
                await page.mouse.click(300, 300)
                await page.wait_for_timeout(2000)
        except (PlaywrightError, OSError) as exc:
            print(f"    [-] Embed scan failed ({type(exc).__name__})")
        finally:
            await page.close()

    return captured_links[0] if captured_links else None


def deduplicate_stream_entries(stream_entries):
    unique = []
    seen = set()
    for entry in stream_entries:
        if not isinstance(entry, dict):
            continue
        embed_url = entry.get("embedUrl")
        if not isinstance(embed_url, str) or not embed_url.strip():
            continue
        key = embed_url.strip()
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique


async def fetch_match_stream_entries(match):
    tasks = []
    match_id = match.get("id", "")
    for source in match.get("sources", []):
        if not isinstance(source, dict):
            continue
        source_name = str(source.get("source") or "admin")
        source_id = str(source.get("id") or match_id)
        safe_name = urllib.parse.quote(source_name, safe="")
        safe_id = urllib.parse.quote(source_id, safe="")
        stream_api = f"{BASE_URL}/api/stream/{safe_name}/{safe_id}"
        tasks.append(asyncio.to_thread(fetch_json, stream_api))

    if not tasks:
        return []

    responses = await asyncio.gather(*tasks)
    entries = []
    for response in responses:
        if isinstance(response, list):
            entries.extend(response)
    return deduplicate_stream_entries(entries)


def build_channel_name(match_title, stream):
    is_hd = stream.get("hd", True)
    quality = "HD" if is_hd else "SD"
    language = stream.get("language", "")
    language = language if isinstance(language, str) else ""
    cleaned = language.replace("English - ", "").replace("English -", "").strip()
    if cleaned and cleaned.lower() not in {"english", "main", "live", "default"}:
        return f"{cleaned} ({quality})"
    return f"{match_title} ({quality})"


async def process_live_match(match, browser_context, semaphore):
    match_id = match.get("id", "")
    match_title = str(match.get("title") or "").strip()
    match_poster = resolve_single_poster(match)
    stream_entries = await fetch_match_stream_entries(match)
    print(
        f"[*] Scanning {len(stream_entries)} unique stream channel(s) for: {match_title}"
    )

    async def extract_entry(stream):
        embed_url = stream.get("embedUrl", "")
        direct_url = await extract_direct_stream(browser_context, embed_url, semaphore)
        if not is_direct_media_url(direct_url):
            return None

        channel_name = build_channel_name(match_title, stream)
        print(f"    [+] {channel_name}: {direct_url}")
        return {
            "channel_name": channel_name,
            "channel_poster": get_channel_specific_poster(channel_name, match_poster),
            "hd": bool(stream.get("hd", True)),
            "direct_stream_url": direct_url,
        }

    extracted = await asyncio.gather(
        *(extract_entry(stream) for stream in stream_entries)
    )
    unique_streams = []
    seen_direct_urls = set()
    for stream in extracted:
        if not stream:
            continue
        direct_url = stream["direct_stream_url"]
        if direct_url in seen_direct_urls:
            continue
        seen_direct_urls.add(direct_url)
        unique_streams.append(stream)

    if not unique_streams:
        print(
            f"    [-] No direct media stream found; omitting live match: {match_title}"
        )
        return None

    date_ms = normalise_date_ms(match.get("date", 0))
    return {
        "id": match_id,
        "title": match_title,
        "category": match.get("category", ""),
        "status": "LIVE_NOW",
        "start_time_bd": format_bd_time(date_ms, live=True),
        "start_epoch_ms": date_ms,
        "poster": match_poster,
        "headers": {
            "User-Agent": USER_AGENT,
            "Referer": REFERER_HEADER,
            "Origin": ORIGIN_HEADER,
        },
        "streams": unique_streams,
    }


async def process_live_matches(matches, browser_context, semaphore):
    if not matches:
        return []
    results = await asyncio.gather(
        *(process_live_match(match, browser_context, semaphore) for match in matches)
    )
    return [result for result in results if result]


def process_upcoming_matches(matches):
    processed = []
    for match in matches:
        date_ms = normalise_date_ms(match.get("date", 0))
        processed.append(
            {
                "id": match.get("id", ""),
                "title": str(match.get("title") or "").strip(),
                "category": match.get("category", ""),
                "status": "UPCOMING",
                "start_time_bd": format_bd_time(date_ms),
                "start_epoch_ms": date_ms,
                "poster": resolve_single_poster(match),
            }
        )

    processed.sort(
        key=lambda match: (
            match["start_epoch_ms"] < 0,
            max(match["start_epoch_ms"], 0),
            match["id"],
        )
    )
    return processed


def get_current_bd_datetime():
    return datetime.now(timezone.utc).astimezone(BD_TIMEZONE)


def get_current_bd_time():
    return get_current_bd_datetime().strftime("%Y-%m-%d | %H:%M:%S")


def count_total_streams(items_list):
    return sum(len(match.get("streams", [])) for match in items_list)


def build_json_file(category_name, items_list, is_live=True):
    total_matches = len(items_list)
    total_streams = count_total_streams(items_list) if is_live else 0
    legacy_total = total_streams if is_live else total_matches
    updated = get_current_bd_datetime()
    return {
        "schema_version": 2,
        "category_name": category_name,
        "total_items": legacy_total,
        "total_matches": total_matches,
        "total_streams": total_streams,
        "updated_time_bd": updated.strftime("%Y-%m-%d | %H:%M:%S"),
        "updated_at_bd": updated.isoformat(timespec="seconds"),
        "notice": "Strictly for EDUCATIONAL PURPOSES only, not for commercial use.",
        "matches": items_list,
    }


def sanitise_m3u_value(value):
    return str(value or "").replace('"', "'").replace("\r", " ").replace("\n", " ")


def build_m3u_file(category_name, items_list):
    total_streams = count_total_streams(items_list)
    lines = [
        "#EXTM3U\n",
        f"# CATEGORY NAME: {category_name}\n",
        f"# TOTAL-ITEMS: {total_streams}\n",
        f"# UPDATED Time and date BD: {get_current_bd_time()}\n",
        "# NOTICE: Strictly for EDUCATIONAL PURPOSES only, not for commercial use.\n\n",
    ]

    for match in items_list:
        for stream in match.get("streams", []):
            stream_url = stream.get("direct_stream_url")
            if not is_direct_media_url(stream_url):
                continue

            channel_title = sanitise_m3u_value(
                stream.get("channel_name", match.get("title", ""))
            )
            channel_logo = sanitise_m3u_value(
                stream.get("channel_poster", match.get("poster", ""))
            )
            group_title = sanitise_m3u_value(match.get("category", "").capitalize())

            lines.append(
                f'#EXTINF:-1 tvg-id="{channel_title}" '
                f'tvg-name="{channel_title}" '
                f'tvg-logo="{channel_logo}" '
                f'group-title="{group_title}", {channel_title}\n'
            )
            lines.append(f"#EXTVLCOPT:http-referrer={REFERER_HEADER}\n")
            lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            lines.append(f"#EXTVLCOPT:http-origin={ORIGIN_HEADER}\n")
            lines.append(
                f'#EXTHTTP:{{"Referer":"{REFERER_HEADER}",'
                f'"Origin":"{ORIGIN_HEADER}","User-Agent":"{USER_AGENT}"}}\n'
            )
            lines.append(f"{stream_url}\n\n")

    return "".join(lines)


def build_output_bundle(
    cricket_live, cricket_upcoming, football_live, football_upcoming
):
    json_payloads = {
        "cricket/live.json": build_json_file(
            "Cricket Live", cricket_live, is_live=True
        ),
        "cricket/upcoming.json": build_json_file(
            "Cricket Upcoming", cricket_upcoming, is_live=False
        ),
        "football/live.json": build_json_file(
            "Football Live", football_live, is_live=True
        ),
        "football/upcoming.json": build_json_file(
            "Football Upcoming", football_upcoming, is_live=False
        ),
    }
    bundle = {
        path: json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        for path, payload in json_payloads.items()
    }
    bundle["cricket/live.m3u"] = build_m3u_file("Cricket Live", cricket_live)
    bundle["football/live.m3u"] = build_m3u_file("Football Live", football_live)
    return bundle


def validate_output_bundle(bundle):
    for relative_path, text in bundle.items():
        if relative_path.endswith(".json"):
            payload = json.loads(text)
            matches = payload.get("matches")
            if not isinstance(matches, list):
                raise CollectorError(f"{relative_path} has invalid matches")
            if payload.get("total_matches") != len(matches):
                raise CollectorError(f"{relative_path} total_matches mismatch")

            stream_count = count_total_streams(matches)
            if payload.get("total_streams") != stream_count:
                raise CollectorError(f"{relative_path} total_streams mismatch")

            for match in matches:
                if "embedUrl" in match or "embed_url" in match:
                    raise CollectorError(f"{relative_path} contains an embed field")
                if match.get("status") == "LIVE_NOW" and not match.get("streams"):
                    raise CollectorError(
                        f"{relative_path} contains an empty live match"
                    )
                for stream in match.get("streams", []):
                    if "embedUrl" in stream or "embed_url" in stream:
                        raise CollectorError(f"{relative_path} contains an embed field")
                    if not is_direct_media_url(stream.get("direct_stream_url")):
                        raise CollectorError(
                            f"{relative_path} contains a non-media URL"
                        )
        else:
            for line in text.splitlines():
                if line.startswith(("http://", "https://")) and not is_direct_media_url(
                    line
                ):
                    raise CollectorError(f"{relative_path} contains a non-media URL")


def write_output_bundle(output_dir, bundle):
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".collector-staging-", dir=output_dir
    ) as temp_dir:
        staging_root = Path(temp_dir)
        for relative_path, text in bundle.items():
            destination = staging_root / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8", newline="\n")

        staged_bundle = {
            relative_path: (staging_root / relative_path).read_text(encoding="utf-8")
            for relative_path in bundle
        }
        validate_output_bundle(staged_bundle)

        for relative_path in bundle:
            destination = output_dir / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staging_root / relative_path, destination)


def default_output_dir():
    current_dir = Path(__file__).resolve().parent
    return current_dir.parent if current_dir.name.lower() == "scanner" else current_dir


async def collect(output_dir, max_concurrency=4, dry_run=False):
    print("=" * 60)
    print("  [*] SPORTS STREAM SCANNER (SAFE PUBLISH MODE)")
    print("=" * 60)

    live_data = fetch_json(f"{BASE_URL}/api/matches/live", required=True)
    all_data = fetch_json(f"{BASE_URL}/api/matches/all", required=True)
    validate_match_payload("live", live_data)
    validate_match_payload("all", all_data, require_nonempty=True)

    live_ids = {match["id"] for match in live_data}
    grouped = split_matches(all_data, live_ids, int(time.time() * 1000))

    print(f"[+] Total Matches in Schedule: {len(all_data)}")
    print(f"[+] Currently Active Live Matches from API: {len(live_data)}")
    for category in SUPPORTED_CATEGORIES:
        counts = grouped[category]
        print(
            f"[*] {category.capitalize()} Live: {len(counts['live'])} | "
            f"Upcoming: {len(counts['upcoming'])} | Ended skipped: {len(counts['ended'])}"
        )

    cricket_live = []
    football_live = []
    if grouped["cricket"]["live"] or grouped["football"]["live"]:
        semaphore = asyncio.Semaphore(max(1, max_concurrency))
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--mute-audio",
                    "--no-sandbox",
                ],
            )
            context = await browser.new_context(user_agent=USER_AGENT)
            cricket_live, football_live = await asyncio.gather(
                process_live_matches(grouped["cricket"]["live"], context, semaphore),
                process_live_matches(grouped["football"]["live"], context, semaphore),
            )
            await browser.close()

    cricket_upcoming = process_upcoming_matches(grouped["cricket"]["upcoming"])
    football_upcoming = process_upcoming_matches(grouped["football"]["upcoming"])
    bundle = build_output_bundle(
        cricket_live,
        cricket_upcoming,
        football_live,
        football_upcoming,
    )
    validate_output_bundle(bundle)

    if dry_run:
        print("[*] Dry run: validated output was not written")
    else:
        write_output_bundle(output_dir, bundle)

    totals = {
        "cricket_live_streams": count_total_streams(cricket_live),
        "cricket_upcoming_matches": len(cricket_upcoming),
        "football_live_streams": count_total_streams(football_live),
        "football_upcoming_matches": len(football_upcoming),
    }
    print(json.dumps(totals, sort_keys=True))
    print("[SUCCESS] Collector output validated")
    return bundle


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect cricket and football stream data"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=default_output_dir(),
        help="Directory that receives cricket/ and football/ output",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=int(os.environ.get("SCAN_CONCURRENCY", "4")),
        help="Maximum simultaneous Playwright pages",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch, scan, build, and validate without changing output files",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    try:
        asyncio.run(
            collect(
                output_dir=args.output_dir.resolve(),
                max_concurrency=args.max_concurrency,
                dry_run=args.dry_run,
            )
        )
    except CollectorError as exc:
        print(f"[FATAL] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
