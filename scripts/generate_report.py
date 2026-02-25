"""VCMS Slack Auto Report Generator

Flow:
1. Holiday/business day check
2. Fetch accumulated feedback from Cloudflare Worker
3. Fetch Slack channel messages + thread replies
4. Generate report via Gemini API (with feedback)
5. Post to Slack with feedback button
"""

import os
import sys
import re
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import google.generativeai as genai
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
import requests

from korean_holidays import is_business_day, is_korean_holiday

# -- Config --
KST = timezone(timedelta(hours=9))
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNEL_ID = os.environ.get("SLACK_CHANNEL_ID", "C0884BV1KNV")
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

FEEDBACK_WORKER_URL = os.environ.get("FEEDBACK_WORKER_URL", "")

slack_client = WebClient(token=SLACK_BOT_TOKEN)
genai.configure(api_key=GEMINI_API_KEY)

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
    """Format accumulated feedback for prompt."""
    if not feedback_list:
        return ""

    category_labels = {
        "correction": "\uc0ac\uc2e4 \uc624\ub958 \uc218\uc815",
        "categorization": "\ubd84\ub958 \uae30\uc900 \ubcc0\uacbd",
        "format": "\ud3ec\ub9f7/\ud615\uc2dd \ubcc0\uacbd",
        "general": "\uae30\ud0c0 \uc758\uacac",
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


def get_bot_user_id():
    """Get this bot's own user ID to filter out self-messages."""
    try:
        result = slack_client.auth_test()
        return result.get("bot_id") or result.get("user_id")
    except SlackApiError:
        return None


BOT_ID = None  # initialized in main()


def format_slack_messages(messages):
    lines = []
    for msg in messages:
        # Skip this bot's own messages (previous reports)
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
    """Weekly: previous Monday 00:00 ~ previous Sunday 23:59:59 KST."""
    last_monday = today - timedelta(days=today.weekday() + 7)
    last_sunday = last_monday + timedelta(days=6)
    start = datetime(last_monday.year, last_monday.month, last_monday.day, 0, 0, 0, tzinfo=KST)
    end = datetime(last_sunday.year, last_sunday.month, last_sunday.day, 23, 59, 59, tzinfo=KST)
    return start, end, f"{last_monday.strftime('%m/%d')}~{last_sunday.strftime('%m/%d')}"


def convert_to_slack_mrkdwn(text):
    """Force convert Markdown to Slack mrkdwn."""
    lines = text.split('\n')
    result = []
    for line in lines:
        line = re.sub(r'^#{1,6}\s+', '', line)
        line = line.replace('**', '*')
        if re.match(r'^-{3,}$', line.strip()):
            line = '\u2500\u2500\u2500'
        result.append(line)
    return '\n'.join(result)


# -- Gemini API --

def generate_report_with_gemini(slack_text, date_label, guide, feedback_text):
    system_instruction = (
        "You are a senior manager of VCMS (accommodation channel manager) operations team.\n"
        "Analyze Slack channel messages and write a weekly summary report.\n\n"
        "Follow this guide:\n\n"
        f"{guide}\n\n"
        "Additional instructions:\n"
        "- CRITICAL: ONLY state facts explicitly mentioned in the messages above\n"
        "- NEVER infer, assume, or fabricate information not in the messages\n"
        "- If something is unclear, say '\ud655\uc778 \ud544\uc694' rather than guessing\n"
        "- Do NOT add background context or history that is not in the messages\n"
        "- Numbers must exactly match what appears in the messages\n"
        "- CRITICAL COUNTING RULES:\n"
        "  * \uc720\uc785 \uac74\uc218: \uc2e0\uaddc \uc2e0\uccad\ub41c \uc219\ubc15\uc5c5\uc18c \uc218. \ub3d9\uc77c \uc5c5\uc18c \uc911\ubcf5 \uc2e0\uccad\uc740 1\uac74\uc73c\ub85c \uce74\uc6b4\ud2b8\n"
        "  * \uc644\ub8cc \uac74\uc218: '\uad50\uc721\uc644\ub8cc' \ub610\ub294 \uc644\ub8cc \uc774\ubaa8\uc9c0(\u2705 \ub4f1)\uac00 \uba85\uc2dc\ub41c \uac74\ub9cc \uce74\uc6b4\ud2b8. \ud574\ub2f9 \uae30\uac04 \uc720\uc785 \uac74\uc5d0 \ud55c\uc815\ud558\uc9c0 \uc54a\uc74c (\uc774\uc804 \uc8fc \uc720\uc785 \uac74 \uc644\ub8cc \ud3ec\ud568)\n"
        "  * \ubbf8\uacb0 \uac74\uc218: \ub2e8\uc21c\ud788 '\uc720\uc785-\uc644\ub8cc'\ub85c \uacc4\uc0b0\ud558\uc9c0 \ub9c8\ub77c. \ucc44\ub110\uc5d0\uc11c \uc544\uc9c1 \uc644\ub8cc \ud45c\uc2dc \uc548 \ub41c \uc9c4\ud589 \uc911\uc778 \uac74\ub9cc \uce74\uc6b4\ud2b8\n"
        "  * \uad50\uc721\uc608\uc815: '\uc608\uc815', '\uc2a4\ucf00\uc904', \ub0a0\uc9dc\uac00 \uba85\uc2dc\ub41c \uac74\ub9cc \uce74\uc6b4\ud2b8. \ucd94\uce21\ud558\uc9c0 \ub9c8\ub77c\n"
        "- IMPORTANT: When citing any number, ALWAYS show the CRITERIA used to count\n"
        "  Good example: '\uc8fc\uac04 \uc2e0\uaddc \uc720\uc785: 7\uac74 (\uae30\uc900: \uc2e0\uaddc \uc2e0\uccad \uba54\uc2dc\uc9c0, \uc911\ubcf5 \uc5c5\uc18c 2\uac74 \uc81c\uc678)'\n"
        "  Bad example: '\uc8fc\uac04 \ucd1d \uc720\uc785: 13\uac74'\n"
        "- For each blocker/issue, ALWAYS include: what happened, how long it took, and suggested action\n"
        "- Action items must be SPECIFIC: include venue name, responsible action, and deadline when available\n"
        "  Good: '\uac15\ub989 \uc194\ubc14\ub78c \ud39c\uc158 \ub2f4\ub2f9\uc790 \ubc30\uc815 \ubc0f \uad50\uc721 \uc77c\uc815 \ud655\ubcf4'\n"
        "  Bad: '\ubbf8\uc644\ub8cc \uac74 \ucc98\ub9ac'\n"
        "- Keep the report compact and scannable. No unnecessary repetition\n"
        "- Include specific names (venues, staff) ONLY if they appear in messages\n"
        "- '\uae30\uc220 \uc774\uc288' \ub300\uc2e0 '\uad50\uc721\uac04 \ud2b9\uc774\uc0ac\ud56d'\uc774\ub77c\ub294 \uc6a9\uc5b4\ub97c \uc0ac\uc6a9\ud560 \uac83\n"
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
        f"Please write the weekly summary report.\n"
        f"{feedback_section}\n"
        f"---SLACK MESSAGES START---\n"
        f"{slack_text}\n"
        f"---SLACK MESSAGES END---"
    )

    model = genai.GenerativeModel(
        "gemini-2.0-flash",
        system_instruction=system_instruction,
    )

    response = model.generate_content(
        user_prompt,
        generation_config=genai.types.GenerationConfig(
            max_output_tokens=2000,
            temperature=0.3,
        ),
    )

    return response.text


# -- Slack Posting with Block Kit --

def post_to_slack(report_text, date_label):
    full_message = f"*\uc8fc\uac04 \ub9ac\ud3ec\ud2b8*  |  {date_label}\n\u2500\u2500\u2500\n\n{report_text}"

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

    # Init bot ID for self-message filtering
    global BOT_ID
    BOT_ID = get_bot_user_id()
    print(f"Bot ID: {BOT_ID}")

    # 1. Fetch accumulated feedback from Worker
    print("Fetching accumulated feedback...")
    feedback_list = fetch_accumulated_feedback()
    feedback_text = format_feedback_for_prompt(feedback_list)
    if feedback_list:
        print(f"Total feedback entries: {len(feedback_list)}")

    # 2. Collect Slack messages (previous Mon~Sun)
    start_dt, end_dt, date_label = get_date_range(today)
    print(f"Collecting: {date_label}")

    messages = fetch_slack_history(start_dt, end_dt)
    print(f"Messages collected: {len(messages)}")

    if len(messages) == 0:
        print("No messages found. Posting null report.")
        null_report = "\ud574\ub2f9 \uae30\uac04 \ucc44\ub110\uc5d0 \uae30\ub85d\ub41c \uba54\uc2dc\uc9c0\uac00 \uc5c6\uc2b5\ub2c8\ub2e4. \ucd94\uac00 \ubcf4\uace0 \uc0ac\ud56d\uc774 \uc788\uc73c\uba74 \uc2a4\ub808\ub4dc\uc5d0 \ub0a8\uaca8\uc8fc\uc138\uc694."
        post_to_slack(null_report, date_label)
        return

    slack_text = format_slack_messages(messages)
    print(f"Formatted text: {len(slack_text)} chars")

    # 3. Generate report with Gemini
    guide = load_guide()
    print("Calling Gemini API...")
    report = generate_report_with_gemini(slack_text, date_label, guide, feedback_text)
    report = convert_to_slack_mrkdwn(report)
    print(f"Report generated ({len(report)} chars)")

    # 4. Post to Slack
    post_to_slack(report, date_label)

    print("All done!")


if __name__ == "__main__":
    main()
