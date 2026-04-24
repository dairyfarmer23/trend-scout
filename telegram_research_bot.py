#!/usr/bin/env python3
"""Telegram Research Digest Bot.

Two modes:
  1. LINKS mode: Manager sends TikTok links → bot scrapes via Apify →
     generates "record this" scripts adapted for your platforms.
  2. DIGEST mode: Pulls VPS trend-scout data → sends daily script digest.

Feedback loop: reply with script number you filmed → system learns.

Usage:
    python telegram_research_bot.py links URL1 URL2 ...   Scrape TikToks, send scripts
    python telegram_research_bot.py digest                Send daily trend digest
    python telegram_research_bot.py poll                  Check for feedback + new links
    python telegram_research_bot.py test                  Send test message

Env vars (see .env.example):
    TELEGRAM_BOT_TOKEN   Bot token (required)
    TELEGRAM_CHAT_ID     Target chat ID (required)
    APIFY_API_KEY        Apify key for TikTok scraping
    OPENAI_API_KEY       OpenAI key for script generation + conversational editing
    VPS_HOST             (optional) SSH host for remote trend-scout data pulls
    VPS_TREND_SCOUT      (optional) path on VPS to the trend-scout install
    VAULT_PATH           (optional) path to your Obsidian vault for memory writes
"""

import json
import os
import re
import shlex
import subprocess
import sys
import textwrap
from datetime import datetime
from pathlib import Path

# --- Config ---

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_TREND_SCOUT = os.environ.get("VPS_TREND_SCOUT", "/opt/trend-scout")
VPS_CONFIG = os.environ.get("VPS_CONFIG", "")

def _load_env_file():
    """Load env vars from .env if they're not already set. TREND_SCOUT_ENV overrides."""
    override = os.environ.get("TREND_SCOUT_ENV")
    for env_path in [Path(override)] if override else [Path(__file__).parent / ".env"]:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    if key not in os.environ or not os.environ[key]:
                        os.environ[key] = val
            break

_load_env_file()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

KB_DIR = Path(__file__).parent / "knowledge_base"
RAW_DIR = KB_DIR / "raw" / "trends"
FEEDBACK_FILE = RAW_DIR / "feedback_local.json"
LINKS_QUEUE_FILE = RAW_DIR / "pending_links.json"

TG_MAX_LEN = 4000
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CHAT_HISTORY_FILE = RAW_DIR / "chat_history.json"

TIKTOK_URL_RE = re.compile(
    r"https?://(?:(?:vm|www)\.)?tiktok\.com/(?:@[\w.]+/video/\d+|[A-Za-z0-9]+/?)"
)
TRAINING_FILE = RAW_DIR / "script_training.json"


# ──────────────────────────────────────────
# Credentials
# ──────────────────────────────────────────

def get_bot_token():
    """Get Telegram bot token: env > VPS config > macOS keychain."""
    if TELEGRAM_BOT_TOKEN:
        return TELEGRAM_BOT_TOKEN
    for fn in [_token_from_vps, _token_from_keychain]:
        t = fn()
        if t:
            return t
    print("ERROR: No Telegram bot token. Set TELEGRAM_BOT_TOKEN env var.")
    sys.exit(1)


def _token_from_vps():
    try:
        r = subprocess.run(
            ["ssh", VPS_HOST,
             f"python3 -c \"import json; print(json.load(open('{VPS_CONFIG}'))['channels']['telegram']['botToken'])\""],
            capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        return None


def _token_from_keychain():
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "com.trend-scout", "-a", "telegram_bot_token", "-w"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        return None


def get_apify_key():
    """Get Apify API key: env > macOS keychain."""
    key = os.environ.get("APIFY_API_KEY", "")
    if key:
        return key
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "com.trend-scout", "-a", "apify_api_key", "-w"],
            capture_output=True, text=True, timeout=5)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


# ──────────────────────────────────────────
# Telegram helpers
# ──────────────────────────────────────────

def send_telegram(token, chat_id, text, parse_mode="HTML"):
    """Send message via Telegram Bot API, splitting if needed."""
    import urllib.request
    import urllib.parse

    chunks = _split_message(text)
    for i, chunk in enumerate(chunks):
        data = urllib.parse.urlencode({
            "chat_id": chat_id, "text": chunk,
            "parse_mode": parse_mode, "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        try:
            resp = urllib.request.urlopen(req, timeout=10)
            result = json.loads(resp.read())
            if not result.get("ok"):
                print(f"Telegram API error: {result}")
                return False
        except Exception as e:
            print(f"Send failed (chunk {i+1}/{len(chunks)}): {e}")
            return False
    return True


def get_updates(token, offset=None):
    """Get recent messages sent to the bot."""
    import urllib.request
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    if offset:
        url += f"?offset={offset}"
    try:
        resp = urllib.request.urlopen(url, timeout=10)
        return json.loads(resp.read()).get("result", [])
    except Exception as e:
        print(f"getUpdates failed: {e}")
        return []


def _split_message(text):
    if len(text) <= TG_MAX_LEN:
        return [text]
    parts = text.split("\n\n")
    chunks, current = [], ""
    for part in parts:
        if len(current) + len(part) + 2 > TG_MAX_LEN:
            if current:
                chunks.append(current.strip())
            current = part
        else:
            current = f"{current}\n\n{part}" if current else part
    if current:
        chunks.append(current.strip())
    return chunks


def ssh_run(cmd, timeout=30):
    return subprocess.run(["ssh", VPS_HOST, cmd], capture_output=True, text=True, timeout=timeout)


# ──────────────────────────────────────────
# TikTok scraping via Apify
# ──────────────────────────────────────────

def resolve_tiktok_urls(urls):
    """Resolve vm.tiktok.com short links to full URLs."""
    import urllib.request
    resolved = []
    for url in urls:
        if "vm.tiktok.com" in url:
            try:
                req = urllib.request.Request(url, method="HEAD")
                req.add_header("User-Agent", "Mozilla/5.0")
                resp = urllib.request.urlopen(req, timeout=5)
                resolved.append(resp.url.split("?")[0])
            except Exception:
                resolved.append(url)
        else:
            resolved.append(url.split("?")[0])
    return resolved


def scrape_tiktoks(urls):
    """Scrape TikTok video details + transcribe audio via Apify.

    Returns list of dicts with: url, username, description, hashtags,
    stats, music, duration, transcript.
    """
    api_key = get_apify_key()
    if not api_key:
        print("WARNING: No Apify key. Using URL metadata only.")
        return [{"url": u, "username": _username_from_url(u)} for u in urls]

    try:
        from apify_client import ApifyClient
    except Exception as e:
        print(f"WARNING: apify-client import failed: {e}. Python: {sys.executable}")
        return [{"url": u, "username": _username_from_url(u)} for u in urls]

    client = ApifyClient(api_key)

    # Step 1: Scrape metadata + stats
    print(f"[Apify] Scraping {len(urls)} TikTok videos...")
    run_input = {
        "postURLs": urls,
        "resultsPerPage": 1,
        "shouldDownloadVideos": False,
        "shouldDownloadCovers": False,
    }

    results = []
    try:
        run = client.actor("clockworks/tiktok-scraper").call(run_input=run_input, timeout_secs=120)
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        print(f"[Apify] Got {len(items)} metadata results")

        for item in items:
            results.append({
                "url": item.get("webVideoUrl", item.get("url", "")),
                "username": item.get("authorMeta", {}).get("name", item.get("author", "")),
                "display_name": item.get("authorMeta", {}).get("nickName", ""),
                "description": item.get("text", item.get("desc", "")),
                "hashtags": [h.get("name", "") for h in item.get("hashtags", [])],
                "stats": {
                    "views": item.get("playCount", item.get("stats", {}).get("playCount", 0)),
                    "likes": item.get("diggCount", item.get("stats", {}).get("diggCount", 0)),
                    "comments": item.get("commentCount", item.get("stats", {}).get("commentCount", 0)),
                    "shares": item.get("shareCount", item.get("stats", {}).get("shareCount", 0)),
                },
                "music": item.get("musicMeta", {}).get("musicName", ""),
                "duration": item.get("videoMeta", {}).get("duration", 0),
                "transcript": "",
            })
    except Exception as e:
        print(f"[Apify] Metadata scrape failed: {e}")

    # Fill in missing URLs
    scraped_urls = {r["url"] for r in results}
    for url in urls:
        if url not in scraped_urls:
            results.append({"url": url, "username": _username_from_url(url), "transcript": ""})

    # Step 1.5: Check for cached transcripts from previous runs
    cached = _get_cached_transcripts()
    for result in results:
        vid_id = _video_id_from_url(result.get("url", ""))
        if vid_id and vid_id in cached:
            result["transcript"] = cached[vid_id]
            print(f"  [cache] {vid_id[:20]}... ({len(cached[vid_id])} chars)")

    # Step 2: Transcribe videos that aren't cached
    uncached_urls = [r["url"] for r in results if not r.get("transcript") and r.get("url")]
    if uncached_urls:
        print(f"[Apify] Transcribing {len(uncached_urls)} new videos...")
        transcripts = transcribe_tiktoks(client, uncached_urls)

        # Match transcripts back to results by video ID
        for result in results:
            if result.get("transcript"):
                continue
            result_url = result.get("url", "")
            result_vid_id = _video_id_from_url(result_url)
            for url_key, transcript in transcripts.items():
                if result_vid_id and result_vid_id in url_key:
                    result["transcript"] = transcript
                    break
                elif result_url == url_key:
                    result["transcript"] = transcript
                    break
    else:
        print("[Apify] All videos found in cache")

    transcribed = sum(1 for r in results if r.get("transcript"))
    print(f"[Apify] Transcribed {transcribed}/{len(results)} videos")

    return results


def transcribe_tiktoks(client, urls):
    """Transcribe TikTok videos.

    Pipeline:
    1. Try Apify transcriber (works for public videos)
    2. Fall back to yt-dlp + Replicate Whisper for blocked/restricted videos
    """
    transcripts = {}

    # Step 1: Try Apify for all URLs
    try:
        print("[Apify] Transcribing via tictechid/anoxvanzi-transcriber...")
        for url in urls:
            try:
                run_input = {"start_urls": url}
                run = client.actor("tictechid/anoxvanzi-transcriber").call(
                    run_input=run_input, timeout_secs=120)
                items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

                for item in items:
                    source_url = item.get("sourceUrl", "")
                    raw_transcript = item.get("transcript", "")
                    status = item.get("status", "")

                    if status == "success" and raw_transcript:
                        clean = re.sub(r"\[\d+\.\d+s\s*-\s*\d+\.\d+s\]\s*", "", raw_transcript)
                        transcripts[source_url] = clean.strip()
                        print(f"  ✓ @{_username_from_url(source_url)} ({len(clean)} chars)")
                    else:
                        print(f"  ✗ {url[:50]}... (blocked/failed)")
            except Exception as e:
                print(f"  ✗ {url[:50]}... ({e})")
    except Exception as e:
        print(f"[Apify] Transcriber error: {e}")

    # Step 2: For any that failed, try yt-dlp + Replicate Whisper
    failed_urls = [u for u in urls if not any(
        _video_id_from_url(u) in k for k in transcripts)]

    if failed_urls:
        print(f"[yt-dlp] Trying local download for {len(failed_urls)} blocked videos...")
        for url in failed_urls:
            transcript = _ytdlp_transcribe(url)
            if transcript:
                transcripts[url] = transcript
                print(f"  ✓ @{_username_from_url(url)} via yt-dlp ({len(transcript)} chars)")
            else:
                print(f"  ✗ {url[:50]}... (yt-dlp also failed — log into TikTok in Chrome)")

    return transcripts


def _ytdlp_transcribe(url):
    """Download TikTok video via yt-dlp (with browser cookies) and transcribe
    via Replicate Whisper. Returns transcript text or None."""
    import tempfile

    vid_id = _video_id_from_url(url) or "unknown"
    tmp_dir = tempfile.mkdtemp(prefix="tiktok_")
    audio_path = os.path.join(tmp_dir, f"{vid_id}.wav")

    # Download audio via yt-dlp with Chrome cookies
    try:
        r = subprocess.run(
            [sys.executable, "-m", "yt_dlp",
             "--cookies-from-browser", "chrome",
             "--impersonate", "chrome",
             "-x", "--audio-format", "wav",
             "-o", audio_path.replace(".wav", ".%(ext)s"),
             "--no-warnings", "--quiet",
             url],
            capture_output=True, text=True, timeout=60,
        )
        # yt-dlp may output to a slightly different filename
        wav_files = [f for f in os.listdir(tmp_dir) if f.endswith(".wav")]
        if r.returncode != 0 or not wav_files:
            return None
        audio_path = os.path.join(tmp_dir, wav_files[0])
    except Exception:
        return None

    # Transcribe via Replicate Whisper
    try:
        import replicate
        with open(audio_path, "rb") as f:
            output = replicate.run(
                "openai/whisper:8099696689d249cf8b122d833c36a26aed0b4e5a44f6026f29e543eadefbefb2",
                input={"audio": f, "model": "base", "language": "en"},
            )
        transcript = output.get("transcription", "")
        return transcript.strip() if transcript else None
    except Exception:
        return None
    finally:
        # Cleanup
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _get_cached_transcripts():
    """Load all previously transcribed videos from reference files and training data.
    Returns dict of {video_id: transcript}."""
    cached = {}

    # Check reference JSON files
    for ref_file in RAW_DIR.glob("*_tiktok_references.json"):
        try:
            data = json.loads(ref_file.read_text())
            for item in data:
                vid_id = _video_id_from_url(item.get("url", ""))
                transcript = item.get("transcript", "")
                if vid_id and transcript and len(transcript) > 30:
                    cached[vid_id] = transcript
        except Exception:
            pass

    # Check training data
    for tf_path in [TRAINING_FILE, Path(__file__).parent / "manager_scripts.json"]:
        if tf_path.exists():
            try:
                data = json.loads(tf_path.read_text())
                for s in data.get("scripts", []):
                    vid_id = s.get("video_id", "") or _video_id_from_url(s.get("url", ""))
                    transcript = s.get("transcript", "")
                    if vid_id and transcript and len(transcript) > 30:
                        cached[vid_id] = transcript
            except Exception:
                pass

    return cached


def _username_from_url(url):
    m = re.search(r"@([\w.]+)", url)
    return m.group(1) if m else ""


def _video_id_from_url(url):
    """Extract the numeric video ID from a TikTok URL."""
    m = re.search(r"/video/(\d+)", url)
    return m.group(1) if m else ""


# Known brand / common-word handles that trip up @mention extraction.
# These show up in captions but aren't actual creator peers we want in the pool.
_DISCOVERY_BLACKLIST = {
    # Platforms / generic
    "tiktok", "instagram", "youtube", "snapchat", "twitter", "x",
    "fyp", "foryoupage", "official", "creator",
    # Partial word captures ("too much", "fashion week", etc.)
    "too", "lor", "louis", "much", "makeup", "fashion", "beauty", "style",
    "fit", "ootd", "food", "life", "mom", "dad", "wife", "husband",
    # Makeup / beauty brands commonly tagged
    "maybelline", "sephora", "kosas", "makeupforever", "ulta", "loreal",
    "mac", "nyx", "morphe", "rarebeauty", "fentybeauty", "fenty", "dior",
    "chanel", "nars", "benefit", "elf", "elfcosmetics", "glossier",
    "kylie", "kyliecosmetics", "kkw", "laneige", "urbandecay", "mario",
    "haus", "toofaced", "charlotte", "tarte", "hourglass", "dyson",
    # Fashion / lifestyle brands
    "gucci", "prada", "lululemon", "nike", "adidas", "shein", "zara",
    "hm", "forever21", "target", "walmart", "amazon", "amazonfashion",
    "skims", "fabletics", "abercrombie",
}


def _looks_like_creator_handle(handle):
    """Heuristic: does this look like an individual creator handle vs a brand/word?
    Handles with digits/underscores/dots are almost always real TikTok handles.
    Pure lowercase short words are usually partial-word captures or brands.
    """
    if len(handle) < 4:
        return False
    if handle in _DISCOVERY_BLACKLIST:
        return False
    # Has non-letter char = likely real handle
    if any(c in handle for c in "0123456789_."):
        return True
    # Pure letters, length 4-8: suspicious (could be a brand or common word)
    # Require at least 7 chars if no distinguishing characters
    return len(handle) >= 7


def discover_via_creator(seed_username, token, max_videos=10, max_new=15,
                         use_mentions=True, use_comments=True, use_music=True):
    """Multi-method discovery: scrape a seed creator and find similar creators via
    (a) @mentions in captions, (b) commenters with their own following,
    (c) videos using the same trending sounds.

    Each method contributes independent signal. Returns total new creators added.
    """
    seed = seed_username.lstrip("@").strip().lower()
    if not seed:
        return 0

    api_key = get_apify_key()
    if not api_key:
        send_telegram(token, TELEGRAM_CHAT_ID, "Need Apify key for /discover.")
        return 0

    try:
        from apify_client import ApifyClient
        from memory_bridge import load_memory, save_memory
    except Exception as e:
        send_telegram(token, TELEGRAM_CHAT_ID, f"Dependency missing: {e}")
        return 0

    send_telegram(token, TELEGRAM_CHAT_ID,
                  f"🔍 Discovering via @{seed} — mentions + commenters + trending sounds...")

    client = ApifyClient(api_key)

    # ── Scrape the seed's recent videos once (shared across methods) ──
    try:
        run = client.actor("clockworks/tiktok-scraper").call(
            run_input={
                "profiles": [f"https://www.tiktok.com/@{seed}"],
                "resultsPerPage": max_videos,
                "shouldDownloadVideos": False,
                "shouldDownloadCovers": False,
            },
            timeout_secs=120,
        )
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
    except Exception as e:
        send_telegram(token, TELEGRAM_CHAT_ID, f"⚠️ Couldn't scrape @{seed}: {e}")
        return 0

    if not items:
        send_telegram(token, TELEGRAM_CHAT_ID, f"No videos found for @{seed}")
        return 0

    mem = load_memory()
    creators = mem.setdefault("creators", {})
    now = datetime.now().isoformat()
    results = {"mention": [], "commenter": [], "music": []}
    skip_blacklist = _DISCOVERY_BLACKLIST | {seed}

    # ── Method 1: @mentions (with tightened filter) ──
    if use_mentions:
        MENTION_RE = re.compile(r"@([A-Za-z0-9._]{4,24})")
        found = {}
        for item in items:
            caption = item.get("text", "") or ""
            for handle in MENTION_RE.findall(caption):
                h = handle.lower()
                if h in skip_blacklist:
                    continue
                if not _looks_like_creator_handle(h):
                    continue
                found[h] = found.get(h, 0) + 1
        for handle, count in sorted(found.items(), key=lambda x: -x[1])[:max_new]:
            if handle in creators:
                continue
            creators[handle] = {
                "platform": "tiktok", "source": "discovered_via_mention",
                "discovered": now, "last_seen": now,
                "discovered_via": seed, "mention_count": count,
            }
            results["mention"].append((handle, f"{count}× mentioned"))

    # ── Method 2: commenters (filtered by follower count) ──
    if use_comments:
        try:
            # Take top 3 most-viewed videos from seed for comment scraping
            top_videos = sorted(items, key=lambda v: v.get("playCount", 0), reverse=True)[:3]
            video_urls = [v.get("webVideoUrl") for v in top_videos if v.get("webVideoUrl")]
            if video_urls:
                run = client.actor("clockworks/tiktok-comments-scraper").call(
                    run_input={
                        "postURLs": video_urls,
                        "commentsPerPost": 50,
                        "maxRepliesPerComment": 0,
                    },
                    timeout_secs=180,
                )
                comments = list(client.dataset(run["defaultDatasetId"]).iterate_items())
                # Filter commenters: must have real following (>1000) and not already known
                by_commenter = {}
                for c in comments:
                    author = c.get("author") or c.get("user") or {}
                    handle = (author.get("name") or author.get("uniqueId") or "").lower().strip()
                    fans = author.get("fans") or author.get("followerCount") or 0
                    if not handle or handle in skip_blacklist or handle in creators:
                        continue
                    if fans < 1000:  # screens out horny-guy commenters and bots
                        continue
                    if not _looks_like_creator_handle(handle):
                        continue
                    prev = by_commenter.get(handle, {"fans": 0, "count": 0})
                    by_commenter[handle] = {
                        "fans": max(prev["fans"], fans),
                        "count": prev["count"] + 1,
                    }
                # Rank by follower count × comment frequency
                ranked = sorted(
                    by_commenter.items(),
                    key=lambda x: -(x[1]["fans"] * x[1]["count"]),
                )[:max_new]
                for handle, meta in ranked:
                    if handle in creators:
                        continue
                    creators[handle] = {
                        "platform": "tiktok", "source": "discovered_via_commenter",
                        "discovered": now, "last_seen": now,
                        "discovered_via": seed,
                        "commenter_fans": meta["fans"],
                        "comment_count": meta["count"],
                    }
                    results["commenter"].append(
                        (handle, f"{meta['fans']:,} fans, {meta['count']}× commented")
                    )
        except Exception as e:
            print(f"[Discover] Commenter scrape failed: {e}")

    # ── Method 3: trending sounds (skipped for original-sound-only creators) ──
    if use_music:
        try:
            # Find sounds used by the seed that are NOT their own original
            sound_urls = []
            for item in items:
                music = item.get("musicMeta") or {}
                if music.get("musicOriginal"):
                    continue  # seed's own sound, no other creators use it
                music_id = music.get("musicId")
                if music_id:
                    sound_urls.append(f"https://www.tiktok.com/music/-{music_id}")
            sound_urls = list(dict.fromkeys(sound_urls))[:3]  # dedupe, cap at 3

            if sound_urls:
                run = client.actor("clockworks/tiktok-sound-scraper").call(
                    run_input={"musicURLs": sound_urls, "resultsPerPage": 20},
                    timeout_secs=180,
                )
                sound_videos = list(client.dataset(run["defaultDatasetId"]).iterate_items())
                by_handle = {}
                for v in sound_videos:
                    author = v.get("authorMeta") or {}
                    handle = (author.get("name") or "").lower().strip()
                    fans = author.get("fans") or 0
                    if not handle or handle in skip_blacklist or handle in creators:
                        continue
                    if fans < 1000:
                        continue
                    if not _looks_like_creator_handle(handle):
                        continue
                    prev = by_handle.get(handle, {"fans": 0, "videos": 0})
                    by_handle[handle] = {
                        "fans": max(prev["fans"], fans),
                        "videos": prev["videos"] + 1,
                    }
                ranked = sorted(
                    by_handle.items(),
                    key=lambda x: -x[1]["fans"],
                )[:max_new]
                for handle, meta in ranked:
                    if handle in creators:
                        continue
                    creators[handle] = {
                        "platform": "tiktok", "source": "discovered_via_sound",
                        "discovered": now, "last_seen": now,
                        "discovered_via": seed,
                        "sound_fans": meta["fans"],
                    }
                    results["music"].append((handle, f"{meta['fans']:,} fans"))
        except Exception as e:
            print(f"[Discover] Sound scrape failed: {e}")

    save_memory(mem)

    total_added = sum(len(v) for v in results.values())
    if total_added == 0:
        send_telegram(token, TELEGRAM_CHAT_ID,
                      f"Scanned @{seed} across 3 methods — no new creators found. "
                      f"Either they don't interact publicly, or all discoveries "
                      f"are already in your pool.")
    else:
        lines = [f"🎯 Discovered {total_added} new creators via @{seed}:"]
        if results["mention"]:
            lines.append(f"\n<b>From @mentions ({len(results['mention'])}):</b>")
            lines += [f"• @{h} ({detail})" for h, detail in results["mention"]]
        if results["commenter"]:
            lines.append(f"\n<b>From commenters ({len(results['commenter'])}):</b>")
            lines += [f"• @{h} ({detail})" for h, detail in results["commenter"]]
        if results["music"]:
            lines.append(f"\n<b>From shared sounds ({len(results['music'])}):</b>")
            lines += [f"• @{h} ({detail})" for h, detail in results["music"]]
        lines.append(f"\nPool size now: {len(creators)}")
        send_telegram(token, TELEGRAM_CHAT_ID, "\n".join(lines))
    print(f"[Discover] via @{seed}: +{total_added} new creators")
    return total_added


def discover_weekly(token, top_n=5):
    """P2: Weekly auto-discovery. Runs discover_via_creator on your top N
    performers (by filmed-script count), sending a summary message.
    """
    try:
        from memory_bridge import load_memory
    except Exception:
        return 0

    mem = load_memory()
    filmed_by_creator = {}
    for s in mem.get("scripts", []):
        if s.get("filmed"):
            u = s.get("username", "")
            if u:
                filmed_by_creator[u] = filmed_by_creator.get(u, 0) + 1

    if not filmed_by_creator:
        inbox_creators = [u for u, meta in mem.get("creators", {}).items()
                          if meta.get("source") == "manager_inbox"]
        top_creators = inbox_creators[:top_n]
    else:
        top_creators = [u for u, _ in sorted(
            filmed_by_creator.items(), key=lambda x: -x[1])[:top_n]]

    if not top_creators:
        send_telegram(token, TELEGRAM_CHAT_ID,
                      "No creators to discover from yet. Film some scripts first.")
        return 0

    send_telegram(token, TELEGRAM_CHAT_ID,
                  f"📅 Weekly discovery starting — expanding from {len(top_creators)} "
                  f"top creators: @{', @'.join(top_creators)}")

    total_added = 0
    for seed in top_creators:
        total_added += discover_via_creator(seed, token, max_videos=10, max_new=10)

    send_telegram(token, TELEGRAM_CHAT_ID,
                  f"✅ Weekly discovery done. Added {total_added} new creators. "
                  f"They'll appear in upcoming research runs.")
    return total_added


def _remember_creators_from_refs(tiktok_data, source="manager_inbox"):
    """Add creators from scraped TikTok data into the shared memory pool.

    Every time the manager sends TikTok URLs, the creators behind those
    videos get added to the rotation pool so future research runs can scrape
    them too. Existing creators get their `last_seen` refreshed.
    """
    if not tiktok_data:
        return 0
    try:
        from memory_bridge import load_memory, save_memory
    except Exception:
        return 0
    mem = load_memory()
    creators = mem.setdefault("creators", {})
    now = datetime.now().isoformat()
    added, refreshed = 0, 0
    for item in tiktok_data:
        username = (item.get("username") or "").strip()
        if not username or len(username) < 3:
            continue
        if username in creators:
            creators[username]["last_seen"] = now
            refreshed += 1
        else:
            creators[username] = {
                "platform": "tiktok",
                "source": source,
                "discovered": now,
                "last_seen": now,
                "nickname": item.get("display_name", ""),
            }
            added += 1
    if added or refreshed:
        save_memory(mem)
        print(f"[Memory] Creators: +{added} new, {refreshed} refreshed")
    return added


def _diversify_by_creator(items, get_username, limit=None):
    """Round-robin items by creator so digests don't over-represent one account.

    Groups items by creator, then pulls one from each creator in turn,
    preserving the original encounter order within each group. If ``limit``
    is given, stops once that many items have been collected.
    """
    from collections import OrderedDict
    by_creator = OrderedDict()
    for item in items:
        u = get_username(item) or ""
        by_creator.setdefault(u, []).append(item)

    result = []
    while any(by_creator.values()):
        for u in list(by_creator.keys()):
            if by_creator[u]:
                result.append(by_creator[u].pop(0))
                if limit and len(result) >= limit:
                    return result
    return result


# ──────────────────────────────────────────
# Script generation from TikTok references
# ──────────────────────────────────────────

def generate_scripts_from_refs(tiktok_data):
    """Turn scraped TikTok references into actionable recording scripts.

    Format: script dialogue in <pre> code blocks, everything else regular text.
    Matches the existing Fanuploader bot teleprompter format.
    """
    now = datetime.now()
    date_str = now.strftime("%B %d")

    msg = f"Here's the teleprompter content for your video shoots:\n\n"
    msg += f"Based on {len(tiktok_data)} reference videos\n"
    msg += "______\n\n"

    for i, vid in enumerate(tiktok_data, 1):
        username = vid.get("username", "unknown")
        desc = vid.get("description", "") or ""
        hashtags = vid.get("hashtags", [])
        views = vid.get("stats", {}).get("views", 0)
        likes = vid.get("stats", {}).get("likes", 0)
        duration = vid.get("duration", 0)
        music = vid.get("music", "")
        url = vid.get("url", "")

        transcript = vid.get("transcript", "") or ""

        # --- Build the script dialogue and directions ---
        script_text, shot, caption = _build_vanilla_script(desc, hashtags, duration, music, transcript)

        msg += f"=== SCRIPT {i} ===\n"
        if username:
            msg += f"Ref: @{username}"
            stats_parts = []
            if views:
                stats_parts.append(f"{views:,} views")
            if likes:
                stats_parts.append(f"{likes:,} likes")
            if stats_parts:
                msg += f" ({', '.join(stats_parts)})"
            msg += "\n"
        if desc:
            desc_clean = desc[:150].replace("<", "&lt;").replace(">", "&gt;")
            msg += f"Original: {desc_clean}\n"
        msg += "\n"

        # Script dialogue in code block (only if we have a real transcript)
        if script_text:
            msg += f"<pre>{_escape_html(script_text)}</pre>\n\n"
        else:
            msg += "⚠️ Couldn't transcribe — watch the reference and recreate in your style\n\n"

        # Shot + caption as regular text
        msg += f"Shot: {shot}\n"
        msg += f"Caption: \"{caption}\"\n"
        if music:
            msg += f"Sound: {music}\n"
        if url:
            msg += f"Ref: {url}\n"

        msg += "\n---\n\n"

    msg += "Reply with the script # you filmed (e.g. \"1\" or \"1,3\")\n"
    msg += "Reply SKIP if none work today."

    return msg


def _escape_html(text):
    """Escape HTML special chars for Telegram."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _build_vanilla_script(desc, hashtags, duration, music, transcript=""):
    """Build vanilla script: returns (dialogue, shot_direction, caption).

    If a transcript is available, use it directly as the script dialogue.
    Otherwise fall back to generating from metadata.
    """
    dur = duration if duration else 30

    # If we have a transcript, use it as the script
    if transcript and len(transcript) > 20:
        dialogue = f"\"{transcript}\""
        shot = (
            f"Recreate this video, {dur}s. "
            f"Talk to camera, match the energy and delivery of the original. "
            f"{'Use sound: ' + music if music else 'Original sound'}."
        )
        # Build caption from description/hashtags if available
        if desc and len(desc) > 5:
            caption = desc[:100]
        elif hashtags:
            caption = " ".join(f"#{h}" for h in hashtags[:4]) + " #fyp"
        else:
            caption = "#fyp #foryou"
        return dialogue, shot, caption

    # No transcript — don't fake a script, just say to watch the reference
    dialogue = None  # signals: no script available
    shot = f"Watch the reference video and recreate it in your style, {dur}s."
    if desc and len(desc) > 5:
        caption = desc[:100]
    elif hashtags:
        caption = " ".join(f"#{h}" for h in hashtags[:4]) + " #fyp"
    else:
        caption = "#fyp #foryou"

    return dialogue, shot, caption




# ──────────────────────────────────────────
# VPS trend data (for digest mode)
# ──────────────────────────────────────────

def pull_vps_data():
    """Pull trend-scout data from VPS."""
    data = {"videos": [], "scripts": [], "feedback": {}}

    r = ssh_run(f"cat {shlex.quote(VPS_TREND_SCOUT)}/trends_db.json 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        try:
            db = json.loads(r.stdout)
            data["videos"] = list(db.get("videos", {}).values())
        except json.JSONDecodeError:
            pass

    r = ssh_run(f"cat {shlex.quote(VPS_TREND_SCOUT)}/latest_scripts.json 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        try:
            data["scripts"] = json.loads(r.stdout).get("scripts", [])
        except json.JSONDecodeError:
            pass

    r = ssh_run(f"cat {shlex.quote(VPS_TREND_SCOUT)}/feedback.json 2>/dev/null")
    if r.returncode == 0 and r.stdout.strip():
        try:
            data["feedback"] = json.loads(r.stdout)
        except json.JSONDecodeError:
            pass

    return data


def format_digest(vps_data):
    """Format daily trend digest for Telegram."""
    now = datetime.now()
    day_name = now.strftime("%A")
    date_str = now.strftime("%B %d")

    scripts = vps_data.get("scripts", [])
    videos = vps_data.get("videos", [])
    feedback = vps_data.get("feedback", {})

    msg = f"<b>📈 Daily Trend Digest — {day_name}, {date_str}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

    # Trending hashtags
    if videos:
        from collections import Counter
        tags = Counter()
        for v in videos:
            for h in v.get("hashtags", []):
                tags[h.lower()] += 1
        if tags:
            top = " ".join(f"#{t}" for t, _ in tags.most_common(6))
            msg += f"<b>Trending:</b> {top}\n"

        # Top video
        top_vid = max(videos, key=lambda v: v.get("stats", {}).get("views", 0))
        views = top_vid.get("stats", {}).get("views", 0)
        user = top_vid.get("username", "?")
        msg += f"<b>Top:</b> @{user} — {views:,} views\n"

    if feedback.get("total_filmed", 0) > 0:
        rate = feedback["total_filmed"] / max(feedback.get("total_scripts", 1), 1) * 100
        msg += f"<b>Film rate:</b> {rate:.0f}%\n"

    msg += "\n"

    # Scripts from VPS — diversify so one creator can't take all 5 slots
    if scripts:
        picked = _diversify_by_creator(
            scripts, lambda s: s.get("username", ""), limit=5,
        )
        for i, s in enumerate(picked, 1):
            topic = s.get("topic", "") or s.get("category", "")
            what = s.get("what_worked", "")
            script = s.get("lana_script", "") or s.get("script", "") or ""

            msg += f"<b>📝 Script {i}</b>"
            if topic:
                msg += f" — <i>{topic}</i>"
            msg += "\n"
            if what:
                msg += f"<b>Why:</b> {what}\n"
            msg += "\n"
            if script:
                msg += textwrap.fill(script.strip(), width=80)
            else:
                msg += "<i>(no script text)</i>"
            msg += "\n\n"
    else:
        msg += "<i>No scripts from VPS today. Send me TikTok links to generate scripts!</i>\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<b>Reply with # you filmed</b> or send TikTok links for scripts.\n"
    msg += "Reply <b>SKIP</b> if none work today."

    return msg


# ──────────────────────────────────────────
# Polling: feedback + incoming TikTok links
# ──────────────────────────────────────────

# ──────────────────────────────────────────
# Conversational AI — edit scripts, build ideas
# ──────────────────────────────────────────

def _is_script_request(text):
    """Check if the message is asking for new scripts/transcripts."""
    t = text.lower()
    triggers = [
        "generate", "new script", "more script", "find me", "find script",
        "similar script", "give me script", "send me script", "research",
        "find more", "get me", "new transcript", "more transcript",
        "transcribe", "watch and transcribe", "similar to", "like these",
        "like the", "more like", "find similar", "script idea",
        "transcript", "give me more", "send more", "another script",
        "write me", "make me", "create script", "create more",
    ]
    return any(kw in t for kw in triggers)


def research_and_send_scripts(token, min_scripts=4):
    """Research TikTok for videos similar to manager's references, transcribe them,
    skip blocked ones, keep going until we have at least min_scripts transcripts.
    Sends real transcripts — not AI-generated content."""

    api_key = get_apify_key()
    if not api_key:
        send_telegram(token, TELEGRAM_CHAT_ID, "Need Apify key to research TikTok.")
        return

    try:
        from apify_client import ApifyClient
    except ImportError as e:
        send_telegram(token, TELEGRAM_CHAT_ID, f"apify-client not installed: {e}. Python: {sys.executable}")
        return
    except Exception as e:
        send_telegram(token, TELEGRAM_CHAT_ID, f"apify import error: {e}")
        return

    client = ApifyClient(api_key)

    # Get ALL reference creators from shared memory + training data
    all_usernames = set()

    # From shared memory
    try:
        from memory_bridge import load_memory
        mem = load_memory()
        for u in mem.get("creators", {}).keys():
            if u and len(u) > 2:
                all_usernames.add(u)
    except Exception:
        pass

    # From training data
    recent = _get_recent_scripts()
    for s in recent:
        u = s.get("username", "")
        if u and len(u) > 2:
            all_usernames.add(u)

    # Skip known-blocked accounts
    skip = {"victoriaantoinet", "lexarxriqnt"}
    usernames = [u for u in all_usernames if u not in skip]

    if not usernames:
        send_telegram(token, TELEGRAM_CHAT_ID,
                      "No reference creators yet. Send me TikTok links first so I know what style to find.")
        return

    # Rotate creators — sample more than we need so that seen_ids filtering
    # still leaves enough creators with fresh content. 12 is the sweet spot:
    # we usually only need 4 scripts, and historically ~50% of sampled creators
    # have at least one fresh video after the seen_ids filter.
    import random
    if len(usernames) > 12:
        usernames = random.sample(usernames, 12)

    send_telegram(token, TELEGRAM_CHAT_ID,
                  f"🔍 Researching TikTok — scanning @{', @'.join(usernames)} for new videos...")

    # Scrape recent videos from reference creators
    all_video_urls = []
    try:
        print(f"[Research] Scraping profiles: {usernames}")
        run_input = {
            "profiles": [f"https://www.tiktok.com/@{u}" for u in usernames],
            "resultsPerPage": 10,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        }
        run = client.actor("clockworks/tiktok-scraper").call(run_input=run_input, timeout_secs=120)
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())
        print(f"[Research] Found {len(items)} videos from {len(usernames)} creators")

        # Get URLs, skip ones we've already seen
        training = _load_training()
        seen_ids = set(training.get("seen_ids", []))

        fresh_items = []
        for item in items:
            vid_url = item.get("webVideoUrl", "")
            vid_id = _video_id_from_url(vid_url)
            if vid_id and vid_id not in seen_ids and vid_url:
                fresh_items.append(item)

        # Interleave by creator so every digest hits diverse accounts
        diversified = _diversify_by_creator(
            fresh_items,
            lambda it: _username_from_url(it.get("webVideoUrl", "")),
        )
        all_video_urls = [it.get("webVideoUrl", "") for it in diversified]

    except Exception as e:
        print(f"[Research] Profile scrape failed: {e}")
        send_telegram(token, TELEGRAM_CHAT_ID, f"⚠️ Couldn't scrape profiles: {e}")
        return

    if not all_video_urls:
        send_telegram(token, TELEGRAM_CHAT_ID,
                      "No new videos found from reference creators. They may not have posted recently.")
        return

    print(f"[Research] {len(all_video_urls)} new videos to try transcribing")

    # Transcribe one by one, skip blocked, stop when we have enough.
    # Strict creator diversity: skip a creator who already contributed a
    # successful script, UNTIL every creator has been attempted at least once.
    # Then (and only then) allow repeats to fill remaining slots.
    transcribed = []
    creators_used = set()
    creators_attempted = set()
    all_creators = {_username_from_url(u) for u in all_video_urls if _username_from_url(u)}

    for url in all_video_urls:
        if len(transcribed) >= min_scripts:
            break

        creator = _username_from_url(url)
        if creator in creators_used and creators_attempted != all_creators:
            continue

        creators_attempted.add(creator)
        print(f"[Research] Transcribing: {url[:60]}...")
        try:
            run_input = {"start_urls": url}
            run = client.actor("tictechid/anoxvanzi-transcriber").call(
                run_input=run_input, timeout_secs=120)
            items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

            for item in items:
                raw = item.get("transcript", "")
                status = item.get("status", "")
                source = item.get("sourceUrl", url)

                if status == "success" and raw and len(raw) > 30:
                    clean = re.sub(r"\[\d+\.\d+s\s*-\s*\d+\.\d+s\]\s*", "", raw).strip()
                    transcribed.append({
                        "url": source,
                        "username": _username_from_url(source),
                        "transcript": clean,
                        "duration": item.get("durationSec", 0),
                    })
                    creators_used.add(creator)
                    print(f"  ✓ ({len(clean)} chars)")
                else:
                    print(f"  ✗ blocked/failed, skipping")
        except Exception as e:
            print(f"  ✗ error: {e}, skipping")

    if not transcribed:
        send_telegram(token, TELEGRAM_CHAT_ID,
                      "Couldn't transcribe any new videos — all were blocked. Try sending different creator links.")
        return

    # Format and send
    msg = f"<b>🎬 Found {len(transcribed)} new scripts from research</b>\n"
    msg += f"<i>From: @{', @'.join(set(t['username'] for t in transcribed if t['username']))}</i>\n"
    msg += "______\n\n"

    for i, t in enumerate(transcribed, 1):
        msg += f"=== SCRIPT {i} ===\n"
        if t["username"]:
            msg += f"Ref: @{t['username']}\n"
        msg += "\n"
        msg += f"<pre>{_escape_html(t['transcript'])}</pre>\n\n"
        msg += f"Shot: Recreate this, {int(t.get('duration', 15))}s. Talk to camera, match the energy.\n"
        if t["url"]:
            msg += f"Ref: {t['url']}\n"
        msg += "\n---\n\n"

    msg += f"<b>Reply with the # you want to film</b>"

    send_telegram(token, TELEGRAM_CHAT_ID, msg)

    # Save to training
    for t in transcribed:
        vid_id = _video_id_from_url(t.get("url", ""))
        if vid_id:
            training = _load_training()
            training.setdefault("scripts", []).append({
                "video_id": vid_id,
                "url": t["url"],
                "username": t["username"],
                "transcript": t["transcript"],
                "duration": t.get("duration", 0),
                "date_added": datetime.now().isoformat(),
                "filmed": False,
                "source": "research",
            })
            training.setdefault("seen_ids", []).append(vid_id)
            training["total_scripts"] = len(training["scripts"])
            training["last_updated"] = datetime.now().isoformat()
            _save_training(training)

    print(f"[Research] Sent {len(transcribed)} scripts, saved to training")


def chat_with_ai(user_message, token):
    """Handle free-text messages. If asking for scripts, research TikTok.
    Otherwise use GPT for conversation."""
    # If asking for scripts, do real TikTok research instead of GPT
    if _is_script_request(user_message):
        research_and_send_scripts(token)
        return None  # already sent via research function

    api_key = OPENAI_API_KEY
    if not api_key:
        return ("💬 I got your message but I need an OpenAI key to chat.\n"
                "Add OPENAI_API_KEY to telegram_bot.env on the VPS.")

    # Load recent scripts for context
    recent_scripts = _get_recent_scripts()
    chat_history = _load_chat_history()

    # Build the system prompt with real examples
    recent_scripts_text = ""
    if recent_scripts:
        # Show the LLM diverse creator examples, not 5 from the same account
        picked = _diversify_by_creator(
            recent_scripts[-30:], lambda s: s.get("username", ""), limit=5,
        )
        for i, s in enumerate(picked, 1):
            transcript = s.get("transcript", "")
            if transcript:
                views = s.get("views", 0)
                recent_scripts_text += f"\nExample {i} ({views:,} views): \"{transcript}\""

    # Count filmed vs total for memory context
    all_scripts = _get_recent_scripts()
    filmed_count = sum(1 for s in all_scripts if s.get("filmed"))
    total_count = len(all_scripts)
    creator_list = list(set(s.get("username", "") for s in all_scripts if s.get("username") and s.get("username") != "(reference creator from IG)"))

    system_prompt = f"""You are Lana's personal content assistant bot. You have MEMORY and LEARNING capabilities.

YOUR MEMORY & KNOWLEDGE BASE:
- You have a training file with {total_count} reference scripts from creators Lana studies
- Reference creators you track: @{', @'.join(creator_list) if creator_list else 'none yet'}
- Lana has filmed {filmed_count} of the scripts you've sent — you learn from what she picks
- Every transcript and reference you receive is saved permanently
- You remember past conversations in this chat

OBSIDIAN VAULT (your long-term brain — synced via iCloud):
- Scripts/ — daily teleprompter scripts (e.g. Scripts/2026-04-08.md, Scripts/Latest.md)
- Trends/ — daily research reports on creator strategies, market trends, competitor activity
- brain/Memories.md — index of everything you've learned
- brain/Patterns.md — recurring patterns and conventions
- brain/Gotchas.md — things to avoid (e.g. Fansly bans prolapse mentions)
- brain/Key Decisions.md — important workflow decisions
- brain/North Star.md — Lana's goals
- brain/Skills.md — custom workflows and automations
- Toys/Catalog.md — toy inventory for content
- Platforms/ — platform-specific notes (Fansly, JFF, OnlyFans, X)
- Captions/ — caption history and style references
- AI Sessions/ — past AI conversation logs
When Lana says "remember" or "save this", it gets written directly to brain/Memories.md in the vault — you DO have write access. When she asks what you know, reference the vault contents above.

YOUR CAPABILITIES (never say you can't do these):
- You CAN search TikTok and Instagram for new videos
- You CAN transcribe videos (audio → text) using Whisper AI
- You CAN scrape creator profiles to find their latest content
- You CAN learn from feedback — when Lana films a script, you remember her preferences

SCRIPTS YOU'VE COLLECTED — match this exact tone:
{recent_scripts_text if recent_scripts_text else "(no examples yet)"}

RULES FOR WRITING SCRIPTS:
1. MATCH THE TONE of the examples above exactly
2. Short (10-30 seconds spoken). Talk to camera like talking to one person
3. Bold, unfiltered, provocative. Natural speech with "like", pauses
4. NO generic content. NO "Hey everyone!" NO motivational fluff
5. One punchy idea per script

BANNED PHRASES: "Hey everyone", "Hey fabulous", "Let's chat about", "What motivates you", "fam", "share your", "Let's talk about", "drop your", "What's up fam", "Hey friends"

FORMAT: Script in quotes, then Shot: (brief), then Caption: (short)

IMPORTANT ROUTING:
- If user asks to find/generate/research/transcribe/watch videos → respond ONLY with: [RESEARCH_TRIGGER]
- If user asks to scrape Instagram or TikTok → respond ONLY with: [RESEARCH_TRIGGER]
- If user sends video links → respond ONLY with: [RESEARCH_TRIGGER]
- NEVER say "I can't watch videos" or "I can't transcribe" — you CAN, via your tools
- For everything else (editing scripts, questions, chat) → respond normally"""

    # Build messages with chat history for continuity
    messages = [{"role": "system", "content": system_prompt}]
    for entry in chat_history[-10:]:  # last 10 exchanges
        messages.append({"role": entry["role"], "content": entry["content"]})
    messages.append({"role": "user", "content": user_message})

    # Call OpenAI
    try:
        import urllib.request
        req_data = json.dumps({
            "model": "gpt-5.4-mini",
            "messages": messages,
            "max_completion_tokens": 1000,
            "temperature": 0.7,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=req_data,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
        )
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        ai_response = result["choices"][0]["message"]["content"].strip()

        # If GPT detected a research request, trigger the research pipeline
        if "[RESEARCH_TRIGGER]" in ai_response:
            research_and_send_scripts(token)
            return None

        # Save to chat history
        chat_history.append({"role": "user", "content": user_message})
        chat_history.append({"role": "assistant", "content": ai_response})
        # Keep last 20 exchanges
        _save_chat_history(chat_history[-40:])

        # Format: put any quoted script text in code blocks for Telegram
        formatted = _format_ai_response(ai_response)
        return formatted

    except Exception as e:
        print(f"OpenAI error: {e}")
        return f"⚠️ AI error: {e}"


def _format_ai_response(text):
    """Convert quoted scripts in AI response to Telegram code blocks."""
    # Find text between quotes that looks like a script (multi-line or long)
    def replace_script(match):
        script = match.group(1)
        if len(script) > 40:
            escaped = _escape_html(script)
            return f"<pre>{escaped}</pre>"
        return f'"{script}"'

    formatted = re.sub(r'"([^"]{40,})"', replace_script, text)
    # Also handle scripts in triple backticks
    formatted = re.sub(r'```\n?(.*?)\n?```', lambda m: f"<pre>{_escape_html(m.group(1))}</pre>",
                       formatted, flags=re.DOTALL)
    return formatted


def _get_recent_scripts():
    """Get recent scripts from training data (local or VPS)."""
    training = _load_training()
    scripts = training.get("scripts", [])

    # Also check VPS manager_scripts.json (where training syncs to)
    if not scripts:
        vps_path = Path(__file__).parent / "trend-scout" / "manager_scripts.json"
        if vps_path.exists():
            try:
                vps_data = json.loads(vps_path.read_text())
                scripts = vps_data.get("scripts", [])
            except Exception:
                pass

    # Also check alongside the script itself (for VPS deployment)
    if not scripts:
        local_path = Path(__file__).parent / "manager_scripts.json"
        if local_path.exists():
            try:
                local_data = json.loads(local_path.read_text())
                scripts = local_data.get("scripts", [])
            except Exception:
                pass

    # Diversify the recent window so callers don't get 5 scripts from one creator
    return _diversify_by_creator(
        scripts[-30:], lambda s: s.get("username", ""), limit=5,
    )


def _load_chat_history():
    CHAT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    if CHAT_HISTORY_FILE.exists():
        try:
            return json.loads(CHAT_HISTORY_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return []


def _save_chat_history(history):
    CHAT_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHAT_HISTORY_FILE.write_text(json.dumps(history, indent=2, default=str))


# ──────────────────────────────────────────
# Memory: save to Obsidian vault
# ──────────────────────────────────────────

def _is_memory_request(text):
    """Check if the message is asking to save/remember something."""
    t = text.lower()
    triggers = ["remember", "save this", "note this", "store this", "add to memory",
                "don't forget", "keep track", "log this", "write this down",
                "add to vault", "save to vault", "update memory"]
    return any(kw in t for kw in triggers)


def save_to_vault_memory(text, token):
    """Save a note to the Obsidian vault brain/Memories.md."""
    from datetime import datetime as _dt
    timestamp = _dt.now().strftime("%Y-%m-%d %H:%M")

    # Clean up the text — remove the "remember" trigger words
    note = text
    for word in ["remember this", "remember", "save this", "note this",
                 "store this", "don't forget", "keep track of"]:
        note = re.sub(re.escape(word), "", note, flags=re.IGNORECASE).strip()
    note = note.strip(" :-–—")

    if not note or len(note) < 3:
        send_telegram(token, TELEGRAM_CHAT_ID, "What should I remember? Send the text after 'remember'.")
        return

    # Write to vault on VPS
    vault_base = os.environ.get("VAULT_PATH")
    if not vault_base:
        return  # No vault configured, skip
    vault_path = str(Path(vault_base) / "brain" / "Memories.md")
    entry = f"\n- [{timestamp}] {note}"

    try:
        if VPS_HOST == "localhost" or "VPS_HOST" not in os.environ:
            # Running on VPS directly
            from pathlib import Path as _P
            mem_file = _P(vault_path)
            if mem_file.exists():
                content = mem_file.read_text()
                # Add under "## Recent Context" section
                if "## Recent Context" in content:
                    content = content.replace("## Recent Context\n", f"## Recent Context\n{entry}\n", 1)
                else:
                    content += f"\n## Recent Context\n{entry}\n"
                mem_file.write_text(content)
            else:
                mem_file.write_text(f"# Memories\n\n## Recent Context\n{entry}\n")
        else:
            # Running remotely — use SSH
            ssh_run(f"echo {shlex.quote(entry)} >> {shlex.quote(vault_path)}")

        send_telegram(token, TELEGRAM_CHAT_ID,
                      f"✅ Saved to memory:\n\n<i>{note}</i>\n\nStored in brain/Memories.md")
    except Exception as e:
        send_telegram(token, TELEGRAM_CHAT_ID, f"⚠️ Couldn't save to vault: {e}")


# ──────────────────────────────────────────
# Polling: feedback + incoming TikTok links + chat
# ──────────────────────────────────────────

def process_updates(token):
    """Check for feedback replies AND incoming TikTok links."""
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    feedback = _load_feedback()
    updates = get_updates(token, offset=feedback.get("last_update_id", 0) + 1)

    new_links = []
    for update in updates:
        update_id = update.get("update_id", 0)
        feedback["last_update_id"] = max(feedback.get("last_update_id", 0), update_id)

        msg = update.get("message", {})
        text = (msg.get("text", "") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(TELEGRAM_CHAT_ID):
            continue

        # Check for TikTok links
        found_urls = TIKTOK_URL_RE.findall(text)
        if found_urls:
            new_links.extend(found_urls)
            send_telegram(token, TELEGRAM_CHAT_ID,
                          f"🔗 Got {len(found_urls)} TikTok link(s). Scraping and generating scripts...")
            continue

        # Feedback: SKIP
        if text.upper() == "SKIP":
            feedback["skipped_days"] = feedback.get("skipped_days", 0) + 1
            send_telegram(token, TELEGRAM_CHAT_ID,
                          "👍 Skipped. Send more links or wait for tomorrow's digest.")

        # Feedback: script numbers (e.g. "1" or "1,3")
        elif re.match(r"^[\d,\s]+$", text):
            nums = [int(n.strip()) for n in text.replace(" ", ",").split(",") if n.strip().isdigit()]
            for n in nums:
                feedback["total_filmed"] = feedback.get("total_filmed", 0) + 1
                feedback.setdefault("filmed", []).append({
                    "script_num": n, "date": datetime.now().isoformat()})
            _mark_filmed(nums)
            total = feedback.get("total_filmed", 0)
            send_telegram(token, TELEGRAM_CHAT_ID,
                          f"🎬 Recorded script(s) #{', '.join(str(n) for n in nums)}! "
                          f"Total filmed: {total} — training updated")

        # Memory: save/remember requests
        elif _is_memory_request(text):
            save_to_vault_memory(text, token)
            continue

        # Calendar: "what should I film", "calendar", "what to film today"
        elif any(kw in text.lower() for kw in ["calendar", "what should i film", "what to film", "film today", "what to record"]):
            send_telegram(token, TELEGRAM_CHAT_ID, "📅 Building your content calendar...")
            try:
                import subprocess as _sp
                _cmd = os.environ.get("CALENDAR_CMD", f"{sys.executable} {Path(__file__).parent / 'calendar_agent.py'}").split()
                _sp.run(_cmd, timeout=60)
            except Exception as e:
                send_telegram(token, TELEGRAM_CHAT_ID, f"Calendar error: {e}")
            continue

        # Research: "what's trending", "competitor update", "research"
        elif any(kw in text.lower() for kw in ["trending", "competitor", "research update", "market update"]):
            send_telegram(token, TELEGRAM_CHAT_ID, "📊 Running research pipeline...")
            try:
                import subprocess as _sp
                _cmd = os.environ.get("AUTO_RESEARCH_CMD", f"{sys.executable} {Path(__file__).parent / 'auto_research.py'}").split()
                _sp.run(_cmd, timeout=180)
            except Exception as e:
                send_telegram(token, TELEGRAM_CHAT_ID, f"Research error: {e}")
            continue

        # Everything else → conversational AI (edit scripts, ask questions, build ideas)
        else:
            response = chat_with_ai(text, token)
            if response:
                send_telegram(token, TELEGRAM_CHAT_ID, response)

    _save_feedback(feedback)

    # Process any new TikTok links
    if new_links:
        print(f"Processing {len(new_links)} new TikTok links...")
        resolved = resolve_tiktok_urls(new_links)
        tiktok_data = scrape_tiktoks(resolved)
        # Grow the rotation pool with creators the manager is sending in
        _remember_creators_from_refs(tiktok_data)
        msg = generate_scripts_from_refs(tiktok_data)
        send_telegram(token, TELEGRAM_CHAT_ID, msg)

        # Save the reference data
        _save_references(tiktok_data)

    return len(updates), len(new_links)


def _load_feedback():
    if FEEDBACK_FILE.exists():
        try:
            return json.loads(FEEDBACK_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_update_id": 0, "filmed": [], "skipped_days": 0,
            "total_scripts": 0, "total_filmed": 0}


def _save_feedback(feedback):
    FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
    FEEDBACK_FILE.write_text(json.dumps(feedback, indent=2, default=str))


def _save_references(tiktok_data):
    """Save scraped TikTok references to raw/trends/ for the knowledge base."""
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    # Markdown version
    md_path = RAW_DIR / f"{ts}_tiktok_references.md"
    content = f"""---
source: manager_tiktok_references
category: scripts
ingested: {datetime.now().isoformat()}
---

# TikTok References — {datetime.now().strftime('%B %d, %Y')}

Manager-sent reference videos for script adaptation.

"""
    for vid in tiktok_data:
        username = vid.get("username", "?")
        desc = vid.get("description", "")
        views = vid.get("stats", {}).get("views", 0)
        url = vid.get("url", "")
        transcript = vid.get("transcript", "")
        content += f"## @{username}\n"
        content += f"- URL: {url}\n"
        if views:
            content += f"- Views: {views:,}\n"
        if desc:
            content += f"- Description: {desc[:200]}\n"
        if transcript:
            content += f"- Transcript: {transcript}\n"
        content += "\n"

    md_path.write_text(content)

    # JSON version
    json_path = RAW_DIR / f"{ts}_tiktok_references.json"
    json_path.write_text(json.dumps(tiktok_data, indent=2, default=str))
    print(f"Saved references: {md_path.name}, {json_path.name}")

    # Feed transcripts into training data
    _train_on_transcripts(tiktok_data)


def _train_on_transcripts(tiktok_data):
    """Save successful transcripts to training file.

    Builds up a library of real scripts the manager sends as references.
    This data feeds into:
    1. Better fallback scripts when transcription fails
    2. VPS trend-scout script generation (synced via kb_sync)
    3. Style/tone patterns for the caption generator
    """
    training = _load_training()

    new_count = 0
    for vid in tiktok_data:
        transcript = vid.get("transcript", "")
        if not transcript or len(transcript) < 20:
            continue

        vid_id = _video_id_from_url(vid.get("url", ""))
        if not vid_id:
            continue

        # Skip if we already have this video
        if vid_id in training.get("seen_ids", set()):
            continue

        entry = {
            "video_id": vid_id,
            "url": vid.get("url", ""),
            "username": vid.get("username", ""),
            "transcript": transcript,
            "views": vid.get("stats", {}).get("views", 0),
            "likes": vid.get("stats", {}).get("likes", 0),
            "duration": vid.get("duration", 0),
            "hashtags": vid.get("hashtags", []),
            "date_added": datetime.now().isoformat(),
            "filmed": False,  # updated when user replies with script #
        }

        training.setdefault("scripts", []).append(entry)
        training.setdefault("seen_ids", [])
        if vid_id not in training["seen_ids"]:
            training["seen_ids"].append(vid_id)
        new_count += 1

    if new_count:
        training["total_scripts"] = len(training.get("scripts", []))
        training["last_updated"] = datetime.now().isoformat()
        _save_training(training)
        print(f"Training: added {new_count} new scripts ({training['total_scripts']} total)")

        # Sync training data to VPS for the trend-scout to learn from
        _sync_training_to_vps(training)


def _load_training():
    TRAINING_FILE.parent.mkdir(parents=True, exist_ok=True)
    if TRAINING_FILE.exists():
        try:
            return json.loads(TRAINING_FILE.read_text())
        except json.JSONDecodeError:
            pass
    return {"scripts": [], "seen_ids": [], "total_scripts": 0}


def _save_training(training):
    TRAINING_FILE.parent.mkdir(parents=True, exist_ok=True)
    TRAINING_FILE.write_text(json.dumps(training, indent=2, default=str))


def _mark_filmed(script_nums):
    """Mark scripts as filmed in training data so the bot learns preferences."""
    training = _load_training()
    scripts = training.get("scripts", [])
    # Mark the most recent N scripts by position (script 1 = most recent batch[-N], etc.)
    # Scripts are appended chronologically, so the last batch is the most recent
    recent = scripts[-10:]  # last batch won't exceed 10
    for num in script_nums:
        idx = num - 1  # 1-indexed to 0-indexed
        if 0 <= idx < len(recent):
            actual_idx = len(scripts) - len(recent) + idx
            scripts[actual_idx]["filmed"] = True
    training["scripts"] = scripts
    _save_training(training)
    _sync_training_to_vps(training)


def _sync_training_to_vps(training):
    """Push training data to VPS so trend-scout can learn from it."""
    try:
        # Build a compact version for the VPS
        vps_data = {
            "total_scripts": training.get("total_scripts", 0),
            "scripts": [
                {
                    "transcript": s["transcript"],
                    "views": s.get("views", 0),
                    "likes": s.get("likes", 0),
                    "duration": s.get("duration", 0),
                    "filmed": s.get("filmed", False),
                    "username": s.get("username", ""),
                }
                for s in training.get("scripts", [])[-50:]  # last 50 scripts
            ],
            "last_updated": training.get("last_updated", ""),
        }
        payload = json.dumps(vps_data, default=str)
        quoted_path = shlex.quote(VPS_TREND_SCOUT)
        ssh_run(f"mkdir -p {quoted_path} && cat > {quoted_path}/manager_scripts.json << 'TRAIN_EOF'\n{payload}\nTRAIN_EOF")
        print("Training data synced to VPS")
    except Exception as e:
        print(f"VPS sync failed (non-fatal): {e}")


# ──────────────────────────────────────────
# CLI commands
# ──────────────────────────────────────────

def cmd_links(urls):
    """Scrape TikTok links and send scripts."""
    print(f"Resolving {len(urls)} TikTok URLs...")
    resolved = resolve_tiktok_urls(urls)
    print(f"Resolved URLs: {resolved}")

    print("Scraping via Apify...")
    tiktok_data = scrape_tiktoks(resolved)
    print(f"Got data for {len(tiktok_data)} videos")

    # Grow the rotation pool with creators behind these URLs
    _remember_creators_from_refs(tiktok_data)

    print("Generating scripts...")
    msg = generate_scripts_from_refs(tiktok_data)
    print(f"Message: {len(msg)} chars")

    token = get_bot_token()
    if send_telegram(token, TELEGRAM_CHAT_ID, msg):
        print("✓ Scripts sent to Telegram!")
        _save_references(tiktok_data)
    else:
        print("✗ Failed to send")
        # Still print the message so it's not lost
        print("\n--- Message content ---")
        print(msg.replace("<b>", "**").replace("</b>", "**")
              .replace("<i>", "_").replace("</i>", "_")
              .replace("<a href=", "[").replace("</a>", "]"))
        sys.exit(1)


def cmd_digest():
    """Send daily trend digest."""
    print("Pulling VPS data...")
    vps_data = pull_vps_data()
    print(f"Got {len(vps_data['videos'])} videos, {len(vps_data['scripts'])} scripts")

    msg = format_digest(vps_data)
    token = get_bot_token()

    if send_telegram(token, TELEGRAM_CHAT_ID, msg):
        print("✓ Digest sent!")
    else:
        print("✗ Failed")
        sys.exit(1)


def cmd_poll():
    """Check for feedback and incoming links."""
    token = get_bot_token()
    n_updates, n_links = process_updates(token)
    print(f"Processed {n_updates} updates, {n_links} new links")


def cmd_test():
    """Send test message."""
    token = get_bot_token()
    msg = (
        "<b>🎬 Research Bot — Online</b>\n\n"
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        "<b>How to use:</b>\n"
        "• Send me TikTok links → I'll generate scripts\n"
        "• Reply with script # when you film it\n"
        "• Reply SKIP if none work\n\n"
        "<b>CLI commands:</b>\n"
        "• <code>links URL1 URL2</code> — scrape + script\n"
        "• <code>digest</code> — daily trend digest\n"
        "• <code>poll</code> — check for messages"
    )
    if send_telegram(token, TELEGRAM_CHAT_ID, msg):
        print("✓ Test sent!")
    else:
        print("✗ Failed")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Telegram Research Digest Bot")
        print()
        print("Usage:")
        print("  telegram_research_bot.py links URL1 URL2 ...  Scrape TikToks → scripts")
        print("  telegram_research_bot.py digest               Daily trend digest")
        print("  telegram_research_bot.py poll                 Check messages + links")
        print("  telegram_research_bot.py test                 Send test message")
        print()
        print("Send TikTok links directly to the bot on Telegram — it will")
        print("auto-scrape and generate scripts when you run 'poll'.")
        print()
        print("Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APIFY_API_KEY, VPS_HOST")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "links":
        if len(sys.argv) < 3:
            print("Usage: telegram_research_bot.py links URL1 URL2 ...")
            sys.exit(1)
        cmd_links(sys.argv[2:])
    elif cmd == "research":
        token = get_bot_token()
        research_and_send_scripts(token)
    elif cmd == "memory":
        if len(sys.argv) < 3:
            print("Usage: telegram_research_bot.py memory 'note to save'")
            sys.exit(1)
        note = " ".join(sys.argv[2:])
        vault_base = os.environ.get("VAULT_PATH")
        if not vault_base:
            print("VAULT_PATH not set — cannot write note to vault")
            sys.exit(1)
        vault_path = Path(vault_base) / "brain" / "Memories.md"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"\n- [{ts}] {note}\n"
        if vault_path.exists():
            content = vault_path.read_text()
            if "## Recent Context" in content:
                content = content.replace("## Recent Context\n", f"## Recent Context\n{entry}", 1)
            else:
                content += f"\n## Recent Context\n{entry}"
            vault_path.write_text(content)
        else:
            vault_path.parent.mkdir(parents=True, exist_ok=True)
            vault_path.write_text(f"# Memories\n\n## Recent Context\n{entry}")
        print(f"Saved to memory: {note}")
    elif cmd == "digest":
        cmd_digest()
    elif cmd == "poll":
        cmd_poll()
    elif cmd == "test":
        cmd_test()
    else:
        # Maybe they passed URLs directly
        if TIKTOK_URL_RE.match(cmd) or "tiktok.com" in cmd:
            cmd_links(sys.argv[1:])
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)


if __name__ == "__main__":
    main()
