from notify_bot import notify_bot, OVG


def send_notification(message, parse_mode=None):
    """Send a Telegram notification to the default chat."""
    return notify_bot(OVG, message, parse_mode=parse_mode)


if __name__ == "__main__":
    send_notification("✈️ Aircraft Tracking: notification system online.")
