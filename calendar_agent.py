#!/usr/bin/env python3
"""Content Calendar Agent — proactive daily filming recommendations.

Analyzes all context (day of week, trends, competitor activity, filmed
history, preferences) and sends a "film this today" recommendation
via Telegram.

Usage:
    python calendar_agent.py          Send today's calendar
    python calendar_agent.py preview  Preview without sending
"""

import json
import os
import sys
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path

# Auto-load env from .env next to the script (TREND_SCOUT_ENV to override)
_env_override = os.environ.get("TREND_SCOUT_ENV")
for env_path in [Path(_env_override)] if _env_override else [Path(__file__).parent / ".env"]:
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, val = line.split("=", 1)
                if key.strip() not in os.environ:
                    os.environ[key.strip()] = val.strip()
        break

from memory_bridge import load_memory, get_context, get_filming_suggestions

OPENAI_KEY = os.environ.get("OPENAI_API_KEY", "")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")


def send_tg(text):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
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


def get_day_priority():
    """Determine today's content priority based on day of week."""
    day = datetime.now().strftime("%A")
    if day in ("Tuesday", "Thursday"):
        return "HIGH", day, "Peak engagement day — major PPV drop + X teaser 8-10 PM EST"
    elif day in ("Monday", "Wednesday", "Friday"):
        return "MEDIUM", day, "Lighter content day — engagement posts, recycled content, DM follow-up"
    else:
        return "LOW", day, "Weekend — maintenance posts, chatter DM activity"


def build_calendar_prompt(context, suggestions, day_info):
    """Build the GPT prompt for calendar generation."""
    priority, day_name, day_advice = day_info

    # Format suggestions
    suggestions_text = ""
    for i, s in enumerate(suggestions, 1):
        transcript = s.get("transcript", "")[:200]
        username = s.get("username", "?")
        views = s.get("views", 0)
        suggestions_text += f"\n{i}. @{username} ({views:,} views): \"{transcript}\""

    # Format recent trends
    trends_text = ""
    for t in context.get("trends", [])[-5:]:
        trends_text += f"\n- {t.get('text', '')[:100]}"

    # Format competitor activity
    comp_text = ""
    for c in context.get("competitor_activity", [])[-5:]:
        comp_text += f"\n- @{c.get('username', '?')}: {c.get('activity', '')[:100]}"

    # Format preferences from memory
    filmed_text = ""
    for s in context.get("filmed_scripts", [])[-3:]:
        filmed_text += f"\n- \"{s.get('transcript', '')[:80]}...\""

    notes_text = ""
    for n in context.get("notes", [])[-5:]:
        notes_text += f"\n- {n.get('text', '')[:100]}"

    prompt = f"""You are a content calendar agent for a creator who makes vanilla TikTok-style talk-to-camera videos.

TODAY: {day_name}
PRIORITY: {priority}
ADVICE: {day_advice}

CREATOR PREFERENCES:
- Style: {context.get('preferences', {}).get('style', 'bold, confident')}
- Avoids: {context.get('preferences', {}).get('avoid', 'generic content')}
- Total scripts in library: {context.get('total_scripts', 0)}
- Scripts filmed: {context.get('filmed_count', 0)}

SCRIPTS USER HAS LIKED AND FILMED:{filmed_text or ' (none yet)'}

PERSONAL NOTES FROM USER:{notes_text or ' (none)'}

CANDIDATE SCRIPTS TO RECOMMEND:{suggestions_text or ' (none available — suggest the user send more TikTok links)'}

RECENT TRENDS:{trends_text or ' (no recent research)'}

COMPETITOR ACTIVITY:{comp_text or ' (no recent data)'}

Write a content calendar message for TODAY. Format:

1. State the day and priority level
2. Recommend the top 3 scripts to film (with WHY each one — reference trends, competitor moves, or past performance)
3. If it's a peak day (Tue/Thu), emphasize the 8-10 PM posting window
4. Add one competitor alert if there's notable activity
5. Add one trend observation if relevant

Keep it direct and actionable. No fluff. The creator reads this on their phone first thing in the morning."""

    return prompt


def generate_calendar():
    """Generate and send today's content calendar."""
    print(f"=== Content Calendar — {datetime.now().strftime('%Y-%m-%d %A')} ===\n")

    context = get_context()
    suggestions = get_filming_suggestions()
    day_info = get_day_priority()

    priority, day_name, day_advice = day_info
    print(f"Day: {day_name} | Priority: {priority}")
    print(f"Scripts available: {len(suggestions)}")
    print(f"Trends: {len(context.get('trends', []))}")
    print(f"Competitor data: {len(context.get('competitor_activity', []))}")

    if not OPENAI_KEY:
        # Fallback: simple rule-based calendar
        msg = f"<b>📅 Content Calendar — {day_name}</b>\n\n"
        msg += f"<b>Priority:</b> {priority}\n"
        msg += f"{day_advice}\n\n"

        if suggestions:
            msg += "<b>Scripts to film:</b>\n"
            for i, s in enumerate(suggestions[:3], 1):
                transcript = s.get("transcript", "")[:100]
                msg += f"\n{i}. <pre>{transcript}</pre>\n"
        else:
            msg += "No scripts available. Send TikTok links to get new ones.\n"

        send_tg(msg)
        return

    # GPT-powered calendar
    prompt = build_calendar_prompt(context, suggestions, day_info)

    try:
        data = json.dumps({
            "model": "gpt-5.4",
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": 800,
            "temperature": 0.5,
        }).encode()

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=data,
            headers={"Authorization": f"Bearer {OPENAI_KEY}", "Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=30)
        result = json.loads(resp.read())
        calendar_text = result["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"GPT error: {e}")
        calendar_text = f"Priority: {priority}\n{day_advice}\n\nCouldn't generate full calendar — check your scripts and send 'more scripts' to the bot."

    # Format for Telegram — put scripts in code blocks
    # Find quoted text that looks like scripts and wrap in <pre>
    def format_scripts_as_code(text):
        # Match "quoted text" that's 30+ chars (likely a script)
        def replace_quote(m):
            script = m.group(1)
            if len(script) > 30:
                escaped = script.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                return f"\n<pre>{escaped}</pre>\n"
            return f'"{script}"'
        text = re.sub(r'"([^"]{30,})"', replace_quote, text)
        # Also handle **bold** markdown → <b>bold</b> for Telegram
        text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
        # Handle ## headers
        text = re.sub(r'^## (.+)$', r'<b>\1</b>', text, flags=re.MULTILINE)
        return text

    calendar_formatted = format_scripts_as_code(calendar_text)

    msg = f"<b>📅 Content Calendar — {day_name}, {datetime.now().strftime('%B %d')}</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    msg += calendar_formatted

    # Add script code blocks if suggestions exist
    if suggestions:
        msg += "\n\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        msg += "<b>Full scripts attached below — tap and hold to copy:</b>\n"

    send_tg(msg)

    # Send individual scripts as separate messages (easier to copy)
    for i, s in enumerate(suggestions[:3], 1):
        transcript = s.get("transcript", "")
        if transcript and len(transcript) > 30:
            username = s.get("username", "?")
            url = s.get("url", "")
            script_msg = f"<b>Script {i}</b> — @{username}\n\n"
            script_msg += f"<pre>{transcript}</pre>\n"
            if url:
                script_msg += f"\nRef: {url}"
            send_tg(script_msg)

    # Save to vault (skip if VAULT_PATH not set)
    vault_path = os.environ.get("VAULT_PATH")
    if not vault_path:
        return
    vault_dir = Path(vault_path) / "Scripts"
    vault_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    vault_content = f"# Content Calendar — {day_name}, {today}\n\n{calendar_text}\n"
    (vault_dir / f"{today} Calendar.md").write_text(vault_content)
    print(f"Saved to vault: Scripts/{today} Calendar.md")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "preview":
        context = get_context()
        suggestions = get_filming_suggestions()
        day_info = get_day_priority()
        prompt = build_calendar_prompt(context, suggestions, day_info)
        print(prompt)
    else:
        generate_calendar()


if __name__ == "__main__":
    main()
