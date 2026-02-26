"""VCMS Slack Weekly Report Generator

Flow:
1. Holiday/business day check
2. Fetch weekly feedback from Cloudflare Worker
3. Load messages from backup JSON (fallback: Slack API direct fetch)
4. Generate report via Claude API (Anthropic)
5. Post to Slack with feedback button
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

FEEDBACK_WORKER_URL = os.environ.get("FEEDBACK_WORKER_URL", "")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

BACKUP_DIR = Path(__file__).parent.parent / "backups"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def get_today_kst():
    return datetime.now(KST).date()


def load_guide():
    guide_path = Path(__file__).parent.parent / "slack-vcms-summary-guide.md"
    if guide_path.exists():
        return guide_path.read_text(encoding="utf-8")
    print("WARNING: guide file not found")
    return ""


# -- Feedback from Cloudflare Worker --

def fetch_accumulated_feedback():
    """Fetch all accumulated feedback from Cloudflare Worker API."""
    if not FEEDBACK_WORKER_URL:
        print("WARNING: FEEDBACK_WORKER_URL not set, skipping feedback")
        return []

    try:
        resp = requests.get(f"{FEEDBACK_WORKER_URL}/feedback", timeout=10)
        if resp.status_code == 200:
            feedback = resp.json()
            print(f"Accumulated feedback loaded: {len(feedback)} entries")
            return feedback
        else:
            print(f"WARNING: feedback fetch failed ({resp.status_code})")
            return []
    except Exception as e:
        print(f"WARNING: feedback fetch error: {e}")
        return []


def fetch_weekly_feedback(start_dt, end_dt):
    """Fetch feedback only for the report period."""
    all_feedback = fetch_accumulated_feedback()
    if not all_feedback:
        return []

    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = end_dt.strftime("%Y-%m-%d")

    weekly = [f for f in all_feedback if start_date <= f.get("date", "") <= end_date]
    print(f"Weekly feedback: {len(weekly)} of {len(all_feedback)} total")
    return weekly


def format_feedback_for_prompt(feedback_list):
    """Format weekly feedback for prompt."""
    if not feedback_list:
        return ""

    category_labels = {
        "correction": "사실 오류 수정",
        "categorization": "분류 기준 변경",
        "format": "포맷/형식 변경",
        "general": "기타 의견",
    }

    lines = []
    for entry in feedback_list:
        cat = category_labels.get(entry.get("category", ""), entry.get("category", ""))
        date = entry.get("date", "")
        user = entry.get("user_name", "")
        text = entry.get("text", "")
        lines.append(f"[{date}] [{cat}] {user}: {text}")

    return "\n".join(lines)


# -- Backup File Reader --

def load_from_backup(start_dt):
    """Try to load messages from backup JSON file.

    Returns (messages_text, success) tuple.
    messages_text: formatted string ready for prompt
    success: True if backup was used
    """
    filename = f"{start_dt.strftime('%Y-%m-%d')}.json"
    filepath = BACKUP_DIR / filename

    if not filepath.exists():
        print(f"  Backup file not found: {filepath}")
        return None, False

    try:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, IOError) as e:
        print(f"  Backup file read error: {e}")
        return None, False

    weekly = data.get("weekly_messages", [])
    late = data.get("late_thread_replies", [])
    all_msgs = weekly + late
    all_msgs.sort(key=lambda m: m.get("ts", "0"))

    stats = data.get("meta", {}).get("stats", {})
    print(f"  Backup loaded: {filepath.name}")
    print(f"    weekly={stats.get('weekly_messages', '?')}, "
          f"late_threads={stats.get('late_thread_replies', '?')}")

    # Format to same text format as format_slack_messages()
    lines = []
    for msg in all_msgs:
        # Skip bot self-messages
        if msg.get("is_self_bot") or msg.get("is_bot"):
            continue
        text = msg.get("text", "").strip()
        if not text:
            continue

        dt_str = msg.get("datetime", "")
        if dt_str:
            # "2026-02-20 15:39:54" -> "02/20 15:39"
            try:
                dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")
                time_str = dt.strftime("%m/%d %H:%M")
            except ValueError:
                time_str = dt_str[:16]
        else:
            time_str = "??/?? ??:??"

        is_reply = msg.get("is_thread_reply", False)
        prefix = "  [reply] " if is_reply else ""
        user_name = msg.get("user_name", "")
        user_tag = f"({user_name}) " if user_name else ""
        lines.append(f"[{time_str}] {user_tag}{prefix}{text}")

    formatted = "\n".join(lines)
    print(f"    Formatted text: {len(formatted)} chars, {len(lines)} lines")
    return formatted, True


# -- Slack History (fallback) --

def fetch_slack_history(start_dt, end_dt):
    """Fetch channel messages with 1-month parent window + thread replies."""
    wide_oldest = str((start_dt - timedelta(days=30)).timestamp())
    latest = str(end_dt.timestamp())
    report_oldest = str(start_dt.timestamp())

    parent_messages = []
    cursor = None
    while True:
        try:
            kwargs = {
                "channel": SLACK_CHANNEL_ID,
                "oldest": wide_oldest,
                "latest": latest,
                "limit": 200,
                "inclusive": True,
            }
            if cursor:
                kwargs["cursor"] = cursor

            result = slack_client.conversations_history(**kwargs)
            parent_messages.extend(result["messages"])

            if not result.get("has_more"):
                break
            cursor = result["response_metadata"]["next_cursor"]

        except SlackApiError as e:
            print(f"ERROR Slack API: {e.response['error']}")
            sys.exit(1)

    print(f"  Parents fetched (1mo window): {len(parent_messages)}")

    all_messages = []
    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    for msg in parent_messages:
        msg_ts = float(msg["ts"])
        parent_in_range = start_ts <= msg_ts <= end_ts

        if parent_in_range:
            all_messages.append(msg)

        if msg.get("reply_count", 0) > 0:
            try:
                thread_result = slack_client.conversations_replies(
                    channel=SLACK_CHANNEL_ID,
                    ts=msg["ts"],
                    oldest=report_oldest,
                    latest=latest,
                    limit=200,
                )
                replies_in_range = []
                for reply in thread_result.get("messages", []):
                    if reply["ts"] == msg["ts"]:
                        continue
                    reply_ts = float(reply["ts"])
                    if start_ts <= reply_ts <= end_ts:
                        replies_in_range.append(reply)

                if replies_in_range:
                    if not parent_in_range:
                        all_messages.append(msg)
                    all_messages.extend(replies_in_range)

            except SlackApiError as e:
                print(f"WARNING: thread fetch failed: {e.response['error']}")

    seen = set()
    unique = []
    for msg in all_messages:
        if msg["ts"] not in seen:
            seen.add(msg["ts"])
            unique.append(msg)

    unique.sort(key=lambda m: float(m["ts"]))
    print(f"  Messages in report period: {len(unique)}")
    return unique


def get_bot_user_id():
    try:
        result = slack_client.auth_test()
        return result.get("bot_id") or result.get("user_id")
    except SlackApiError:
        return None


BOT_ID = None


def format_slack_messages(messages):
    lines = []
    for msg in messages:
        if BOT_ID and msg.get("bot_id") == BOT_ID:
            continue
        ts = datetime.fromtimestamp(float(msg["ts"]), tz=KST)
        time_str = ts.strftime("%m/%d %H:%M")
        text = msg.get("text", "").strip()
        if not text:
            continue
        is_reply = "thread_ts" in msg and msg.get("thread_ts") != msg.get("ts")
        prefix = "  [reply] " if is_reply else ""
        lines.append(f"[{time_str}] {prefix}{text}")
    return "\n".join(lines)


# -- Report Logic --

def get_date_range(today):
    """Weekly: previous Friday 00:00 ~ this Thursday 23:59:59 KST."""
    days_since_friday = (today.weekday() - 4) % 7
    this_friday = today - timedelta(days=days_since_friday)
    prev_friday = this_friday - timedelta(days=7)
    this_thursday = this_friday - timedelta(days=1)

    start = datetime(prev_friday.year, prev_friday.month, prev_friday.day,
                     0, 0, 0, tzinfo=KST)
    end = datetime(this_thursday.year, this_thursday.month, this_thursday.day,
                   23, 59, 59, tzinfo=KST)

    date_label = (
        f"{prev_friday.strftime('%m/%d')} 00:00 ~ "
        f"{this_thursday.strftime('%m/%d')} 23:59 KST"
    )
    return start, end, date_label


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


# -- Claude API --

def generate_report_with_claude(slack_text, date_label, guide, feedback_text):
    system_prompt = (
        "You are a senior manager of VCMS (accommodation channel manager) operations team.\n"
        "Analyze Slack channel messages and write a weekly summary report.\n\n"
        "Follow this guide:\n\n"
        f"{guide}\n\n"
        "Additional instructions:\n"
        "- CRITICAL: ONLY state facts explicitly mentioned in the messages above\n"
        "- NEVER infer, assume, or fabricate information not in the messages\n"
        "- If something is unclear, say '확인 필요' rather than guessing\n"
        "- Do NOT add background context or history that is not in the messages\n"
        "- Numbers must exactly match what appears in the messages\n"
        "- CRITICAL COUNTING RULES:\n"
        "  * 유입 건수: 신규 신청된 숙박업소 수. 동일 업소 중복 신청은 1건으로 카운트\n"
        "  * 완료 건수: '교육완료' 또는 완료 이모지(✅ 등)가 명시된 건만 카운트. 해당 기간 유입 건에 한정하지 않음 (이전 주 유입 건 완료 포함)\n"
        "  * 미결 건수: 단순히 '유입-완료'로 계산하지 마라. 채널에서 아직 완료 표시 안 된 진행 중인 건만 카운트\n"
        "  * 교육예정: '예정', '스케줄', 날짜가 명시된 건만 카운트. 추측하지 마라\n"
        "- IMPORTANT: When citing any number, ALWAYS show the CRITERIA used to count\n"
        "  Good example: '주간 신규 유입: 7건 (기준: 신규 신청 메시지, 중복 업소 2건 제외)'\n"
        "  Bad example: '주간 총 유입: 13건'\n"
        "- For each blocker/issue, ALWAYS include: what happened, how long it took, and suggested action\n"
        "- Action items must be SPECIFIC: include venue name, responsible action, and deadline when available\n"
        "  Good: '강릉 솔바람 펜션 담당자 배정 및 교육 일정 확보'\n"
        "  Bad: '미완료 건 처리'\n"
        "- Keep the report compact and scannable. No unnecessary repetition\n"
        "- Include specific names (venues, staff) ONLY if they appear in messages\n"
        "- '기술 이슈' 대신 '교육간 특이사항'이라는 용어를 사용할 것\n"
        "- Provide root cause analysis based ONLY on evidence in messages\n"
        "- Suggest improvements only when patterns are clearly visible in the data\n"
        "- Write in Korean\n"
        "- CRITICAL: Use Slack mrkdwn format, NOT standard Markdown\n"
        "- Bold: *single asterisk* (NOT **double**)\n"
        "- No ### or #### headers. Use *bold text* with emoji for sections\n"
        "- Italic: _underscore_ (NOT *asterisk*)\n"
        "- Lists: use bullet or numbered 1. 2. 3.\n"
        "- Divider: use three dashes\n"
        "- Example section header: *section title here*\n"
    )

    feedback_section = ""
    if feedback_text:
        feedback_section = (
            "\n\n---THIS WEEK'S FEEDBACK---\n"
            "Below is feedback received during this report period.\n"
            "Summarize these in a dedicated section at the end of the report.\n"
            "For each feedback: state what was requested and what action was taken (or will be taken).\n\n"
            f"{feedback_text}\n"
            "---FEEDBACK END---\n"
        )

    user_prompt = (
        f"Below are Slack messages from #vendit-system-noti for {date_label}.\n"
        f"Messages marked [reply] are thread replies.\n"
        f"Please write the weekly summary report.\n"
        f"{feedback_section}\n"
        f"---SLACK MESSAGES START---\n"
        f"{slack_text}\n"
        f"---SLACK MESSAGES END---"
    )

    response = claude_client.messages.create(
        model="claude-sonnet-4-5-20250514",
        max_tokens=2000,
        temperature=0.3,
        system=system_prompt,
        messages=[
            {"role": "user", "content": user_prompt}
        ],
    )

    return response.content[0].text


# -- Slack Posting with Block Kit --

def post_to_slack(report_text, date_label):
    full_message = f"*주간 리포트*  |  {date_label}\n───\n\n{report_text}"

    try:
        result = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=full_message,
            mrkdwn=True,
        )
        report_ts = result["ts"]
        print(f"OK Slack posted: ts={report_ts}")

        feedback_blocks = [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "\ud83d\udcac \ud53c\ub4dc\ubc31 \ud558\uae30",
                            "emoji": True,
                        },
                        "action_id": "feedback_button",
                        "style": "primary",
                    },
                ],
            },
        ]
        slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            thread_ts=report_ts,
            text="\ud53c\ub4dc\ubc31\uc744 \ub0a8\uaca8\uc8fc\uc138\uc694",
            blocks=feedback_blocks,
        )
        print("OK Feedback button posted in thread")

        return report_ts
    except SlackApiError as e:
        print(f"ERROR Slack post failed: {e.response['error']}")
        return None


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

    global BOT_ID
    BOT_ID = get_bot_user_id()
    print(f"Bot ID: {BOT_ID}")

    # 1. Get date range
    start_dt, end_dt, date_label = get_date_range(today)
    print(f"Report period: {date_label}")

    # 2. Fetch weekly feedback from Worker
    print("Fetching weekly feedback...")
    feedback_list = fetch_weekly_feedback(start_dt, end_dt)
    feedback_text = format_feedback_for_prompt(feedback_list)
    if feedback_list:
        print(f"Weekly feedback entries: {len(feedback_list)}")

    # 3. Load messages: backup first, fallback to Slack API
    print("Loading messages...")
    slack_text, from_backup = load_from_backup(start_dt)

    if not from_backup:
        print("  Backup not available, falling back to Slack API...")
        messages = fetch_slack_history(start_dt, end_dt)
        print(f"  Messages collected: {len(messages)}")

        if len(messages) == 0:
            print("No messages found. Posting null report.")
            null_report = "해당 기간 채널에 기록된 메시지가 없습니다. 추가 보고 사항이 있으면 스레드에 남겨주세요."
            post_to_slack(null_report, date_label)
            return

        slack_text = format_slack_messages(messages)
    else:
        if not slack_text:
            print("Backup loaded but no messages. Posting null report.")
            null_report = "해당 기간 채널에 기록된 메시지가 없습니다. 추가 보고 사항이 있으면 스레드에 남겨주세요."
            post_to_slack(null_report, date_label)
            return

    print(f"Formatted text: {len(slack_text)} chars")

    # 4. Generate report with Claude
    guide = load_guide()
    print("Calling Claude API...")
    report = generate_report_with_claude(slack_text, date_label, guide, feedback_text)
    report = convert_to_slack_mrkdwn(report)
    print(f"Report generated ({len(report)} chars)")

    # 5. Post to Slack
    post_to_slack(report, date_label)

    print("All done!")


if __name__ == "__main__":
    main()
