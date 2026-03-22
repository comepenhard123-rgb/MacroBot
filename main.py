"""
Scheduler — Lance le scan toutes les heures
Compatible Railway / Render (free tier)
"""

import time
import schedule
from analyzer import run_scan

def job():
    try:
        run_scan()
    except Exception as e:
        print(f"❌ Erreur pendant le scan: {e}")

# Scan toutes les heures
schedule.every(1).hours.do(job)

# Scan immédiat au démarrage
print("🤖 Bot Elliot Strategy démarré...")
job()

while True:
    schedule.run_pending()
    time.sleep(60)
