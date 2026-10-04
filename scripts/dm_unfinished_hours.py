#!/usr/bin/env python3
"""DM everyone in a CSV exported from the Atlantis users page
("Export users short on hours") asking if they need help finishing their hours.

Sends as whoever owns SLACK_TOKEN (a user token, so the DMs come from you).

Env:
  SLACK_TOKEN               user token (xoxp-...) with im:write, chat:write
  TEST_SLACK_USER_REDIRECT  send every DM to this Slack ID instead
  TEST_ONLY_SLACK_ID        only DM this one row from the CSV, and don't log it
"""

import csv
import os
import sys
import termios
import tty

import requests
from dotenv import load_dotenv

load_dotenv()

SENT_LOG_FILE = "sent_unfinished_hours.txt"
SLACK_TOKEN = os.environ.get("SLACK_TOKEN", "")
TEST_SLACK_USER_REDIRECT = os.environ.get("TEST_SLACK_USER_REDIRECT", "")
TEST_ONLY_SLACK_ID = os.environ.get("TEST_ONLY_SLACK_ID", "").strip()


def getch(valid: str) -> str:
    """Read a single keypress and return it if it's in the valid set."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch == "\x03":
                raise KeyboardInterrupt
            ch = ch.upper()
            if ch in valid:
                sys.stdout.write(ch + "\n")
                sys.stdout.flush()
                return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def load_sent_users() -> set[str]:
    if not os.path.exists(SENT_LOG_FILE):
        return set()
    with open(SENT_LOG_FILE, "r") as f:
        return set(line.strip() for line in f if line.strip())


def log_sent_user(slack_id: str) -> None:
    with open(SENT_LOG_FILE, "a") as f:
        f.write(slack_id + "\n")


def slack_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {SLACK_TOKEN}"}


def open_dm_channel(slack_id: str) -> str | None:
    """Open (or reuse) a DM channel and return its channel id."""
    target = TEST_SLACK_USER_REDIRECT or slack_id
    try:
        resp = requests.post(
            "https://slack.com/api/conversations.open",
            headers=slack_headers(),
            json={"users": target},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"  ⚠️  Failed to open conversation: {data}")
            return None
        return data["channel"]["id"]
    except Exception as e:
        print(f"  ⚠️  Slack request failed while opening DM: {e}")
        return None


def fetch_authed_user_id() -> str | None:
    """Return the user id of the token owner, to confirm the token works."""
    try:
        resp = requests.post(
            "https://slack.com/api/auth.test",
            headers=slack_headers(),
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data.get("user_id")
        print(f"  ⚠️  Slack auth.test failed: {data}")
    except Exception as e:
        print(f"  ⚠️  Slack request failed during auth.test: {e}")
    return None


def send_slack_dm(channel_id: str, message: str) -> bool:
    """Send a message to an already-opened DM channel."""
    try:
        resp = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers=slack_headers(),
            json={"channel": channel_id, "text": message},
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            print(f"\n❌ Failed to send message: {data}")
            return False
        return True
    except Exception as e:
        print(f"\n❌ Slack request failed while sending message: {e}")
        return False


def build_message(slack_id: str) -> str:
    return (
        f"hi <@{slack_id}>. it's swarit! i noticed you haven't finished your 10 hours "
        "for weeks 1+2 for atlantis yet. is there any way i can help you finish the "
        "hours? keep in mind you'll need to track 10 hours by sunday 11:59PM EDT to "
        "get your printer :)"
    )


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python dm_unfinished_hours.py <csv_file>")
        sys.exit(1)

    csv_file = sys.argv[1]

    if not SLACK_TOKEN:
        print("❌ SLACK_TOKEN environment variable not set.")
        sys.exit(1)

    if TEST_SLACK_USER_REDIRECT:
        print(f"⚠️  TEST MODE: all DMs will be sent to {TEST_SLACK_USER_REDIRECT}\n")
    if TEST_ONLY_SLACK_ID:
        print(f"⚠️  TEST MODE: only DMing {TEST_ONLY_SLACK_ID}.\n")

    print("🔍 Checking token identity...")
    authed_user_id = fetch_authed_user_id()
    if not authed_user_id:
        print("❌ Could not identify Slack token owner (auth.test failed).")
        sys.exit(1)
    print(f"✅ Authenticated as {authed_user_id}\n")

    with open(csv_file, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    print(f"📋 Loaded {len(rows)} records from {csv_file}")

    sent_users = load_sent_users()
    target_rows = []
    for row in rows:
        slack_id = row.get("slack_id", "").strip()
        if not slack_id:
            continue
        if TEST_ONLY_SLACK_ID and slack_id != TEST_ONLY_SLACK_ID:
            continue
        if not TEST_ONLY_SLACK_ID and slack_id in sent_users:
            continue
        target_rows.append(row)

    if TEST_ONLY_SLACK_ID and not target_rows:
        print(f"❌ No rows found for TEST_ONLY_SLACK_ID={TEST_ONLY_SLACK_ID}.")
        return

    print(f"📨 We will DM {len(target_rows)} users. Continue? (Y/N): ", end="", flush=True)
    proceed = getch("YN")
    if proceed == "N":
        print("⏹️  Cancelled. No messages sent.")
        return

    total = len(target_rows)
    for i, row in enumerate(target_rows, start=1):
        slack_id = row.get("slack_id", "").strip()
        username = row.get("username", "").strip()

        clear_screen()
        print(f"{'=' * 60}")
        print(f"  [{i}/{total}] {username}")
        print(f"  Slack ID: {slack_id}")
        print(f"  Week:     {row.get('week', '').strip()}")
        print(f"  Tracked:  {row.get('progress', '').strip()}")
        print(f"  Left:     {row.get('left', '').strip()}")
        print(f"{'=' * 60}")

        print("\n📨 Opening DM...")
        channel_id = open_dm_channel(slack_id)
        if not channel_id:
            print("❌ Could not open DM. User NOT logged as sent.")
            continue

        message = build_message(slack_id)

        print(f"\n{'─' * 60}")
        print(message)
        print(f"{'─' * 60}")

        print("\n📤 Sending DM...")
        if send_slack_dm(channel_id, message):
            print("✅ Message sent!")
            if TEST_ONLY_SLACK_ID:
                print("🧪 Test mode: not adding user to sent log.")
            else:
                log_sent_user(slack_id)
                sent_users.add(slack_id)
        else:
            print("❌ Failed to send. User NOT logged as sent.")

    clear_screen()
    print("🎉 All done! Processed all users.")


if __name__ == "__main__":
    main()
