#!/usr/bin/env python3
"""Auto-Research Pipeline — runs multiple tools in parallel.

Searches Reddit/X for trends, scrapes competitor profiles, and
writes structured results to Obsidian vault + shared memory.

Usage:
    python auto_research.py           Full research pipeline
    python auto_research.py trends    Just trend search
    python auto_research.py compete   Just competitor scraping
"""

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

# Auto-load env from .env next to the script (TREND_SCOUT_ENV to override)
env_override = os.environ.get("TREND_SCOUT_ENV")
for env_path in [Path(env_override)] if env_override else [Path(__file__).parent / ".env"]:
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()
        break

from memory_bridge import load_memory, save_memory, add_trend, add_competitor_activity

TAVILY_KEY = os.environ.get("TAVILY_API_KEY", "")
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "")
APIFY_KEY = os.environ.get("APIFY_API_KEY", "")
OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

VAULT = Path(os.environ.get("VAULT_PATH", str(Path(__file__).parent / "vault")))


def send_tg(text):
    """Send Telegram message."""
    if not TG_TOKEN or not TG_CHAT:
        return
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT, "text": chunk,
            "parse_mode": "HTML", "disable_web_page_preview": "true",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            data=data, headers={"User-Agent": "FanUploader/1.0"})
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception:
            pass


# ── Research Sources ──

def search_tavily(queries):
    """Search via Tavily API (AI-optimized search)."""
    if not TAVILY_KEY:
        print("[Tavily] No API key")
        return []

    results = []
    for query in queries:
        try:
            data = json.dumps({
                "api_key": TAVILY_KEY,
                "query": query,
                "max_results": 5,
                "search_depth": "basic",
            }).encode()
            req = urllib.request.Request(
                "https://api.tavily.com/search",
                data=data,
                headers={"Content-Type": "application/json"})
            resp = urllib.request.urlopen(req, timeout=15)
            result = json.loads(resp.read())

            for r in result.get("results", []):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", "")[:300],
                    "source": "tavily",
                    "query": query,
                })
        except Exception as e:
            print(f"[Tavily] Error for '{query}': {e}")

    print(f"[Tavily] Got {len(results)} results from {len(queries)} queries")
    return results


def search_brave(queries):
    """Search via Brave Search API."""
    if not BRAVE_KEY:
        print("[Brave] No API key, skipping")
        return []

    results = []
    for query in queries:
        try:
            url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode({
                "q": query, "count": 5})
            req = urllib.request.Request(url, headers={
                "X-Subscription-Token": BRAVE_KEY,
                "Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())

            for r in data.get("web", {}).get("results", []):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("description", "")[:300],
                    "source": "brave",
                    "query": query,
                })
        except Exception as e:
            print(f"[Brave] Error for '{query}': {e}")

    print(f"[Brave] Got {len(results)} results")
    return results


def scrape_competitors():
    """Scrape reference creator profiles via Apify."""
    if not APIFY_KEY:
        print("[Apify] No API key")
        return []

    try:
        from apify_client import ApifyClient
    except ImportError:
        print("[Apify] apify-client not installed")
        return []

    mem = load_memory()
    creators = list(mem.get("creators", {}).keys())
    # Filter to real usernames, prefer TikTok creators
    creators = [c for c in creators if c and not c.startswith("ref_") and len(c) > 2]

    # Skip known-private/blocked accounts
    skip = {"victoriaantoinet", "lexarxriqnt"}
    creators = [c for c in creators if c not in skip]

    if not creators:
        print("[Apify] No creators to monitor")
        return []

    client = ApifyClient(APIFY_KEY)
    results = []

    # Scrape TikTok profiles — up to 8 creators, 5 videos each
    try:
        print(f"[Apify] Scraping {min(len(creators), 8)} TikTok profiles...")
        run_input = {
            "profiles": [f"https://www.tiktok.com/@{u}" for u in creators[:8]],
            "resultsPerPage": 5,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
        }
        run = client.actor("clockworks/tiktok-scraper").call(
            run_input=run_input, timeout_secs=120)
        items = list(client.dataset(run["defaultDatasetId"]).iterate_items())

        for item in items:
            username = item.get("authorMeta", {}).get("name", "")
            views = item.get("playCount", 0)
            desc = item.get("text", "")[:200]
            url = item.get("webVideoUrl", "")

            if username and views:
                results.append({
                    "username": username,
                    "views": views,
                    "description": desc,
                    "url": url,
                    "source": "tiktok",
                })

        print(f"[Apify] Got {len(results)} competitor videos")
    except Exception as e:
        print(f"[Apify] Scrape failed: {e}")

    return results


def summarize_with_gpt(research_text):
    """Use GPT-5.4 to synthesize research into actionable insights."""
    if not OPENAI_KEY:
        return research_text[:500]

    try:
        data = json.dumps({
            "model": "gpt-5.4",
            "messages": [
                {"role": "system", "content": "You are a content strategy analyst for a creator who makes vanilla TikTok-style videos. Summarize research findings into 3-5 actionable bullet points. Be specific and direct. No fluff."},
                {"role": "user", "content": f"Here's today's research. What should this creator know and do?\n\n{research_text[:3000]}"},
            ],
            "max_completion_tokens": 500,
            "temperature": 0.3,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        return result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[GPT] Summarize failed: {e}")
        return research_text[:500]


# ── Main Pipeline ──

def run_full_pipeline():
    """Run all research sources in parallel, then synthesize."""
    print(f"=== Auto-Research Pipeline — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===\n")

    mem = load_memory()
    creators = [c for c in mem.get("creators", {}).keys()
                if c and not c.startswith("ref_") and len(c) > 2]

    # Build search queries
    trend_queries = [
        "viral TikTok content ideas this week",
        "trending TikTok sounds April 2026",
        "content creator growth strategies reddit",
    ]

    competitor_queries = []
    for creator in creators[:3]:
        competitor_queries.append(f"@{creator} TikTok new video")

    # Run in parallel
    all_trends = []
    all_competitors = []

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {
            executor.submit(search_tavily, trend_queries): "tavily",
            executor.submit(search_brave, competitor_queries): "brave",
            executor.submit(scrape_competitors): "apify",
        }

        for future in as_completed(futures):
            source = futures[future]
            try:
                results = future.result()
                if source in ("tavily", "brave"):
                    all_trends.extend(results)
                else:
                    all_competitors.extend(results)
            except Exception as e:
                print(f"[{source}] Failed: {e}")

    print(f"\nTotal: {len(all_trends)} trend results, {len(all_competitors)} competitor videos")

    # Step 2: Transcribe the top competitor videos (highest views)
    all_competitors.sort(key=lambda c: c.get("views", 0), reverse=True)
    transcribed_scripts = []
    top_videos = [c for c in all_competitors if c.get("url") and c.get("views", 0) > 100][:6]

    if top_videos and APIFY_KEY:
        print(f"\nTranscribing top {len(top_videos)} competitor videos...")
        try:
            from apify_client import ApifyClient as _AC
            _client = _AC(APIFY_KEY)
            for vid in top_videos:
                if len(transcribed_scripts) >= 4:
                    break
                url = vid.get("url", "")
                if not url:
                    continue
                try:
                    run = _client.actor("tictechid/anoxvanzi-transcriber").call(
                        run_input={"start_urls": url}, timeout_secs=120)
                    items = list(_client.dataset(run["defaultDatasetId"]).iterate_items())
                    for item in items:
                        raw = item.get("transcript", "")
                        if item.get("status") == "success" and raw and len(raw) > 30:
                            import re as _re
                            clean = _re.sub(r"\[\d+\.\d+s\s*-\s*\d+\.\d+s\]\s*", "", raw).strip()
                            transcribed_scripts.append({
                                "username": vid.get("username", "?"),
                                "transcript": clean,
                                "views": vid.get("views", 0),
                                "url": url,
                            })
                            print(f"  ✓ @{vid.get('username', '?')} ({len(clean)} chars)")
                        else:
                            print(f"  ✗ @{vid.get('username', '?')} blocked/no speech")
                except Exception as e:
                    print(f"  ✗ {url[:40]}... ({e})")
        except Exception as e:
            print(f"Transcription failed: {e}")

    print(f"Transcribed {len(transcribed_scripts)} scripts")

    # Step 3: Discover new similar creators from Tavily
    # Search specifically for creators similar to the reference list
    if TAVILY_KEY:
        print("\nSearching for new similar creators...")
        # Use actual reference creators to find similar ones
        ref_creators = [c for c in mem.get("creators", {}).keys()
                        if mem["creators"][c].get("source") == "manager_reference"]
        discovery_queries = [
            "TikTok creators like @officialjustpeechi @zonamaeee talk to camera bold confident",
            "trans female TikTok creator talk to camera monologue viral 2026",
            "TikTok creator bold unfiltered opinion hot takes similar to @justpeechi",
        ]
        if ref_creators:
            sample = ref_creators[:3]
            discovery_queries.append(f"TikTok creators similar to @{' @'.join(sample)} style")

        discovery_results = search_tavily(discovery_queries)
        # Look for @usernames in results
        for r in discovery_results:
            snippet = r.get("snippet", "") + " " + r.get("title", "")
            found = re.findall(r"@(\w{3,20})", snippet)
            for username in found:
                mem = load_memory()
                if username not in mem.get("creators", {}) and username.lower() not in ("tiktok", "instagram", "youtube"):
                    mem.setdefault("creators", {})[username] = {
                        "platform": "tiktok",
                        "source": "auto_discovery",
                        "discovered": datetime.now().isoformat(),
                    }
                    save_memory(mem)
                    print(f"  New creator discovered: @{username}")

    # Save to memory
    for t in all_trends[:20]:
        add_trend(f"{t['title']}: {t['snippet'][:100]}")

    for c in all_competitors[:20]:
        add_competitor_activity(c.get("username", "?"),
                                f"{c.get('views', 0):,} views: {c.get('description', '')[:80]}")

    mem = load_memory()
    mem["last_research"] = datetime.now().isoformat()
    save_memory(mem)

    # Build research text for GPT summarization
    research_text = "TRENDS:\n"
    for t in all_trends[:10]:
        research_text += f"- {t['title']}: {t['snippet'][:150]}\n"

    research_text += "\nCOMPETITOR ACTIVITY:\n"
    for c in all_competitors[:10]:
        research_text += f"- @{c.get('username', '?')}: {c.get('views', 0):,} views — {c.get('description', '')[:100]}\n"

    # GPT synthesis
    print("\nSynthesizing with GPT-5.4...")
    insights = summarize_with_gpt(research_text)

    # Save to Obsidian vault
    today = datetime.now().strftime("%Y-%m-%d")
    vault_dir = VAULT / "Trends"
    vault_dir.mkdir(parents=True, exist_ok=True)

    vault_content = f"""---
title: "Auto-Research — {today}"
type: research
date: {today}
trends: {len(all_trends)}
competitors: {len(all_competitors)}
tags:
  - research
  - auto-generated
---

# Auto-Research — {today}

## Key Insights

{insights}

## Trend Signals ({len(all_trends)})

"""
    for t in all_trends[:15]:
        vault_content += f"- **{t['title']}** — {t['snippet'][:150]}\n"

    vault_content += f"\n## Competitor Activity ({len(all_competitors)})\n\n"
    for c in all_competitors[:10]:
        vault_content += f"- **@{c.get('username', '?')}** — {c.get('views', 0):,} views: {c.get('description', '')[:100]}\n"

    (vault_dir / f"{today} Auto-Research.md").write_text(vault_content)
    print(f"Saved to vault: Trends/{today} Auto-Research.md")

    # Save raw JSON
    raw_dir = Path(__file__).parent / "knowledge_base" / "raw" / "trends"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    (raw_dir / f"{ts}_auto_research.json").write_text(json.dumps({
        "trends": all_trends, "competitors": all_competitors,
        "insights": insights, "date": today,
    }, indent=2, default=str))

    # Send Telegram summary
    tg_msg = f"<b>📊 Auto-Research — {today}</b>\n\n"
    tg_msg += f"<b>Key Insights:</b>\n{insights}\n\n"

    if all_competitors:
        # Show diverse competitors, not just one
        seen_users = set()
        tg_msg += "<b>Competitor Highlights:</b>\n"
        for c in all_competitors[:15]:
            user = c.get("username", "?")
            if user not in seen_users:
                seen_users.add(user)
                tg_msg += f"• @{user}: {c.get('views', 0):,} views\n"
            if len(seen_users) >= 5:
                break

    send_tg(tg_msg)

    # Send transcribed scripts as separate messages
    if transcribed_scripts:
        scripts_msg = f"<b>🎬 {len(transcribed_scripts)} Fresh Scripts from Research</b>\n"
        scripts_msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        for i, s in enumerate(transcribed_scripts, 1):
            escaped = s["transcript"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            scripts_msg += f"=== SCRIPT {i} ===\n"
            scripts_msg += f"Ref: @{s['username']} ({s['views']:,} views)\n\n"
            scripts_msg += f"<pre>{escaped}</pre>\n\n"
            if s.get("url"):
                scripts_msg += f"Ref: {s['url']}\n"
            scripts_msg += "\n---\n\n"

        scripts_msg += "<b>Reply with the # you want to film</b>"
        send_tg(scripts_msg)
        print(f"Sent {len(transcribed_scripts)} transcribed scripts")
    else:
        send_tg("<i>No new scripts transcribed today. Send TikTok links for fresh scripts.</i>")

    print("Telegram summary sent")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "full"

    if cmd == "trends":
        results = search_tavily([
            "viral TikTok content ideas this week",
            "content creator growth strategies",
        ])
        for r in results:
            print(f"  {r['title']}: {r['snippet'][:80]}")
    elif cmd == "compete":
        results = scrape_competitors()
        for r in results:
            print(f"  @{r.get('username')}: {r.get('views', 0):,} views")
    else:
        run_full_pipeline()


if __name__ == "__main__":
    main()
