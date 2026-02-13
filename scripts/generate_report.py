"""VCMS Slack Auto Report Generator

Runs on weekdays at 10:00 KST via GitHub Actions.
1. Holiday check -> skip
2. Day check -> daily (Mon-Thu) / weekly (Fri)
3. Fetch Slack history + thread replies
4. Generate report via Claude API
5. Post to Slack + Save to Notion
"""

import os
import sys
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import requests

from korean_holidays import is_business_day, is_korean_holiday

# -- Config --
KST = timezone(timedelta(hours=9))
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0884BV1KNV")
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_TOKEN = os.environ.get("NOTION_API_TOKEN", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
FORCE_TYPE = os.environ.get("FORCE_TYPE", "auto")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_today_kst():
    return datetime.now(KST).date()


def load_guide():
    guide_path = Path(__file__).parent.parent / "slack-vcms-summary-guide.md"
    if guide_path.exists():
        return guide_path.read_text(encoding="utf-8")
    print("WARNING: guide file not found")
    return ""


def fetch_slack_history(start_dt, end_dt):
    """Fetch channel messages + all thread replies."""
    messages = []
    oldest = str(start_dt.timestamp())
    latest = str(end_dt.timestamp())
    cursor = None

    # 1. Get top-level messages
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
            messages.extend(result["messages"])

            if not result.get("has_more"):
                break
            cursor = result["response_metadata"]["next_cursor"]

        except SlackApiError as e:
            print(f"ERROR Slack API: {e.response['error']}")
            sys.exit(1)

    # 2. Fetch thread replies for messages that have threads
    all_messages = []
    for msg in messages:
        all_messages.append(msg)
        if msg.get("reply_count", 0) > 0:
            try:
                thread_result = slack_client.conversations_replies(
                    channel=SLACK_CHANNEL_ID,
                    ts=msg["ts"],
                    oldest=oldest,
                    latest=latest,
                    limit=200,
                )
                replies = thread_result.get("messages", [])[1:]
                all_messages.extend(replies)
            except SlackApiError as e:
                print(f"WARNING: thread fetch failed: {e.response['error']}")

    all_messages.sort(key=lambda m: float(m["ts"]))
    print(f"  Top-level: {len(messages)}, With threads: {len(all_messages)}")
    return all_messages


def format_slack_messages(messages):
    lines = []
    for msg in messages:
        ts = datetime.fromtimestamp(float(msg["ts"]), tz=KST)
        time_str = ts.strftime("%m/%d %H:%M")
        text = msg.get("text", "").strip()
        if not text:
            continue
        is_reply = "thread_ts" in msg and msg.get("thread_ts") != msg.get("ts")
        prefix = "  [reply] " if is_reply else ""
        lines.append(f"[{time_str}] {prefix}{text}")
    return "\n".join(lines)


def determine_report_type(today):
    if FORCE_TYPE in ("daily", "weekly"):
        return FORCE_TYPE
    if today.weekday() == 4:
        return "weekly"
    return "daily"


def get_date_range(today, report_type):
    if report_type == "daily":
        yesterday = today - timedelta(days=1)
        start = datetime(yesterday.year, yesterday.month, yesterday.day, 0, 0, 0, tzinfo=KST)
        end = datetime(yesterday.year, yesterday.month, yesterday.day, 23, 59, 59, tzinfo=KST)
        return start, end, yesterday.strftime("%Y-%m-%d")
    else:
        # Weekly: this week Mon ~ Thu
        this_monday = today - timedelta(days=today.weekday())
        this_thursday = today - timedelta(days=1)
        start = datetime(this_monday.year, this_monday.month, this_monday.day, 0, 0, 0, tzinfo=KST)
        end = datetime(this_thursday.year, this_thursday.month, this_thursday.day, 23, 59, 59, tzinfo=KST)
        return start, end, f"{this_monday.strftime('%m/%d')}~{this_thursday.strftime('%m/%d')}"


def convert_to_slack_mrkdwn(text):
    """Force convert Markdown to Slack mrkdwn."""
    lines = text.split('\n')
    result = []
    for line in lines:
        line = re.sub(r'^#{1,6}\s+', '', line)
        line = line.replace('**', '*')
        if re.match(r'^-{3,}$', line.strip()):
            line = '───'
        result.append(line)
    return '\n'.join(result)


def generate_report_with_claude(slack_text, report_type, date_label, guide):
    if report_type == "daily":
        case_instruction = "[Case A] daily quick report format"
    else:
        case_instruction = "[Case B] weekly operation diagnosis report format"

    system_prompt = (
        "You are a senior manager of VCMS (accommodation channel manager) operations team.\n"
        "Analyze Slack channel messages and write a report.\n\n"
        "Follow this guide:\n\n"
        f"{guide}\n\n"
        "Additional instructions:\n"
        "- Be precise with numbers, do not fabricate\n"
        "- Include specific names (venues, staff)\n"
        "- Provide root cause analysis and concrete action items\n"
        "- Suggest systemic improvements for recurring patterns\n"
        "- Write in Korean\n"
        "- CRITICAL: Use Slack mrkdwn format, NOT standard Markdown\n"
        "- Bold: *single asterisk* (NOT **double**)\n"
        "- No ### or #### headers. Use *bold text* with emoji for sections\n"
        "- Italic: _underscore_ (NOT *asterisk*)\n"
        "- Lists: use bullet or numbered 1. 2. 3.\n"
        "- Divider: use three dashes\n"
        "- Example section header: *section title here*\n"
    )

    user_prompt = (
        f"Below are Slack messages from #system-vcms-noti for {date_label}.\n"
        f"Messages marked [reply] are thread replies.\n"
        f"Please write the report in {case_instruction}.\n\n"
        f"---SLACK MESSAGES START---\n"
        f"{slack_text}\n"
        f"---SLACK MESSAGES END---"
    )

    response = claude_client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=2000,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return response.content[0].text


def post_to_slack(report_text, report_type, date_label):
    if report_type == "daily":
        type_label = "Daily Quick Report"
    else:
        type_label = "Weekly Diagnosis Report"

    full_message = f"*{type_label}*  |  {date_label}\n───\n\n{report_text}"

    try:
        result = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=full_message,
            mrkdwn=True,
        )
        print(f"OK Slack posted: ts={result['ts']}")
        return result["ts"]
    except SlackApiError as e:
        print(f"ERROR Slack post failed: {e.response['error']}")
        return None


def save_to_notion(report_text, report_type, date_label, today):
    if not NOTION_API_TOKEN or not NOTION_DATABASE_ID:
        print("WARNING: Notion not configured, skipping")
        return

    type_label = "daily" if report_type == "daily" else "weekly"
    title = f"[{type_label}] {date_label} VCMS Report"

    chunks = [report_text[i:i+2000] for i in range(0, len(report_text), 2000)]
    children = []
    for chunk in chunks:
        children.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            }
        })

    payload = {
        "parent": {"database_id": NOTION_DATABASE_ID},
        "properties": {
            "Name": {"title": [{"text": {"content": title}}]},
            "Date": {"date": {"start": today.isoformat()}},
            "Type": {"select": {"name": type_label}},
        },
        "children": children,
    }

    headers = {
        "Authorization": f"Bearer {NOTION_API_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }

    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=headers,
        json=payload,
    )

    if resp.status_code == 200:
        page_id = resp.json()["id"]
        print(f"OK Notion saved: {page_id}")
    else:
        print(f"WARNING Notion save failed ({resp.status_code}): {resp.text}")


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

    report_type = determine_report_type(today)
    print(f"Report type: {report_type}")

    start_dt, end_dt, date_label = get_date_range(today, report_type)
    print(f"Collecting: {date_label}")

    messages = fetch_slack_history(start_dt, end_dt)
    print(f"Messages collected: {len(messages)}")

    if len(messages) == 0:
        print("No messages found. Skipping.")
        return

    slack_text = format_slack_messages(messages)
    print(f"Formatted text: {len(slack_text)} chars")

    guide = load_guide()
    print("Calling Claude API...")
    report = generate_report_with_claude(slack_text, report_type, date_label, guide)
    report = convert_to_slack_mrkdwn(report)
    print(f"Report generated ({len(report)} chars)")

    post_to_slack(report, report_type, date_label)
    save_to_notion(report, report_type, date_label, today)

    print("All done!")


if __name__ == "__main__":
    main()
