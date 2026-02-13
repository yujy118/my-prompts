"""VCMS Slack Auto Report Generator

Flow:
1. Holiday/business day check
2. Fetch accumulated feedback from Cloudflare Worker
3. Fetch Slack channel messages + thread replies
4. Generate report via Claude API (with feedback)
5. Post to Slack with '피드백 하기' button
6. Save to Notion
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

FORCE_TYPE = os.environ.get("FORCE_TYPE", "auto")
FEEDBACK_WORKER_URL = os.environ.get("FEEDBACK_WORKER_URL", "")

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


def format_feedback_for_prompt(feedback_list):
    """Format accumulated feedback for Claude prompt."""
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
        text = entry.get("text", "")
        lines.append(f"[{date}] [{cat}] {text}")

    return "\n".join(lines)


# -- Slack History --

def fetch_slack_history(start_dt, end_dt):
    """Fetch channel messages + all thread replies."""
    messages = []
    oldest = str(start_dt.timestamp())
    latest = str(end_dt.timestamp())
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
            messages.extend(result["messages"])

            if not result.get("has_more"):
                break
            cursor = result["response_metadata"]["next_cursor"]

        except SlackApiError as e:
            print(f"ERROR Slack API: {e.response['error']}")
            sys.exit(1)

    # Fetch thread replies
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


# -- Report Logic --

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


# -- Claude API --

def generate_report_with_claude(slack_text, report_type, date_label, guide, feedback_text):
    if report_type == "daily":
        case_instruction = "[Case A] VCMS daily quick report format"
    else:
        case_instruction = "[Case B] VCMS weekly operation report format"

    system_prompt = (
        "You are a senior manager of VCMS (accommodation channel manager) operations team.\n"
        "Analyze Slack channel messages and write a report.\n\n"
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
        "- When citing any number, show the CRITERIA used to count, not individual items\n"
        "  Good: '주간 신규 유입: 11건 (기준: 신규 신청 메시지, 중복 업소 2건 제외)'\n"
        "  Bad: '주간 총 유입: 13건 (02/10 호텔A, 02/10 호텔B...)'\n"
        "- Keep the report compact and scannable. No unnecessary repetition\n"
        "- Include specific names (venues, staff) ONLY if they appear in messages\n"
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
            "\n\n---ACCUMULATED TEAM FEEDBACK---\n"
            "Below is accumulated feedback from team members across all previous reports.\n"
            "These are PERMANENT corrections and preferences. ALWAYS apply them:\n"
            "- If feedback says something is NOT a certain category, never categorize it that way\n"
            "- If feedback corrects a factual error, always use the corrected version\n"
            "- If feedback requests a format change, always apply it\n\n"
            f"{feedback_text}\n"
            "---FEEDBACK END---\n"
        )

    user_prompt = (
        f"Below are Slack messages from #system-vcms-noti for {date_label}.\n"
        f"Messages marked [reply] are thread replies.\n"
        f"Please write the report in {case_instruction}.\n"
        f"{feedback_section}\n"
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


# -- Slack Posting with Block Kit --

def post_to_slack(report_text, report_type, date_label):
    if report_type == "daily":
        type_label = "VCMS 일간 리포트"
    else:
        type_label = "VCMS 주간 리포트"

    full_message = f"*{type_label}*  |  {date_label}\n───\n\n{report_text}"

    try:
        # 1. Post report as main message
        result = slack_client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=full_message,
            mrkdwn=True,
        )
        report_ts = result["ts"]
        print(f"OK Slack posted: ts={report_ts}")

        # 2. Post feedback button as thread reply
        feedback_blocks = [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": "💬 피드백 하기",
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
            text="피드백을 남겨주세요",
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

    report_type = determine_report_type(today)
    print(f"Report type: {report_type}")

    # 1. Fetch accumulated feedback from Worker
    print("Fetching accumulated feedback...")
    feedback_list = fetch_accumulated_feedback()
    feedback_text = format_feedback_for_prompt(feedback_list)
    if feedback_list:
        print(f"Total feedback entries: {len(feedback_list)}")

    # 2. Collect Slack messages
    start_dt, end_dt, date_label = get_date_range(today, report_type)
    print(f"Collecting: {date_label}")

    messages = fetch_slack_history(start_dt, end_dt)
    print(f"Messages collected: {len(messages)}")

    if len(messages) == 0:
        print("No messages found. Skipping.")
        return

    slack_text = format_slack_messages(messages)
    print(f"Formatted text: {len(slack_text)} chars")

    # 3. Generate report with Claude
    guide = load_guide()
    print("Calling Claude API...")
    report = generate_report_with_claude(slack_text, report_type, date_label, guide, feedback_text)
    report = convert_to_slack_mrkdwn(report)
    print(f"Report generated ({len(report)} chars)")

    # 4. Post to Slack
    post_to_slack(report, report_type, date_label)

    print("All done!")


if __name__ == "__main__":
    main()
