"""Weekly Slack Channel Backup

Strategy:
1. Backup period: prev Friday 00:00 ~ this Thursday 23:59:59 KST
2. Fetch 1-month parent window + all thread replies
3. Compare with previous week's backup to catch late thread replies
4. Save as JSON to backups/ directory

Output per week:
- weekly_messages: messages posted within this week's date range
- late_thread_replies: thread replies on older parents, not seen in prev backup
- seen_ts: all ts values observed (used for next week's diff)
"""

import os
import sys
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

from korean_holidays import is_business_day, is_korean_holiday

# -- Config --
KST = timezone(timedelta(hours=9))
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0884BV1KNV")

slack_client = WebClient(token=SLACK_BOT_TOKEN)

BACKUP_DIR = Path(__file__).parent.parent / "backups"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_today_kst():
    return datetime.now(KST).date()


# -- Date Range --

def get_weekly_range(today):
    """prev Friday 00:00 KST ~ this Thursday 23:59:59 KST."""
    days_since_friday = (today.weekday() - 4) % 7
    this_friday = today - timedelta(days=days_since_friday)
    prev_friday = this_friday - timedelta(days=7)
    this_thursday = this_friday - timedelta(days=1)

    start = datetime(prev_friday.year, prev_friday.month, prev_friday.day,
                     0, 0, 0, tzinfo=KST)
    end = datetime(this_thursday.year, this_thursday.month, this_thursday.day,
                   23, 59, 59, tzinfo=KST)
    return start, end


# -- Slack Fetch --

def get_bot_user_id():
    try:
        result = slack_client.auth_test()
        return result.get("bot_id") or result.get("user_id")
    except SlackApiError:
        return None


def resolve_user_name(user_id, user_cache):
    """Resolve Slack user ID to display name."""
    if user_id in user_cache:
        return user_cache[user_id]
    try:
        result = slack_client.users_info(user=user_id)
        profile = result["user"]["profile"]
        name = profile.get("display_name") or profile.get("real_name") or user_id
        user_cache[user_id] = name
        return name
    except SlackApiError:
        user_cache[user_id] = user_id
        return user_id


def fetch_all_messages_with_threads(end_dt):
    """Fetch 1 month of parent messages + ALL thread replies.

    Returns dict: { ts: message_dict } for every message seen.
    """
    wide_start = end_dt - timedelta(days=30)
    oldest = str(wide_start.timestamp())
    latest = str(end_dt.timestamp())

    # 1. Fetch parent messages (1-month window)
    parents = []
    cursor = None
    while True:
        try:
            kwargs = {
                "channel": SLACK_CHANNEL_ID,
                "oldest": oldest,
                "latest": latest,
                "limit": 200,
                "inclusive": True,
            }
            if cursor:
                kwargs["cursor"] = cursor
            result = slack_client.conversations_history(**kwargs)
            parents.extend(result["messages"])
            if not result.get("has_more"):
                break
            cursor = result["response_metadata"]["next_cursor"]
        except SlackApiError as e:
            print(f"ERROR fetching history: {e.response['error']}")
            sys.exit(1)

    print(f"  Parents fetched (1mo): {len(parents)}")

    # 2. For each parent with threads, fetch all replies
    all_messages = {}
    for msg in parents:
        all_messages[msg["ts"]] = msg

        if msg.get("reply_count", 0) > 0:
            try:
                thread_result = slack_client.conversations_replies(
                    channel=SLACK_CHANNEL_ID,
                    ts=msg["ts"],
                    limit=200,
                )
                for reply in thread_result.get("messages", []):
                    all_messages[reply["ts"]] = reply
            except SlackApiError as e:
                print(f"WARNING: thread fetch error: {e.response['error']}")

    print(f"  Total messages (parents + threads): {len(all_messages)}")
    return all_messages


# -- Previous Backup --

def load_previous_seen_ts():
    """Load seen_ts from the most recent backup file."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(BACKUP_DIR.glob("*.json"), reverse=True)
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            seen = set(data.get("seen_ts", []))
            print(f"  Previous backup loaded: {f.name} ({len(seen)} ts)")
            return seen
        except (json.JSONDecodeError, KeyError):
            continue
    print("  No previous backup found. Starting fresh.")
    return set()


# -- Message Enrichment --

def enrich_message(msg, bot_id, user_cache):
    """Add human-readable fields to a message."""
    ts_float = float(msg["ts"])
    dt = datetime.fromtimestamp(ts_float, tz=KST)

    enriched = {
        "ts": msg["ts"],
        "datetime": dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date": dt.strftime("%Y-%m-%d"),
        "text": msg.get("text", ""),
        "is_thread_reply": ("thread_ts" in msg and msg.get("thread_ts") != msg.get("ts")),
    }

    if msg.get("thread_ts") and msg["thread_ts"] != msg["ts"]:
        enriched["parent_ts"] = msg["thread_ts"]

    # User info
    user_id = msg.get("user", "")
    if user_id:
        enriched["user_id"] = user_id
        enriched["user_name"] = resolve_user_name(user_id, user_cache)
    elif msg.get("bot_id"):
        enriched["user_name"] = msg.get("username", f"bot:{msg['bot_id']}")
        enriched["is_bot"] = True

    # Skip our own bot messages
    if bot_id and msg.get("bot_id") == bot_id:
        enriched["is_self_bot"] = True

    # Attachments / files indicator
    if msg.get("files"):
        enriched["has_files"] = True
        enriched["file_names"] = [f.get("name", "") for f in msg["files"]]
    if msg.get("attachments"):
        enriched["has_attachments"] = True

    # Reactions
    if msg.get("reactions"):
        enriched["reactions"] = [
            {"name": r["name"], "count": r["count"]}
            for r in msg["reactions"]
        ]

    return enriched


# -- Main --

def main():
    today = get_today_kst()
    print(f"Today: {today} ({DAY_NAMES[today.weekday()]})")

    is_holiday, holiday_name = is_korean_holiday(today)
    if is_holiday:
        print(f"Holiday ({holiday_name}). Skipping.")
        return

    if not is_business_day(today):
        print("Not a business day. Skipping.")
        return

    # 1. Date range
    start_dt, end_dt = get_weekly_range(today)
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()
    period_label = (
        f"{start_dt.strftime('%Y-%m-%d')} ~ {end_dt.strftime('%Y-%m-%d')}"
    )
    print(f"Backup period: {period_label}")

    # 2. Fetch all messages (1-month window + threads)
    print("Fetching messages...")
    all_messages = fetch_all_messages_with_threads(end_dt)

    # 3. Load previous backup
    print("Loading previous backup...")
    prev_seen_ts = load_previous_seen_ts()

    # 4. Categorize
    bot_id = get_bot_user_id()
    user_cache = {}

    weekly_messages = []      # messages within this week's date range
    late_thread_replies = []  # thread replies on old parents, newly found

    for ts, msg in all_messages.items():
        msg_ts = float(ts)
        in_range = start_ts <= msg_ts <= end_ts
        is_new = ts not in prev_seen_ts

        enriched = enrich_message(msg, bot_id, user_cache)

        # Skip self-bot messages
        if enriched.get("is_self_bot"):
            continue

        if in_range:
            weekly_messages.append(enriched)
        elif is_new and enriched.get("is_thread_reply"):
            # Thread reply outside this week's range, but not seen before
            late_thread_replies.append(enriched)

    # Sort by timestamp
    weekly_messages.sort(key=lambda m: m["ts"])
    late_thread_replies.sort(key=lambda m: m["ts"])

    print(f"  Weekly messages: {len(weekly_messages)}")
    print(f"  Late thread replies: {len(late_thread_replies)}")

    # 5. Build backup object
    all_seen_ts = list(all_messages.keys())

    backup = {
        "meta": {
            "period": period_label,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "generated_at": datetime.now(KST).isoformat(),
            "channel_id": SLACK_CHANNEL_ID,
            "stats": {
                "weekly_messages": len(weekly_messages),
                "late_thread_replies": len(late_thread_replies),
                "total_seen": len(all_seen_ts),
            },
        },
        "weekly_messages": weekly_messages,
        "late_thread_replies": late_thread_replies,
        "seen_ts": all_seen_ts,
    }

    # 6. Save
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{start_dt.strftime('%Y-%m-%d')}.json"
    filepath = BACKUP_DIR / filename
    filepath.write_text(
        json.dumps(backup, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Backup saved: {filepath}")
    print("Done!")


if __name__ == "__main__":
    main()
