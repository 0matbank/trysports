import os
import json
import time
import urllib.request
from datetime import datetime, timezone, timedelta
from playwright.sync_api import sync_playwright

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER_HEADER = "https://embed.st/"
ORIGIN_HEADER = "https://embed.st"
BASE_URL = "https://streamed.pk"

def fetch_json(url, referer="https://streamed.pk/"):
    headers = {"User-Agent": USER_AGENT, "Referer": referer}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        print(f"[-] Fetch error for {url}: {e}")
        return None

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
        page.goto(embed_url, referer="https://streamed.pk/", timeout=15000)
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

def build_json_file(category_name, items_list):
    """কাস্টম হেডার সহ ফ্রেশ ও ক্লিন JSON তৈরি করা"""
    return {
        "category_name": category_name,
        "total_items": len(items_list),
        "updated_time_bd": get_current_bd_time(),
        "notice": "Strictly for EDUCATIONAL PURPOSES only, not for commercial use.",
        "matches": items_list
    }

def build_m3u_file(category_name, items_list):
    """স্ট্যান্ডার্ড ও ক্লিন M3U প্লেলিস্ট তৈরি করা"""
    bd_time = get_current_bd_time()
    lines = [
        "#EXTM3U\n",
        f"# CATEGORY NAME: {category_name}\n",
        f"# TOTAL-ITEMS: {len(items_list)}\n",
        f"# UPDATED Time and date BD: {bd_time}\n",
        "# NOTICE: Strictly for EDUCATIONAL PURPOSES only, not for commercial use.\n\n"
    ]

    for m in items_list:
        for s in m.get("streams", []):
            stream_url = s.get("direct_stream_url")
            if not stream_url:
                continue

            hd_str = "HD" if s.get("hd") else "SD"
            num_str = s.get("streamNo", 1)
            channel_title = f"{m['title']} | Stream {num_str} [{hd_str}]"

            # IPTV Tag
            lines.append(
                f'#EXTINF:-1 tvg-id="{m["title"]}" '
                f'tvg-name="{channel_title}" '
                f'tvg-logo="{m["poster"]}" '
                f'group-title="{m["category"].capitalize()}", {channel_title}\n'
            )
            # Headers
            lines.append(f"#EXTVLCOPT:http-referrer={REFERER_HEADER}\n")
            lines.append(f"#EXTVLCOPT:http-user-agent={USER_AGENT}\n")
            lines.append(f"#EXTVLCOPT:http-origin={ORIGIN_HEADER}\n")
            lines.append(f'#EXTHTTP:{{"Referer":"{REFERER_HEADER}","Origin":"{ORIGIN_HEADER}","User-Agent":"{USER_AGENT}"}}\n')
            # Direct Stream URL
            lines.append(f"{stream_url}\n\n")

    return "".join(lines)

def run_collector():
    print("=" * 60)
    print("  [*] SPORTS STREAM SCANNER (CLEAN STREAMS)")
    print("=" * 60)

    base_dir = os.path.dirname(__file__)
    cricket_dir = os.path.join(base_dir, "cricket")
    football_dir = os.path.join(base_dir, "football")
    os.makedirs(cricket_dir, exist_ok=True)
    os.makedirs(football_dir, exist_ok=True)

    live_data = fetch_json(f"{BASE_URL}/api/matches/live") or []
    live_ids = set(l.get("id") for l in live_data if isinstance(l, dict))

    all_data = fetch_json(f"{BASE_URL}/api/matches/all") or []
    print(f"[+] Total Matches: {len(all_data)} | Active Live: {len(live_data)}\n")

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

    print("[*] Extracting direct live streams...")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--mute-audio", "--no-sandbox"]
        )
        context = browser.new_context(user_agent=USER_AGENT)

        def process_match_list(matches_list, is_live=False):
            processed = []
            for m in matches_list:
                date_ms = m.get("date", 0)
                match_id = m.get("id", "")
                
                poster = m.get("poster")
                if poster and poster.startswith("/"):
                    poster_url = f"{BASE_URL}{poster}"
                elif poster:
                    poster_url = poster
                else:
                    poster_url = f"{BASE_URL}/favicon.ico"

                if date_ms > 0:
                    bst_dt = datetime.fromtimestamp(date_ms / 1000.0, tz=timezone.utc) + timedelta(hours=6)
                    start_time_bd = bst_dt.strftime("%d %b %Y, %I:%M %p (BD Time)")
                else:
                    start_time_bd = "24/7 Live Channel"

                sources = []
                raw_sources = m.get("sources", [])
                
                if is_live:
                    print(f"[*] Scanning Stream for: {m.get('title')}")
                    for idx, s in enumerate(raw_sources, 1):
                        s_name = s.get("source", "admin")
                        s_id = s.get("id", match_id)
                        embed_url = f"https://embed.st/embed/{s_name}/{s_id}/{idx}"
                        
                        direct_m3u8 = extract_direct_m3u8(context, embed_url)
                        if direct_m3u8:
                            print(f"    [+] Stream {idx} Direct URL: {direct_m3u8}")
                            sources.append({
                                "streamNo": idx,
                                "hd": True,
                                "direct_stream_url": direct_m3u8
                            })
                else:
                    for idx, s in enumerate(raw_sources, 1):
                        s_name = s.get("source", "admin")
                        s_id = s.get("id", match_id)
                        sources.append({
                            "streamNo": idx,
                            "hd": True,
                            "embedUrl": f"https://embed.st/embed/{s_name}/{s_id}/{idx}"
                        })

                processed.append({
                    "id": match_id,
                    "title": m.get("title", "").strip(),
                    "category": m.get("category", ""),
                    "status": "LIVE_NOW" if is_live else "UPCOMING",
                    "start_time_bd": start_time_bd,
                    "poster": poster_url,
                    "headers": {
                        "User-Agent": USER_AGENT,
                        "Referer": REFERER_HEADER,
                        "Origin": ORIGIN_HEADER
                    },
                    "total_streams": len(sources),
                    "streams": sources
                })

            return processed

        cricket_live = process_match_list(cricket_live_raw, is_live=True)
        football_live = process_match_list(football_live_raw, is_live=True)
        cricket_upcoming = process_match_list(cricket_upcoming_raw, is_live=False)
        football_upcoming = process_match_list(football_upcoming_raw, is_live=False)

        browser.close()

    cricket_upcoming.sort(key=lambda x: x.get("start_time_bd", ""))
    football_upcoming.sort(key=lambda x: x.get("start_time_bd", ""))

    # ১. Cricket Files
    with open(os.path.join(cricket_dir, "live.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Cricket Live", cricket_live), f, indent=2, ensure_ascii=False)
    with open(os.path.join(cricket_dir, "live.m3u"), "w", encoding="utf-8") as f:
        f.write(build_m3u_file("Cricket Live", cricket_live))
    with open(os.path.join(cricket_dir, "upcoming.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Cricket Upcoming", cricket_upcoming), f, indent=2, ensure_ascii=False)

    # ২. Football Files
    with open(os.path.join(football_dir, "live.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Football Live", football_live), f, indent=2, ensure_ascii=False)
    with open(os.path.join(football_dir, "live.m3u"), "w", encoding="utf-8") as f:
        f.write(build_m3u_file("Football Live", football_live))
    with open(os.path.join(football_dir, "upcoming.json"), "w", encoding="utf-8") as f:
        json.dump(build_json_file("Football Upcoming", football_upcoming), f, indent=2, ensure_ascii=False)

    # ৩. All Live Sports Combined M3U
    all_live_items = cricket_live + football_live
    with open(os.path.join(base_dir, "live.m3u"), "w", encoding="utf-8") as f:
        f.write(build_m3u_file("All Live Sports (Cricket & Football)", all_live_items))

    print(f"[*] cricket/live.m3u       ({len(cricket_live)} live items)")
    print(f"[*] cricket/live.json      ({len(cricket_live)} live items)")
    print(f"[*] cricket/upcoming.json  ({len(cricket_upcoming)} upcoming items)")
    print(f"[*] football/live.m3u      ({len(football_live)} live items)")
    print(f"[*] football/live.json     ({len(football_live)} live items)")
    print(f"[*] football/upcoming.json ({len(football_upcoming)} upcoming items)")
    print(f"[*] live.m3u (Combined)    ({len(all_live_items)} total live items)")

    print("=" * 60)
    print("  [SUCCESS] CLEANED STREAMS GENERATED!")
    print("=" * 60)

if __name__ == "__main__":
    run_collector()
