import slack
import os
import re
import time
from pathlib import Path
from dotenv import load_dotenv
from datetime import datetime

# Load .env FIRST before accessing any environment variables
env_path = Path(".") / ".env"
load_dotenv(dotenv_path=env_path)

client = slack.WebClient(token=os.environ["SLACK_TOKEN"])

# Configuration - PRODUCTION MODE
MONITORED_CHANNEL = "#enterprise-customers-followup"  # Channel to monitor for tickets
ALERTS_CHANNEL = "#ip_reminder"  # Channel to send reminders to
CHECK_INTERVAL_HOURS = 6  # Check every 6 hours
CHECKMARK_EMOJI = "white_check_mark"  # The checkmark emoji name (✅)
CHECKED_EMOJI = "eyes"  # Emoji to mark message as checked (👀)

# Track: original_message_ts -> reminder_message_ts (so we can delete reminders)
sent_reminders = {}


def get_channel_id(channel_name):
    """Convert channel name to channel ID."""
    channel_name = channel_name.lstrip("#")

    try:
        cursor = None
        while True:
            result = client.conversations_list(
                types="public_channel,private_channel", cursor=cursor, limit=200
            )
            for channel in result["channels"]:
                if channel["name"] == channel_name:
                    return channel["id"]

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    except slack.errors.SlackApiError as e:
        print(f"Error getting channel list: {e}")

    print(f"Channel '{channel_name}' not found.")
    return None


def get_all_messages(channel_id):
    """Fetch ALL messages from the channel (with pagination)."""
    all_messages = []
    cursor = None

    try:
        while True:
            result = client.conversations_history(
                channel=channel_id, cursor=cursor, limit=200  # Max per request
            )
            messages = result.get("messages", [])
            all_messages.extend(messages)

            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return all_messages
    except slack.errors.SlackApiError as e:
        print(f"Error fetching messages: {e}")
        return []


def has_checkmark_reaction(channel_id, message_ts):
    """Check if a message has a checkmark emoji reaction."""
    try:
        result = client.reactions_get(channel=channel_id, timestamp=message_ts)
        reactions = result.get("message", {}).get("reactions", [])
        for reaction in reactions:
            if reaction["name"] == CHECKMARK_EMOJI:
                return True
        return False
    except slack.errors.SlackApiError as e:
        if "no_item_specified" in str(e) or "message_not_found" in str(e):
            return False
        print(f"Error checking reactions: {e}")
        return False


def extract_ticket_info(message_text):
    """Extract key fields from the ticket message."""
    info = {}

    patterns = {
        "Date": r"Date:\s*(.+?)(?:\n|$)",
        "Province": r"Province:\s*(.+?)(?:\n|$)",
        "Project": r"Project:\s*(.+?)(?:\n|$)",
        "Type": r"Type:\s*(.+?)(?:\n|$)",
        "Customer": r"Customer(?:\s*Name)?:\s*(.+?)(?:\n|$)",
        "Description": r"Description:\s*(.+?)(?:\n|$)",
    }

    for field, pattern in patterns.items():
        match = re.search(pattern, message_text, re.IGNORECASE)
        if match:
            info[field] = match.group(1).strip()

    return info


def send_reminder(alerts_channel_id, original_message, monitored_channel_id):
    """Send a reminder to the alerts channel. Returns the reminder message ts."""
    message_text = original_message.get("text", "")
    message_ts = original_message.get("ts", "")

    # Create message link
    message_link = f"https://slack.com/archives/{monitored_channel_id}/p{message_ts.replace('.', '')}"

    # Extract ticket info
    ticket_info = extract_ticket_info(message_text)

    # Build a clean reminder message
    reminder_lines = ["⚠️ *Pending Ticket - Needs Attention*", ""]

    if ticket_info.get("Date"):
        reminder_lines.append(f"📅 *Date:* {ticket_info['Date']}")
    if ticket_info.get("Province"):
        reminder_lines.append(f"📍 *Province:* {ticket_info['Province']}")
    if ticket_info.get("Project"):
        reminder_lines.append(f"📁 *Project:* {ticket_info['Project']}")
    if ticket_info.get("Type"):
        reminder_lines.append(f"🏷️ *Type:* {ticket_info['Type']}")
    if ticket_info.get("Customer"):
        reminder_lines.append(f"👤 *Customer:* {ticket_info['Customer']}")
    if ticket_info.get("Description"):
        reminder_lines.append(f"📝 *Description:* {ticket_info['Description']}")

    reminder_lines.append("")
    reminder_lines.append(f"<{message_link}|🔗 View Original Message>")
    reminder_lines.append("")
    reminder_lines.append("_React with ✅ on the original message when handled._")

    reminder_text = "\n".join(reminder_lines)

    try:
        result = client.chat_postMessage(channel=alerts_channel_id, text=reminder_text)
        reminder_ts = result.get("ts")
        print("✅ Sent reminder to alerts channel")
        return reminder_ts
    except slack.errors.SlackApiError as e:
        print(f"Error sending reminder: {e}")
        return None


def delete_reminder(alerts_channel_id, reminder_ts):
    """Delete a reminder message from the alerts channel."""
    try:
        client.chat_delete(channel=alerts_channel_id, ts=reminder_ts)
        print("🗑️ Deleted reminder (resolved with ✅)")
        return True
    except slack.errors.SlackApiError as e:
        print(f"Error deleting reminder: {e}")
        return False


def reply_to_original_thread(channel_id, thread_ts):
    """Add a reply to the original message thread with check time."""
    check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    reply_text = (
        f"🔔 *Reminder Check*\n_Checked at {check_time} - No ✅ found. Reminder sent._"
    )

    try:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=reply_text
        )
        print("📝 Replied to original thread")
    except slack.errors.SlackApiError as e:
        print(f"Error replying to thread: {e}")


def add_checked_reaction(channel_id, message_ts):
    """Add a 👀 reaction to mark message as checked."""
    try:
        client.reactions_add(
            channel=channel_id, name=CHECKED_EMOJI, timestamp=message_ts
        )
        print("👀 Added checked reaction to original message")
    except slack.errors.SlackApiError as e:
        if "already_reacted" not in str(e):
            print(f"Error adding reaction: {e}")


def check_and_remind():
    """Main function to check messages and send reminders."""
    check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n{'='*50}")
    print(f"[{check_time}] Starting check...")
    print(f"{'='*50}")

    # Get channel IDs
    monitored_channel_id = get_channel_id(MONITORED_CHANNEL)
    alerts_channel_id = get_channel_id(ALERTS_CHANNEL)

    if not monitored_channel_id:
        print(f"Error: Could not find monitored channel: {MONITORED_CHANNEL}")
        return

    if not alerts_channel_id:
        print(f"Error: Could not find alerts channel: {ALERTS_CHANNEL}")
        return

    # Get ALL messages (not just recent)
    messages = get_all_messages(monitored_channel_id)
    print(f"Found {len(messages)} total messages in channel")

    reminders_sent = 0
    reminders_deleted = 0

    for message in messages:
        message_ts = message.get("ts", "")
        message_text = message.get("text", "")

        # Skip system messages (like channel join/leave) but ALLOW bot/workflow messages
        if message.get("subtype") in [
            "channel_join",
            "channel_leave",
            "channel_topic",
            "channel_purpose",
        ]:
            continue

        # Check for checkmark reaction
        has_checkmark = has_checkmark_reaction(monitored_channel_id, message_ts)

        # If message now has ✅ and we sent a reminder for it, DELETE the reminder
        if has_checkmark and message_ts in sent_reminders:
            reminder_ts = sent_reminders[message_ts]
            if delete_reminder(alerts_channel_id, reminder_ts):
                del sent_reminders[message_ts]
                reminders_deleted += 1
            continue

        # If message has ✅, skip (resolved)
        if has_checkmark:
            continue

        # Skip messages we've already alerted about
        if message_ts in sent_reminders:
            continue

        # No checkmark - send reminder!
        print(f"\n📋 Found unresolved ticket: {message_text[:50]}...")

        # 1. Send reminder to alerts channel
        reminder_ts = send_reminder(alerts_channel_id, message, monitored_channel_id)

        if reminder_ts:
            reminders_sent += 1
            sent_reminders[message_ts] = reminder_ts

            # 2. Reply to the original thread with check time
            reply_to_original_thread(monitored_channel_id, message_ts)

            # 3. Add 👀 reaction to mark it was checked
            add_checked_reaction(monitored_channel_id, message_ts)

    print(f"\n{'='*50}")
    print(
        f"Summary: Sent {reminders_sent} reminders, deleted {reminders_deleted} resolved"
    )
    print(f"{'='*50}")


def run_reminder_loop():
    """Run the reminder check in a loop."""
    check_interval_seconds = CHECK_INTERVAL_HOURS * 60 * 60

    print("=" * 60)
    print("Slack Reminder Bot - PRODUCTION MODE")
    print("=" * 60)
    print(f"Monitoring: {MONITORED_CHANNEL}")
    print(f"Alerts to: {ALERTS_CHANNEL}")
    print(f"Check interval: Every {CHECK_INTERVAL_HOURS} hours")
    print("=" * 60)
    print("\nLogic:")
    print("  1. Check ALL messages in channel")
    print("  2. No ✅ → Send reminder + Reply to thread + Add 👀")
    print("  3. ✅ added → Delete the reminder")
    print("=" * 60)

    while True:
        try:
            check_and_remind()
        except Exception as e:
            print(f"Error during check: {e}")

        next_check = datetime.now().strftime("%H:%M:%S")
        print(
            f"\nNext check in {CHECK_INTERVAL_HOURS} hours (started at {next_check})..."
        )
        time.sleep(check_interval_seconds)


if __name__ == "__main__":
    run_reminder_loop()
