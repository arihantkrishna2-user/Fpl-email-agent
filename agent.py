import os
import smtplib
import requests
from email.message import EmailMessage
from datetime import datetime, timezone
from google import genai

# 1. Load & clean secrets (strips accidental spaces)
TEAM_ID = os.environ.get("FPL_TEAM_ID", "").strip()
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GMAIL_USER = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_PASS = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()

if not all([TEAM_ID, GEMINI_KEY, GMAIL_USER, GMAIL_PASS]):
    raise ValueError("One or more GitHub Secrets are empty! Check FPL_TEAM_ID, GEMINI_API_KEY, GMAIL_ADDRESS, and GMAIL_APP_PASSWORD.")

# 2. Fetch live FPL Master Data
headers = {"User-Agent": "Mozilla/5.0"}
bootstrap_res = requests.get("https://premierleague.com", headers=headers)
bootstrap_res.raise_for_status()
bootstrap = bootstrap_res.json()

events = bootstrap["events"]
players = bootstrap["elements"]
teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

# 3. Determine upcoming & current Gameweeks safely
next_gw = next((e for e in events if e.get("is_next")), None)
if not next_gw:
    next_gw = next((e for e in events if not e.get("finished")), events[-1])

deadline = datetime.fromisoformat(next_gw["deadline_time"].replace("Z", "+00:00"))
hours_until_deadline = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600

# Automatically bypass the 6-hour timer when you click "Run workflow" manually!
is_manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
if not is_manual_run and not (5.5 <= hours_until_deadline <= 6.5):
    print(f"Scheduled check: {hours_until_deadline:.1f}h until GW{next_gw['id']} deadline. Waiting for 6h window.")
    exit(0)

current_gw = next((e["id"] for e in events if e.get("is_current")), None)
if not current_gw:
    finished_gws = [e["id"] for e in events if e.get("finished")]
    current_gw = finished_gws[-1] if finished_gws else 1

print(f"Fetching squad for Team ID {TEAM_ID} (GW {current_gw} -> Next GW {next_gw['id']})...")

# 4. Pull your current 15-man squad, bank balance, and chip history
picks_res = requests.get(f"https://premierleague.com{TEAM_ID}/event/{current_gw}/picks/", headers=headers)
if picks_res.status_code != 200:
    raise RuntimeError(f"FPL API failed for Team ID '{TEAM_ID}' (Status {picks_res.status_code}). Verify FPL_TEAM_ID is only numbers.")
picks_data = picks_res.json()

history_data = requests.get(f"https://premierleague.com{TEAM_ID}/history/", headers=headers).json()
used_chips = [c["name"] for c in history_data.get("chips", [])]
bank = picks_data["entry_history"]["bank"] / 10.0

my_squad = []
for p in picks_data["picks"]:
    info = next(x for x in players if x["id"] == p["element"])
    my_squad.append({
        "name": info["web_name"],
        "team": teams[info["team"]],
        "pos": {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}.get(info["element_type"], "UNK"),
        "price": info["now_cost"] / 10.0,
        "selected_by_%": info["selected_by_percent"],
        "total_points": info["total_points"],
        "ppg": info["points_per_game"],
        "influence": info["influence"],
        "news": info["news"],
        "is_starter": p["position"] <= 11
    })

# 5. Filter top 40 market players ranked by your 4 priorities
eligible_players = [p for p in players if p.get("minutes", 0) > 90] or players
top_market = sorted(
    eligible_players,
    key=lambda x: (
        float(x.get("selected_by_percent") or 0) +
        float(x.get("total_points") or 0) * 0.5 +
        float(x.get("points_per_game") or 0) * 5 +
        float(x.get("influence") or 0) / 20
    ),
    reverse=True
)[:40]

market_summary = [{
    "name": p["web_name"],
    "team": teams[p["team"]],
    "pos": {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}.get(p["element_type"], "UNK"),
    "price": p["now_cost"] / 10.0,
    "selected_by_%": p["selected_by_percent"],
    "total_points": p["total_points"],
    "ppg": p["points_per_game"],
    "influence": p["influence"],
    "news": p["news"]
} for p in top_market]

# 6. The AI System Prompt
prompt = f"""
You are my personal Fantasy Premier League (FPL) Assistant. Gameweek {next_gw['id']} deadline is in {hours_until_deadline:.1f} hours.

MY SELECTION PRIORITIES (Strictly evaluate and rank players using these 4 metrics):
1. Most Selected Players (selected_by_%)
2. Most Total Points (total_points)
3. Highest Average Points Per Game (ppg)
4. Highest Impact / Influence Score (influence)

MY LIVE TEAM DATA:
- Bank Remaining: £{bank}m
- Chips Already Used: {used_chips}
- My Current 15-Man Squad: {my_squad}
- Top 40 Market Players: {market_summary}

TASK: Write a clear, structured email briefing covering these 4 sections:
1. SQUAD PERFORMANCE & INJURY NEWS: Review my current 15 players. Flag anyone with injury/suspension news, rotation risks, or poor PPG/influence.
2. RECOMMENDED TRANSFERS (WITHIN BUDGET): Suggest the top 1-for-1 replacements strictly within my available budget (sold player price + £{bank}m bank) and max 3-players-per-club limit. Prioritize players with high ownership %, total points, PPG, and influence. Show a comparison table of stats.
3. OPTIMAL FORMATION & STARTING XI: Observe my current Playing 11 vs. Bench and recommend the highest-scoring valid formation (e.g., 3-4-3, 3-5-2, 4-4-2), exact Starting 11, and ordered Substitutes (1 to 3).
4. CAPTAINCY & CHIP STRATEGY: Pick my Captain & Vice-Captain. Advise whether to play Bench Boost, Triple Captain, Free Hit, or Wildcard this week based on my remaining unused chips and overall squad health.

Format cleanly using clear headings, bullet points, and tables for easy reading in an email.
"""

print("Generating Gemini AI briefing...")
client = genai.Client(api_key=GEMINI_KEY)
response = client.models.generate_content(model="gemini-2.5-flash", contents=prompt)

# 7. Send Briefing via Gmail
print(f"Sending email to {GMAIL_USER}...")
msg = EmailMessage()
msg["Subject"] = f"⚽ FPL Gameweek {next_gw['id']} Briefing"
msg["From"] = GMAIL_USER
msg["To"] = GMAIL_USER
msg.set_content(response.text)

with smtplib.SMTP_SSL("://gmail.com", 465) as smtp:
    smtp.login(GMAIL_USER, GMAIL_PASS)
    smtp.send_message(msg)

print("SUCCESS: Email briefing sent!")
