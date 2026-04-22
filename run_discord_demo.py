#!/usr/bin/env python3
"""Run notifier with real Discord sends."""

import subprocess
import sys
import json
from pathlib import Path

# Create fresh state
state_file = Path("ottoneu_live_demo_state.json")
state = {"sent_keys": [], "announced_lineups": [], "final_summaries": [], "seen_clips": []}
state_file.write_text(json.dumps(state, indent=2))

print("Starting notifier with SABR scoring - sending real Discord messages...")
print("=" * 70)

proc = subprocess.Popen(
    [
        sys.executable,
        "ottoneu_discord_notifier.py",
        "--date", "2026-04-21",
        "--game-pk", "823148",
        "--state-file", "ottoneu_live_demo_state.json",
        "--replay-final-games",
        "--once"
    ],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True
)

# Read and print output in real-time
for line in proc.stdout:
    print(line.rstrip())

proc.wait()

print("=" * 70)

# Show results
state = json.loads(state_file.read_text())
total = len(state['announced_lineups']) + len(state['sent_keys']) + len(state['final_summaries'])

print(f"\n>>> MESSAGES SENT TO DISCORD: {total}")
print(f"    - Lineup announcements: {len(state['announced_lineups'])}")
print(f"    - Play-by-play events: {len(state['sent_keys'])}")
print(f"    - Final summaries: {len(state['final_summaries'])}")
print(f"    - Highlight clips: {len(state.get('seen_clips', []))}")
