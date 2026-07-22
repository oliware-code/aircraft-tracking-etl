from notify_bot import notify_bot, OVG


def send_notification(message, parse_mode=None, disable_link_preview=False):
    """Send a Telegram notification to the default chat."""
    return notify_bot(OVG, message, parse_mode=parse_mode, disable_link_preview=disable_link_preview)


if __name__ == "__main__":
    send_notification("✈️ Aircraft Tracking: notification system online.")
