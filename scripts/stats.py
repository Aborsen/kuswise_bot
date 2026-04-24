"""Print database stats: users, meals, daily activity.

Usage:
    python scripts/stats.py

Requires DATABASE_URL in .env (pull it once with:
    vercel env pull .env.local --yes
and then copy DATABASE_URL to .env, OR just: cp .env.local .env).
"""
import os
import sys

import psycopg

try:
    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(".env.local", override=False)
except ImportError:
    pass


def main() -> int:
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if not url:
        print("Missing DATABASE_URL. Run: vercel env pull .env.local --yes", file=sys.stderr)
        return 1

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        # Users
        cur.execute("SELECT COUNT(*) FROM users")
        n_users = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM meals")
        n_meals = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM daily_logs")
        n_days = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM pending_photos")
        n_pending = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM daily_recommendations")
        n_recs = cur.fetchone()[0]

        print("📊 DATABASE STATS")
        print("─" * 40)
        print(f"Users:                 {n_users}")
        print(f"Meals logged:          {n_meals}")
        print(f"Days with data:        {n_days}")
        print(f"Pending entries:       {n_pending}")
        print(f"Daily recommendations: {n_recs}")

        # Per-user
        cur.execute("""
            SELECT u.user_id, COALESCE(u.username, ''), u.created_at,
                   COUNT(m.id) AS meals,
                   MAX(m.created_at) AS last_meal
            FROM users u
            LEFT JOIN meals m ON m.user_id = u.user_id
            GROUP BY u.user_id, u.username, u.created_at
            ORDER BY meals DESC, u.created_at DESC
        """)
        rows = cur.fetchall()
        if rows:
            print()
            print("👥 USERS")
            print("─" * 80)
            print(f"{'user_id':<12} {'username':<20} {'joined':<22} {'meals':>6}  last meal")
            print("─" * 80)
            for r in rows:
                uid, uname, joined, meals, last = r
                joined_short = (joined or "")[:19]
                last_short = (last or "—")[:19]
                print(f"{uid:<12} {uname[:20]:<20} {joined_short:<22} {meals:>6}  {last_short}")

        # Last 10 meals
        cur.execute("""
            SELECT user_id, date, meal_type, description, calories, created_at
            FROM meals ORDER BY id DESC LIMIT 10
        """)
        meals = cur.fetchall()
        if meals:
            print()
            print("🍽️ LAST 10 MEALS")
            print("─" * 100)
            for r in meals:
                uid, date, mt, desc, cal, ts = r
                desc_short = (desc or "")[:45]
                print(f"  {ts[:19]}  uid={uid}  {date}  {mt:<10} {round(cal or 0):>5} cal  {desc_short}")

        # Today's daily_logs summary
        cur.execute("""
            SELECT user_id, date, total_calories, total_protein_g,
                   total_carbs_g, total_fat_g, summary_sent
            FROM daily_logs ORDER BY date DESC, user_id LIMIT 15
        """)
        dl = cur.fetchall()
        if dl:
            print()
            print("📅 RECENT DAILY LOGS")
            print("─" * 80)
            print(f"{'date':<12} {'user_id':<12} {'cal':>6} {'P':>5} {'C':>5} {'F':>5}  summary?")
            for r in dl:
                uid, date, cal, p, c, f, sent = r
                print(f"{date:<12} {uid:<12} {round(cal or 0):>6} {round(p or 0):>5} {round(c or 0):>5} {round(f or 0):>5}  {'✓' if sent else '—'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
