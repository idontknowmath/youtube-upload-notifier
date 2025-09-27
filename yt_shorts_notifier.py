#!/usr/bin/env python3
"""
One-shot YouTube Shorts notifier for GitHub Actions.
- Reads CHANNEL_IDS (edit below)
- Uses DISCORD_WEBHOOK from env (set as repo secret)
- Downloads short (yt-dlp) and attaches to webhook if under size limit
- Updates processed_videos.json (committed by workflow)
"""

import os, json, requests, shlex, subprocess, sys
from datetime import datetime, timezone
import xml.etree.ElementTree as ET

# ------------- EDIT THIS: add the channel IDs you want to monitor -------------
# Use the channel ID (starts with "UC..."). See instructions below if you need help.
CHANNEL_IDS = [
    "UCNhu3VutC-UjZvirkyBvQOw",   # example - replace with the channels you want
]

PROCESSED_DB = "processed_videos.json"
SHORT_DURATION_SECONDS = 60
YTDLP_BIN = os.environ.get("YTDLP_BIN", "yt-dlp")
DISCORD_MAX_BYTES = int(os.environ.get("DISCORD_MAX_BYTES", str(25 * 1024 * 1024)))

HEADERS = {"User-Agent": "yt-shorts-notifier/1.0"}

def load_processed():
    if os.path.exists(PROCESSED_DB):
        with open(PROCESSED_DB, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()

def save_processed(s):
    with open(PROCESSED_DB, "w", encoding="utf-8") as f:
        json.dump(list(s), f, indent=2)

def fetch_rss_latest(channel_id):
    rss = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    r = requests.get(rss, headers=HEADERS, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    ns = {"yt": "http://www.youtube.com/xml/schemas/2015"}
    entries = []
    for entry in root.findall("entry"):
        vid = entry.find("yt:videoId", ns).text
        title = entry.find("title").text
        published = entry.find("published").text
        link = None
        for l in entry.findall("link"):
            href = l.get("href")
            if href and "youtu" in href:
                link = href
                break
        if not link:
            link = f"https://youtu.be/{vid}"
        entries.append({"id": vid, "title": title, "published": published, "link": link})
    return entries

def parse_iso8601_duration_to_seconds(iso):
    iso = iso.replace("PT","")
    s = 0; cur=""
    for ch in iso:
        if ch.isdigit():
            cur += ch; continue
        if ch == "H": s += int(cur)*3600
        elif ch == "M": s += int(cur)*60
        elif ch == "S": s += int(cur)
        cur = ""
    return s

def get_video_duration_api(video_id, api_key):
    if not api_key:
        return None
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {"id": video_id, "part":"contentDetails,snippet", "key": api_key}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = data.get("items", [])
    if not items: return None
    item = items[0]
    dur = parse_iso8601_duration_to_seconds(item["contentDetails"]["duration"])
    title = item["snippet"]["title"]
    channel_title = item["snippet"]["channelTitle"]
    return {"duration": dur, "title": title, "channel_title": channel_title}

def probe_duration_ytdlp(video_url):
    cmd = f"{shlex.quote(YTDLP_BIN)} --no-warnings --dump-json {shlex.quote(video_url)}"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=25)
        if proc.returncode != 0:
            return None
        info = json.loads(proc.stdout)
        return {"duration": info.get("duration"), "title": info.get("title"), "channel_title": info.get("uploader")}
    except Exception:
        return None

def make_shorts_url(video_id, original_link):
    if "/shorts/" in original_link:
        return original_link
    return f"https://youtube.com/shorts/{video_id}"

def send_discord_link(channel_title, title, short_url, published):
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        print("DISCORD_WEBHOOK not set in env. Exiting.")
        sys.exit(1)
    content = f"New short by **{channel_title}**: {short_url}\nTitle: {title}\nPublished: {published}"
    data = {"content": content, "embeds":[{"title": title, "url": short_url, "fields":[{"name":"Channel","value":channel_title},{"name":"Published","value":published}], "timestamp": datetime.now(timezone.utc).isoformat()}]}
    r = requests.post(webhook, json=data, timeout=15)
    r.raise_for_status()
    print("Posted link to Discord:", short_url)

def download_and_send_discord(channel_title, title, short_url, published, video_id):
    webhook = os.environ.get("DISCORD_WEBHOOK")
    if not webhook:
        print("DISCORD_WEBHOOK not set. Exiting.")
        sys.exit(1)

    outtmpl = f"{video_id}.%(ext)s"
    cmd = f"{shlex.quote(YTDLP_BIN)} -f \"bestvideo[height<=720]+bestaudio/best[height<=720]/best[ext=mp4]/best\" --merge-output-format mp4 -o {shlex.quote(outtmpl)} {shlex.quote(short_url)}"
    print("Running:", cmd)
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
        if proc.returncode != 0:
            print("yt-dlp failed:", proc.stderr[:400])
            send_discord_link(channel_title, title, short_url, published)
            return
    except Exception as e:
        print("yt-dlp exception:", e)
        send_discord_link(channel_title, title, short_url, published)
        return

    filename = None
    for ext in ("mp4","mkv","webm","m4a"):
        cand = f"{video_id}.{ext}"
        if os.path.exists(cand):
            filename = cand
            break
    if not filename:
        print("Download produced no file, sending link instead.")
        send_discord_link(channel_title, title, short_url, published)
        return

    size = os.path.getsize(filename)
    print("Downloaded file size:", size)
    if size > DISCORD_MAX_BYTES:
        print(f"File too large ({size} bytes) > DISCORD_MAX_BYTES ({DISCORD_MAX_BYTES}). Sending link instead.")
        try: os.remove(filename)
        except: pass
        send_discord_link(channel_title, title, short_url, published)
        return

    try:
        content = f"New short by **{channel_title}**: {short_url}\nTitle: {title}\nPublished: {published}"
        with open(filename, "rb") as f:
            files = {"file": (os.path.basename(filename), f, "video/mp4")}
            payload = {"content": content}
            r = requests.post(webhook, data=payload, files=files, timeout=60)
            r.raise_for_status()
        print("Posted file to Discord:", short_url)
    except Exception as e:
        print("Failed to post file, sending link instead. Error:", e)
        send_discord_link(channel_title, title, short_url, published)
    finally:
        try:
            if os.path.exists(filename):
                os.remove(filename)
        except:
            pass

def main():
    api_key = os.environ.get("YOUTUBE_API_KEY")
    processed = load_processed()
    changed = False

    for ch in CHANNEL_IDS:
        try:
            entries = fetch_rss_latest(ch)
        except Exception as e:
            print("Failed to fetch RSS for", ch, e)
            continue
        for entry in entries:
            vid = entry["id"]
            if vid in processed:
                continue
            meta = None
            if api_key:
                try:
                    meta = get_video_duration_api(vid, api_key)
                except Exception as e:
                    print("API lookup failed:", e)
                    meta = None
            if not meta:
                meta = probe_duration_ytdlp(entry["link"])
            duration_ok = True
            if meta and meta.get("duration") is not None:
                duration_ok = meta["duration"] <= SHORT_DURATION_SECONDS
            if not duration_ok:
                print("Skipping (not a short):", vid, meta.get("duration"))
                processed.add(vid)
                changed = True
                continue

            short_url = make_shorts_url(vid, entry["link"])
            published_iso = entry.get("published") or datetime.now(timezone.utc).isoformat()+"Z"
            channel_title = meta.get("channel_title") if meta else "Unknown"
            title = meta.get("title") if meta else entry.get("title")

            try:
                download_and_send_discord(channel_title, title, short_url, published_iso, vid)
            except Exception as e:
                print("Error sending:", e)
            processed.add(vid)
            changed = True

    if changed:
        save_processed(processed)
    else:
        print("No new shorts processed.")

if __name__ == "__main__":
    main()
