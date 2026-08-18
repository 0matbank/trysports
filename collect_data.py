import os
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER_HEADER = "https://embed.st/"
ORIGIN_HEADER = "https://embed.st"

BASE_URL = os.environ.get("STREAM_SITE_URL", os.environ.get("BASE_URL", "https://streamed.pk")).rstrip("/")

CRICKET_DEFAULT_LOGO = "https://streamed.pk/api/images/proxy/GwZg7AZpYEZgHCAjAJgCzuAQ2C4+cBjYAUwRQFYxi8xg61rLoATdMFSCiUME-YAE5gFbLnrlGUGtB5lBFRvPqVgSWMEJ58fBJCGlxo+BP5zgEIA.webp"
FOOTBALL_DEFAULT_LOGO = "https://streamed.pk/favicon.ico"

def fetch_json(url, referer=None):
    if not referer:
        referer = f"{BASE_URL}/"
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[-] Fetch error for {url}: {e}")
        return None

def format_image_url(val_str):
    """টিম ব্যাজ বা পোস্টারের স্ট্রিং থেকে ১০০% সঠিক ও ভ্যালিড ওয়েবপি (.webp) ইউআরএল তৈরি করে"""
    if not val_str or not isinstance(val_str, str):
        return ""
    val = val_str.strip()
    if not val:
        return ""
    if val.startswith("http://") or val.startswith("https://"):
        return val
    if val.startswith("/"):
        return f"{BASE_URL}{val}" if val.endswith(".webp") else f"{BASE_URL}{val}.webp"
    return f"{BASE_URL}/api/images/proxy/{val}.webp"

def resolve_single_poster(m):
    """একটি ম্যাচের জন্য মাত্র একটি সঠিক ও প্রধান পোস্টার/লোগো নির্ধারণ করা"""
    # ১. সরাসরি পোস্টার থাকলে
    poster = format_image_url(m.get("poster"))
    if poster:
        return poster

    # ২. হোম টিমের লোগো থাকলে
    teams_raw = m.get("teams", {})
    if isinstance(teams_raw, dict):
        home_badge = format_image_url(teams_raw.get("home", {}).get("badge"))
        if home_badge:
            return home_badge
        away_badge = format_image_url(teams_raw.get("away", {}).get("badge"))
        if away_badge:
            return away_badge

    # ৩. ক্যাটাগরি ডিফল্ট লোগো
    cat = m.get("category", "").lower()
    return CRICKET_DEFAULT_LOGO if cat == "cricket" else FOOTBALL_DEFAULT_LOGO

def get_channel_specific_poster(channel_name, match_poster):
    """উইলো বা নির্দিষ্ট চ্যানেলের জন্য স্পেসিফিক লোগো"""
    name_lower = channel_name.lower()
    if "willow" in name_lower:
        return CRICKET_DEFAULT_LOGO
    return match_poster

def extract_direct_m3u8(browser_context, embed_url):
    """Playwright দিয়ে এম্বেড প্লেয়ার থেকে আসল ডিরেক্ট .m3u8 লিঙ্ক বের করা"""
    captured_links = []
    page = browser_context.new_page()
    
    def on_req(r):
        url = r.url
        if any(k in url.lower() for k in [".m3u8", ".mpd", "playlist", "chunklist"]) and url not in captured_links:
            if not any(ign in url.lower() for ign in ["google", "analytics", "doubleclick", "stat"]):
                captured_links.append(url)

    page.on("request", on_req)
    try:
        page.goto(embed_url, referer=f"{BASE_URL}/", timeout=15000)
        page.wait_for_timeout(3500)
        if not captured_links:
            page.mouse.click(300, 300)
            page.wait_for_timeout(2000)
    except Exception:
        pass
    finally:
        page.close()

    return captured_links[0] if captured_links else None

def get_current_bd_time():
    """বাংলাদেশ সময় (BST / UTC+6) ফরম্যাট: YYYY-MM-DD | HH:MM:SS"""
    utc_dt = datetime.now(timezone.utc)
    bst_dt = utc_dt + timedelta(hours=6)
    return bst_dt.strftime("%Y-%m-%d | %H:%M:%S")

def count_total_channels(items_list, is_live=True):
    if is_live:
        return sum(len(m.get("streams", [])) for m in items_list)
    return len(items_list)

def build_json_file(category_name, items_list, is_live=True):
    total_count = count_total_channels(items_list, is_live=is_live)
    return {
        "category_name": category_name,
        "total_items": total_count,
        "updated_time_bd": get_current_bd_time(),
        "notice": "Strictly for EDUCATIONAL PURPOSES only, not for commercial use.",
        "matches": items_list
    }

def build_m3u_file(category_name, items_list):
    bd_time = get_current_bd_time()
    total_channels = count_total_channels(items_list, is_live=True)
    
    lines = [
        "#EXTM3U\n",
        f"# CATEGORY NAME: {category_name}\n",
        f"# TOTAL-ITEMS: {total_channels}\n",
        f"# UPDATED Time and date BD: {bd_time}\n",
        "# NOTICE: Strictly for EDUCATIONAL PURPOSES only, not for commercial use.\n\n"
    ]

    for m in items_list:
        for s in m.get("streams", []):
            stream_url = s.get("direct_stream_url")
            if not stream_url:
                continue

            channel_title = s.get("channel_name", m["title"])
            channel_logo = s.get("channel_poster", m["poster"])

            lines.append(
                f'#EXTINF:-1 tvg-id="{channel_title}" '
                f'tvg-name="{channel_title}" '
                f'tvg-logo="{channel_logo}" '
                f'group-title="{m["category"].capitalize()}", {channel_title}\n'
            )
            lines.append(f"#EXTVLCOPT:http-referrer={REFERER_HEADER}\n")
            lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            lines.append(f"#EXTVLCOPT:http-origin={ORIGIN_HEADER}\n")
            lines.append(f'#EXTHTTP:{{"Referer":"{REFERER_HEADER}","Origin":"{ORIGIN_HEADER}","User-Agent":"{USER_AGENT}"}}\n')
            lines.append(f"{stream_url}\n\n")

    return "".join(lines)

def run_collector():
    print("=" * 60)
    print("  [*] SPORTS STREAM SCANNER (CLEAN & SINGLE POSTER FORMAT)")
    print("=" * 60)

    base_dir = os.path.dirname(__file__)
    cricket_dir = os.path.join(base_dir, "cricket")
    football_dir = os.path.join(base_dir, "football")
    os.makedirs(cricket_dir, exist_ok=True)
    os.makedirs(football_dir, exist_ok=True)

    live_data = fetch_json(f"{BASE_URL}/api/matches/live") or []
    live_ids = set(l.get("id") for l in live_data if isinstance(l, dict))

    all_data = fetch_json(f"{BASE_URL}/api/matches/all") or []
    print(f"[+] Total Matches in Schedule: {len(all_data)}")
    print(f"[+] Currently Active Live Matches from API: {len(live_data)}\n")

    now_ms = time.time() * 1000

    cricket_live_raw = []
    cricket_upcoming_raw = []
    football_live_raw = []
    football_upcoming_raw = []

    for m in all_data:
        cat = m.get("category", "").lower()
        if cat not in ["cricket", "football"]:
            continue

        date_ms = m.get("date", 0)
        match_id = m.get("id", "")

        is_live = (date_ms == 0) or (match_id in live_ids) or (0 < date_ms <= now_ms <= date_ms + (4 * 3600 * 1000))

        if cat == "cricket":
            if is_live:
                cricket_live_raw.append(m)
            else:
                cricket_upcoming_raw.append(m)
        elif cat == "football":
            if is_live:
                football_live_raw.append(m)
            else:
                football_upcoming_raw.append(m)

    print(f"[*] Cricket Live: {len(cricket_live_raw)} | Cricket Upcoming: {len(cricket_upcoming_raw)}")
    print(f"[*] Football Live: {len(football_live_raw)} | Football Upcoming: {len(football_upcoming_raw)}\n")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--mute-audio", "--no-sandbox"]
        )
        context = browser.new_context(user_agent=USER_AGENT)

        def process_live_matches(matches_list):
            processed = []
            for m in matches_list:
                date_ms = m.get("date", 0)
                match_id = m.get("id", "")
                match_title = m.get("title", "").strip()
                match_poster = resolve_single_poster(m)

                start_time_bd = "24/7 Live Channel" if date_ms == 0 else datetime.fromtimestamp(date_ms / 1000.0, tz=timezone.utc).strftime("%d %b %Y, %I:%M %p (BD Time)")

                all_streams_info = []
                for s in m.get("sources", []):
                    s_name = s.get("source", "admin")
                    s_id = s.get("id", match_id)
                    streams_api = f"{BASE_URL}/api/stream/{s_name}/{s_id}"
                    stream_data = fetch_json(streams_api)
                    if stream_data and isinstance(stream_data, list):
                        all_streams_info.extend(stream_data)
                    else:
                        all_streams_info.append({
                            "streamNo": 1,
                            "language": "Main",
                            "hd": True,
                            "embedUrl": f"https://embed.st/embed/{s_name}/{s_id}/1"
                        })

                extracted_streams = []
                print(f"[*] Scanning {len(all_streams_info)} stream channels for: {match_title}")

                for st in all_streams_info:
                    st_hd = st.get("hd", True)
                    hd_label = "HD" if st_hd else "SD"
                    st_lang = st.get("language", "")
                    
                    lang_clean = st_lang.replace("English - ", "").replace("English -", "").strip() if st_lang else ""
                    if lang_clean and lang_clean.lower() not in ["english", "main", "live", "default"]:
                        clean_name = f"{lang_clean} ({hd_label})"
                    else:
                        clean_name = f"{match_title} ({hd_label})"

                    channel_poster = get_channel_specific_poster(clean_name, match_poster)

                    embed_url = st.get("embedUrl", "")
                    direct_url = extract_direct_m3u8(context, embed_url) if embed_url else None
                    
                    if direct_url:
                        print(f"    [+] {clean_name}: {direct_url}")
                        extracted_streams.append({
                            "channel_name": clean_name,
                            "channel_poster": channel_poster,
                            "hd": st_hd,
                            "direct_stream_url": direct_url
                        })

                processed.append({
                    "id": match_id,
                    "title": match_title,
                    "category": m.get("category", ""),
                    "status": "LIVE_NOW",
                    "start_time_bd": start_time_bd,
                    "poster": match_poster,
                    "headers": {
                        "User-Agent": USER_AGENT,
                        "Referer": REFERER_HEADER,
                        "Origin": ORIGIN_HEADER
                    },
                    "streams": extracted_streams
                })

            return processed

        def process_upcoming_matches(matches_list):
            processed = []
            for m in matches_list:
                date_ms = m.get("date", 0)
                match_id = m.get("id", "")
                match_poster = resolve_single_poster(m)

                if date_ms > 0:
                    bst_dt = datetime.fromtimestamp(date_ms / 1000.0, tz=timezone.utc) + timedelta(hours=6)
                    start_time_bd = bst_dt.strftime("%d %b %Y, %I:%M %p (BD Time)")
                else:
                    start_time_bd = "Upcoming"

                processed.append({
                    "id": match_id,
                    "title": m.get("title", "").strip(),
                    "category": m.get("category", ""),
                    "status": "UPCOMING",
                    "start_time_bd": start_time_bd,
                    "poster": match_poster
                })
            return processed

        cricket_live = process_live_matches(cricket_live_raw)
        football_live = process_live_matches(football_live_raw)
        cricket_upcoming = process_upcoming_matches(cricket_upcoming_raw)
        football_upcoming = process_upcoming_matches(football_upcoming_raw)

        browser.close()

    cricket_upcoming.sort(key=lambda x: x.get("start_time_bd", ""))
    football_upcoming.sort(key=lambda x: x.get("start_time_bd", ""))

    # ১. Cricket Files
    with open(os.path.join(cricket_dir, "live.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Cricket Live", cricket_live, is_live=True), f, indent=2, ensure_ascii=False)
    with open(os.path.join(cricket_dir, "live.m3u"), "w", encoding="utf-8") as f:
        f.write(build_m3u_file("Cricket Live", cricket_live))
    with open(os.path.join(cricket_dir, "upcoming.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Cricket Upcoming", cricket_upcoming, is_live=False), f, indent=2, ensure_ascii=False)

    # ২. Football Files
    with open(os.path.join(football_dir, "live.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Football Live", football_live, is_live=True), f, indent=2, ensure_ascii=False)
    with open(os.path.join(football_dir, "live.m3u"), "w", encoding="utf-8") as f:
        f.write(build_m3u_file("Football Live", football_live))
    with open(os.path.join(football_dir, "upcoming.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Football Upcoming", football_upcoming, is_live=False), f, indent=2, ensure_ascii=False)

    total_cricket_channels = count_total_channels(cricket_live, is_live=True)
    total_football_channels = count_total_channels(football_live, is_live=True)

    print("\n" + "=" * 60)
    print(f"[*] cricket/live.m3u       (TOTAL-ITEMS: {total_cricket_channels} channels)")
    print(f"[*] cricket/live.json      (TOTAL-ITEMS: {total_cricket_channels} channels)")
    print(f"[*] cricket/upcoming.json  (TOTAL-ITEMS: {len(cricket_upcoming)} matches)")
    print(f"[*] football/live.m3u      (TOTAL-ITEMS: {total_football_channels} channels)")
    print(f"[*] football/live.json     (TOTAL-ITEMS: {total_football_channels} channels)")
    print(f"[*] football/upcoming.json (TOTAL-ITEMS: {len(football_upcoming)} matches)")
    print("=" * 60)
    print("  [SUCCESS] 100% CLEAN JSON & SINGLE POSTER FORMAT APPLIED!")
    print("=" * 60)

if __name__ == "__main__":
    run_collector()
