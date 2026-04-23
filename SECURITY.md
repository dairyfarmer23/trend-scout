# Security Policy

## Reporting a Vulnerability

If you find a security issue, please **do not open a public GitHub issue**.

Instead, use GitHub's private vulnerability reporting:
1. Go to the [Security tab](https://github.com/dairyfarmer23/trend-scout/security)
2. Click **Report a vulnerability**
3. Describe the issue with steps to reproduce

You'll get an initial response within 72 hours. Fixes for verified issues
typically ship within 7 days, coordinated with the reporter.

## Scope

In scope:
- Command injection, SSRF, or other code execution in the Python scripts
- Secrets that could be extracted from the deployed bot
- Anything that lets an unauthorized Telegram user trigger actions

Out of scope:
- API cost burn from a leaked token (rotate the token — see README)
- yt-dlp cookie access on shared hosts (documented limitation)
- Rate-limit exhaustion on Apify / OpenAI / Telegram when you run it yourself

## Supported Versions

Only `main` is supported. If you're running a fork, please update regularly.

## Security-Relevant Configuration

- **Never commit `.env`.** The repo's `.gitignore` excludes it, but double-check before pushing.
- **Rotate `TELEGRAM_BOT_TOKEN` immediately** if your `.env` ever leaves your machine. Whoever has the token can use your bot to burn your OpenAI + Apify credits.
- **`TELEGRAM_CHAT_ID` is the only auth.** The bot only responds to messages from that chat ID. If you deploy to a group chat, everyone in the group can command the bot — make sure that's what you want.
- **Don't run on shared machines.** `yt-dlp --cookies-from-browser chrome` reads whoever's Chrome profile is active. Single-user machines only.
