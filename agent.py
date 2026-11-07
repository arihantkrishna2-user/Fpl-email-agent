import os
import smtplib
import requests
from email.message import EmailMessage
from datetime import datetime, timezone
from google import genai

# 1. Load your stored secrets
TEAM_ID = os.environ["FPL_TEAM_ID"]
GEMINI_KEY = os.environ["GEMINI_API_KEY"]
GMAIL_USER = os.environ["GMAIL_ADDRESS"]
GMAIL_PASS = os.environ["GMAIL_APP_PASSWORD"]

# 2. Fetch live FPL Master Data
bootstrap = requests.get("https://premierleague.com").json()
events = bootstrap["events"]
players = bootstrap["elements"]
teams = {t["id"]: t["short_name"] for t in bootstrap["teams"]}

# 3. Check if the next Gameweek deadline is ~6 hours away (between 5.5 and 6.5 hours)
next_gw = next((e for e in events if e["is_next"]), None)
if not next_gw:
    exit("No upcoming gameweek found.")

deadline = datetime.fromisoformat(next_gw["deadline_time"].replace("Z", "+00:00"))
hours_until_deadline = (deadline - datetime.now(timezone.utc)).total_seconds() / 3600
#
#
# Comment out the next 3 lines when running a manual test!
# if not (5.5 <= hours_until_deadline <= 6.5):
    print(f"Not 6 hours before deadline yet ({hours_until_deadline:.1f}h remaining). Exiting.")
    exit()

current_gw = next(e["id"] for e in events if e["is_current"])

# 4. Automatically pull your current 15-man squad, bank balance, and chip history
picks_data = requests.get(f"https://premierleague.com{TEAM_ID}/event/{current_gw}/picks/").json()
history_data = requests.get(f"https://premierleague.com{TEAM_ID}/history/").json()
used_chips = [c["name"] for c in history_data.get("chips", [])]
bank = picks_data["entry_history"]["bank"] / 10.0

my_squad = []
for p in picks_data["picks"]:
    info = next(x for x in players if x["id"] == p["element"])
    my_squad.append({
        "name": info["web_name"],
        "team": teams[info["team"]],
        "pos": info["element_type"], # 1=GKP, 2=DEF, 3=MID, 4=FWD
        "price": info["now_cost"] / 10.0,
        "selected_by_%": info["selected_by_percent"],
        "total_points": info["total_points"],
        "ppg": info["points_per_game"],
        "influence": info["influence"],
        "news": info["news"],
        "is_starter": p["position"] <= 11
    })

# 5. Filter top 40 market players ranked by your 4 priorities
top_market = sorted(
    [p for p in players if p["minutes"] > 180],
    key=lambda x: (
        float(x["selected_by_percent"]) +
        float(x["total_points"]) * 0.5 +
        float(x["points_per_game"]) * 5 +
        float(x["influence"]) / 20
    ),
    reverse=True
)[:40]

market_summary = [{
    "name": p["web_name"],
    "team": teams[p["team"]],
    "pos": p["element_type"],
    "price": p["now_cost"] / 10.0,
    "selected_by_%": p["selected_by_percent"],
    "total_points": p["total_points"],
    "ppg": p["points_per_game"],
    "influence": p["influence"],
    "news": p["news"]
} for p in top_market]

# 6. The AI System Prompt
prompt = f"""
You are my personal Fantasy Premier League (FPL) Assistant. Gameweek {next_gw['id']} deadline is in 6 hours.

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
2. RECOMMENDED TRANSFERS (WITHIN BUDGET): Suggest the top 1-for-1 replacements strictly within my available budget (sold player price + £{bank}m bank) and max 3-players-per-club rule. Prioritize players with high ownership %, total points, PPG, and influence. Show a comparison table of stats.
3. OPTIMAL FORMATION & STARTING XI: Observe my current Playing 11 vs. Bench and recommend the highest-scoring valid formation (e.g., 3-4-3, 3-5-2, 4-4-2), exact Starting 11, and ordered Substitutes (1 to 3).
4. CAPTAINCY & CHIP STRATEGY: Pick my Captain & Vice-Captain. Advise whether to play Bench Boost, Triple Captain, Free Hit, or Wildcard this week based on my remaining unused chips and overall squad health.

Format cleanly using clear headings, bullet points, and tables for easy reading in an email.
"""

client = genai.Client(api_key=GEMINI_KEY)
response = client.models.generate_content(model="gemini-3.8-flash", contents=prompt)

# 7. Send Briefing via Gmail
msg = EmailMessage()
msg["Subject"] = f"⚽ FPL Gameweek {next_gw['id']} Briefing (6 Hours to Deadline)"
msg["From"] = GMAIL_USER
msg["To"] = GMAIL_USER
msg.set_content(response.text)

with smtplib.SMTP_SSL("://gmail.com", 465) as smtp:
    smtp.login(GMAIL_USER, GMAIL_PASS)
    smtp.send_message(msg)

print("Email briefing sent successfully!")
