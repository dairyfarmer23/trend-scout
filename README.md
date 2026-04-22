# trend-scout

**Stop guessing what to film.** Trend research → record-ready script → delivered to your Telegram.

trend-scout watches what's already working in your niche, writes scripts adapted to your voice, and sends them to your phone. Reply with the number you filmed — it learns which hooks are actually landing for you.

Built by a creator who got tired of staring at an empty TikTok.

---

## The pipeline

```
┌─────────────────┐      ┌─────────────────┐      ┌─────────────────┐
│  TREND SCOUT    │  →   │  TELEPROMPTER   │  →   │   FEEDBACK      │
│                 │      │                 │      │                 │
│ scrape top      │      │ generate scripts│      │ reply "filmed 3"│
│ creators,       │      │ adapted to your │      │ system learns   │
│ transcribe vids │      │ platform/voice  │      │ which hooks win │
└─────────────────┘      └─────────────────┘      └─────────────────┘
       auto_research.py        telegram_research_bot.py
```

---

## What each piece does

| File | Role |
|---|---|
| [`auto_research.py`](./auto_research.py) | **Trend scout.** Parallel searches Reddit/X for your niche's trending topics. Scrapes competitor TikTok profiles via Apify. Transcribes top videos. Writes structured results + new creator discoveries to your knowledge base. |
| [`telegram_research_bot.py`](./telegram_research_bot.py) | **Teleprompter.** Two modes: (1) send it TikTok links → it scrapes + generates record-ready scripts adapted to your platforms; (2) daily digest mode pulls the scout's data and sends you scripts to film. Also does conversational editing — reply to any script and it'll rewrite it. |
| [`calendar_agent.py`](./calendar_agent.py) | **Content calendar.** Analyzes everything (day-of-week patterns, trends, competitor activity, what you've already filmed) and sends a "film this today" recommendation. |
| [`memory_bridge.py`](./memory_bridge.py) | **Shared state.** Tracks filmed scripts, competitor data, feedback, and filming patterns. Writes optional notes into an Obsidian vault if you have one. |

---

## Quickstart (5 minutes)

```bash
git clone https://github.com/dairyfarmer23/trend-scout
cd trend-scout
pip install -r requirements.txt
cp .env.example .env
# Fill in TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, APIFY_API_KEY, OPENAI_API_KEY
```

Test the Telegram connection:
```bash
python telegram_research_bot.py test
```

Send it TikTok links to generate scripts:
```bash
python telegram_research_bot.py links https://www.tiktok.com/@user/video/12345
```

Run a full research cycle (scrapes configured creators, finds trends, sends digest):
```bash
python auto_research.py
```

Get a daily "film this" recommendation:
```bash
python calendar_agent.py
```

---

## How to use it daily

The intended loop:

1. **Morning:** Run `calendar_agent.py` (or cron it at 8 AM). You get a Telegram message saying "film this today," with 3–5 scripts ranked by what's working in your niche + what fits today's day-of-week pattern.
2. **Film the ones you like.** Reply to the bot with the script number: `filmed 3`.
3. **The system learns.** Next digest weighs hooks similar to the ones you filmed. Hooks you skip get down-weighted.
4. **Or: send links.** See a TikTok going viral that you want to adapt? Send the URL to the bot. It scrapes, transcribes, and generates 3 adapted scripts in your voice.

Every filmed script + every skipped script becomes training data for the next run. It gets better at suggesting the stuff you'd actually record.

---

## Config reference

All config is env vars — see [`.env.example`](./.env.example) for the full list with notes.

**Required** to do anything useful:
- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `TELEGRAM_CHAT_ID` — message [@userinfobot](https://t.me/userinfobot)
- `APIFY_API_KEY` — TikTok scraping
- `OPENAI_API_KEY` — script generation

**Optional:**
- `TAVILY_API_KEY` or `BRAVE_API_KEY` — web-based trend search (in addition to TikTok)
- `VAULT_PATH` — path to an Obsidian vault; if set, calendar + memory notes get written there
- `VPS_HOST` + `VPS_TREND_SCOUT` — if you split the scout (heavy, runs on VPS) from the teleprompter (runs on laptop)

---

## Scheduling

Cron example (runs research at 6 AM, calendar at 8 AM, sends feedback digest at 9 PM):

```cron
0 6 * * * cd /path/to/trend-scout && python auto_research.py >> auto_research.log 2>&1
0 8 * * * cd /path/to/trend-scout && python calendar_agent.py >> calendar.log 2>&1
0 * * * * cd /path/to/trend-scout && python telegram_research_bot.py poll >> bot.log 2>&1
```

The `poll` run handles inbound feedback ("filmed 3") + queued link submissions.

---

## Philosophy

Most trend tools dump you a dashboard. You still have to interpret it, write the script, and remember to actually film. trend-scout is built backward from the *output*: a script in your hand, at the right time, that you can just read into the camera.

The feedback loop matters more than the trend detection. Anyone can scrape TikTok. Knowing which hooks actually fit *your* voice and face requires data that only exists once you start filming.

---

## Limitations

- **Apify quota.** The TikTok scraper uses Apify's free tier; heavy use will hit rate limits. Plan B: point `VPS_HOST` at a beefier remote trend-scout install.
- **OpenAI cost.** Script generation averages ~$0.01–0.05 per script depending on length. Budget accordingly if you run a large digest.
- **Niche fit.** The scout is most useful if you have a defined niche to point it at. Configure your reference creators in `memory_bridge.py` (see `_DEFAULT_MEMORY`).

---

## License

MIT — see [LICENSE](./LICENSE). Do whatever. Credit appreciated but not required.

## Feedback

Issues and PRs welcome. Extra welcome: tell me what hooks landed for you.
