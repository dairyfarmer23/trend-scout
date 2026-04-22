#!/usr/bin/env python3
"""Shared Memory Bridge — single source of truth for all AI context.

Merges data from training files, Obsidian vault, and chat history into
one unified memory store. All components read/write through this.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# Base directory — override via TREND_SCOUT_BASE env var. Defaults to the
# directory containing this file, which works fine for local dev.
BASE = Path(os.environ.get("TREND_SCOUT_BASE", str(Path(__file__).parent)))

MEMORY_FILE = BASE / "shared_memory.json"
TRAINING_FILE = BASE / "knowledge_base" / "raw" / "trends" / "script_training.json"
MANAGER_FILE = BASE / "manager_scripts.json"
VAULT_MEMORIES = Path(os.environ.get("VAULT_PATH", str(BASE / "vault"))) / "brain" / "Memories.md"

_DEFAULT_MEMORY = {
    "creators": {},
    "scripts": [],
    "filmed_ids": [],
    "preferences": {
        "style": "bold, confident, unfiltered, talk-to-camera",
        "avoid": "generic, motivational, lifestyle fluff",
        "peak_days": ["Tuesday", "Thursday"],
        "peak_time": "8-10 PM EST",
    },
    "trends": [],
    "competitor_activity": [],
    "calendar_state": {},
    "last_research": None,
    "last_sync": None,
}


def load_memory():
    """Load unified memory, merging all sources."""
    mem = _DEFAULT_MEMORY.copy()

    # Load existing shared memory
    if MEMORY_FILE.exists():
        try:
            saved = json.loads(MEMORY_FILE.read_text())
            mem.update(saved)
        except Exception:
            pass

    # Merge training data
    for tf in [TRAINING_FILE, MANAGER_FILE]:
        if tf.exists():
            try:
                data = json.loads(tf.read_text())
                for script in data.get("scripts", []):
                    vid_id = script.get("video_id", "")
                    if vid_id and not any(s.get("video_id") == vid_id for s in mem["scripts"]):
                        mem["scripts"].append(script)
                    # Track creators
                    username = script.get("username", "")
                    if username and username not in mem["creators"]:
                        mem["creators"][username] = {
                            "source": "training",
                            "first_seen": script.get("date_added", ""),
                        }
            except Exception:
                pass

    # Track filmed preferences
    mem["filmed_ids"] = [s["video_id"] for s in mem["scripts"] if s.get("filmed")]

    return mem


def save_memory(mem):
    """Save unified memory."""
    mem["last_sync"] = datetime.now().isoformat()
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(mem, indent=2, default=str))


def remember(note, category="general"):
    """Save a note to memory and Obsidian vault."""
    mem = load_memory()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    entry = {"text": note, "category": category, "date": ts}
    mem.setdefault("notes", []).append(entry)
    save_memory(mem)

    # Also write to Obsidian vault
    if VAULT_MEMORIES.exists():
        content = VAULT_MEMORIES.read_text()
        new_line = f"\n- [{ts}] ({category}) {note}"
        if "## Recent Context" in content:
            content = content.replace("## Recent Context\n",
                                      f"## Recent Context\n{new_line}\n", 1)
        else:
            content += f"\n## Recent Context\n{new_line}\n"
        VAULT_MEMORIES.write_text(content)

    return entry


def get_context(topic=None):
    """Get structured context for the AI, optionally filtered by topic."""
    mem = load_memory()

    # Real transcripts only
    real_scripts = [s for s in mem["scripts"]
                    if s.get("transcript") and len(s.get("transcript", "")) > 30
                    and not s.get("transcript", "").startswith("(")]

    # Filmed scripts (preferences)
    filmed = [s for s in real_scripts if s.get("filmed")]
    not_filmed = [s for s in real_scripts if not s.get("filmed")]

    # Creators
    creators = list(mem.get("creators", {}).keys())

    # Recent trends
    trends = mem.get("trends", [])[-10:]

    # Competitor activity
    competitors = mem.get("competitor_activity", [])[-10:]

    context = {
        "total_scripts": len(real_scripts),
        "filmed_count": len(filmed),
        "creators": creators,
        "preferences": mem.get("preferences", {}),
        "recent_scripts": real_scripts[-5:],
        "filmed_scripts": filmed[-5:],
        "trends": trends,
        "competitor_activity": competitors,
        "notes": mem.get("notes", [])[-10:],
        "last_research": mem.get("last_research"),
    }

    return context


def add_trend(trend):
    """Add a trend observation."""
    mem = load_memory()
    mem.setdefault("trends", []).append({
        "text": trend,
        "date": datetime.now().isoformat(),
    })
    # Keep last 50
    mem["trends"] = mem["trends"][-50:]
    save_memory(mem)


def add_competitor_activity(username, activity):
    """Log competitor activity."""
    mem = load_memory()
    mem.setdefault("competitor_activity", []).append({
        "username": username,
        "activity": activity,
        "date": datetime.now().isoformat(),
    })
    mem["competitor_activity"] = mem["competitor_activity"][-100:]
    save_memory(mem)


def get_filming_suggestions(day_of_week=None):
    """Get script suggestions based on context."""
    mem = load_memory()
    if not day_of_week:
        day_of_week = datetime.now().strftime("%A")

    real_scripts = [s for s in mem["scripts"]
                    if s.get("transcript") and len(s.get("transcript", "")) > 30
                    and not s.get("transcript", "").startswith("(")]

    # Filter out already-filmed
    unfilmed = [s for s in real_scripts if not s.get("filmed")]

    # Boost scripts from preferred creators (ones user has filmed before)
    filmed_creators = set()
    for s in real_scripts:
        if s.get("filmed") and s.get("username"):
            filmed_creators.add(s["username"])

    # Score and sort
    scored = []
    for s in unfilmed:
        score = 0
        if s.get("username") in filmed_creators:
            score += 10  # preferred creator
        if s.get("views", 0) > 50000:
            score += 5  # viral content
        if s.get("source") == "research":
            score += 3  # from research pipeline
        scored.append((score, s))

    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:8]]
