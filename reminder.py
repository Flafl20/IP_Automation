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
MONITORED_CHANNEL = "#ip-test"  # Channel to monitor for tickets
ALERTS_CHANNEL = "#ip_reminder"  # Channel to send reminders to
CHECK_INTERVAL_HOURS = 0.003  # Check every 6 hours
CHECKMARK_EMOJI = "white_check_mark"  # The checkmark emoji name (✅)
CHECKED_EMOJI = "eyes"  # Emoji to mark message as checked (👀)

# Track: original_message_ts -> reminder_message_ts (so we can delete reminders)
sent_reminders = {}

# Track messages that have been marked as resolved (to post Date Ended only once)
resolved_messages = set()


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


def extract_sender_from_ticket(message_text):
    """Extract sender mention from ticket message."""
    # Look for Slack user ID format anywhere after "Sender:" - handles *<@U123>* or <@U123>
    match = re.search(r"Sender:.*?<@([A-Z0-9]+)>", message_text, re.IGNORECASE)
    if match:
        user_id = match.group(1)
        return user_id, f"<@{user_id}>"

    # Fallback: try plain text format Sender: @Name or Sender: Name
    match = re.search(r"Sender:\s*\*?@?([^*\n<>]+)", message_text, re.IGNORECASE)
    if match:
        name = match.group(1).strip()
        if name:
            return None, f"@{name}"

    return None, None


def extract_team_from_ticket(message_text):
    """Extract To Team mention from ticket message."""
    # Look for Slack user/group ID format after "To Team:" - handles *<@U123>* or <!subteam^S123>
    match = re.search(r"To Team:.*?<[@!]([A-Z0-9^]+)>", message_text, re.IGNORECASE)
    if match:
        team_id = match.group(1)
        # Handle subteam format like "subteam^S123ABC"
        if "^" in team_id:
            team_id = team_id.split("^")[1]
            return team_id, f"<!subteam^{team_id}>"
        return team_id, f"<@{team_id}>"

    # Fallback: try plain text format To Team: @team-name
    match = re.search(r"To Team:.*?@([\w-]+)", message_text, re.IGNORECASE)
    if match:
        team_name = match.group(1).strip()
        if team_name:
            return None, f"@{team_name}"

    return None, None


def get_thread_replies(channel_id, thread_ts):
    """Get all replies in a thread."""
    try:
        result = client.conversations_replies(channel=channel_id, ts=thread_ts)
        messages = result.get("messages", [])
        # First message is the parent, rest are replies
        return messages[1:] if len(messages) > 1 else []
    except slack.errors.SlackApiError as e:
        print(f"Error fetching thread replies: {e}")
        return []


def get_last_human_replier(replies):
    """Get the user ID of the last person who replied (excluding bot messages)."""
    for reply in reversed(replies):
        if not reply.get("bot_id") and not reply.get("subtype"):
            return reply.get("user")
    return None


def reply_to_original_thread(channel_id, thread_ts, original_message):
    """Add a reply to the original message thread with check time and tag the right person."""
    check_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    message_text = original_message.get("text", "")

    # Extract sender and team from ticket
    sender_id, sender_mention = extract_sender_from_ticket(message_text)
    team_id, team_mention = extract_team_from_ticket(message_text)

    print(f"   Sender: {sender_mention}, Team: {team_mention}")

    # Get thread replies to determine who to mention
    replies = get_thread_replies(channel_id, thread_ts)
    last_replier = get_last_human_replier(replies)

    # Determine who to remind based on last reply
    if not replies:
        # No replies yet - remind the sender
        who_to_remind = sender_mention or team_mention
        reason = "No replies yet"
    elif sender_id and last_replier == sender_id:
        # Sender replied last - remind the team
        who_to_remind = team_mention or sender_mention
        reason = "Sender replied, waiting for team"
    elif team_id and last_replier == team_id:
        # Team replied last - remind the sender
        who_to_remind = sender_mention or team_mention
        reason = "Team replied, waiting for sender"
    else:
        # Someone else replied - remind the sender by default
        who_to_remind = sender_mention or team_mention
        reason = "Waiting for response"

    print(f"   Reminding: {who_to_remind} ({reason})")

    # Build reply with mention
    if who_to_remind:
        reply_text = (
            f"🔔 *Reminder Check*\n"
            f"Hey {who_to_remind}! _Checked at {check_time} - No ✅ found._\n"
            f"_{reason}_\n"
            f"Please react with ✅ when this is resolved."
        )
    else:
        reply_text = f"🔔 *Reminder Check*\n_Checked at {check_time} - No ✅ found. Reminder sent._"

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


def post_date_ended(channel_id, thread_ts):
    """Post the Date Ended to the thread when ticket is resolved."""
    end_time = datetime.now().strftime("%B %d, %Y at %I:%M %p GMT+3")

    reply_text = f"✅ *Ticket Resolved*\n" f"📅 *Date Ended:* {end_time}"

    try:
        client.chat_postMessage(
            channel=channel_id, thread_ts=thread_ts, text=reply_text
        )
        print(f"✅ Posted Date Ended: {end_time}")
    except slack.errors.SlackApiError as e:
        print(f"Error posting Date Ended: {e}")


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

        # If message has ✅ (resolved)
        if has_checkmark:
            # Delete the reminder if we sent one
            if message_ts in sent_reminders:
                reminder_ts = sent_reminders[message_ts]
                if delete_reminder(alerts_channel_id, reminder_ts):
                    del sent_reminders[message_ts]
                    reminders_deleted += 1

            # Post Date Ended if we haven't already
            if message_ts not in resolved_messages:
                post_date_ended(monitored_channel_id, message_ts)
                resolved_messages.add(message_ts)

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

            # 2. Reply to the original thread with check time and tag sender
            reply_to_original_thread(monitored_channel_id, message_ts, message)

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
