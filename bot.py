from datetime import datetime
from ipaddress import ip_address
from time import sleep
from requests import ConnectionError
import json, logging, os, requests, subprocess, sys

from telegram.error import NetworkError
from telegram.ext import CommandHandler, Filters, MessageHandler, Updater
import telegram


default_settings = {
    "whitelist": [],
    "ip_changes": [],
    "restarts": [],
    "max_text_warning": 3,
    "connection": {
        "max_retries": 300,
        "retry_timeout": 60,
        "ip_check_interval": 3600,
        "uptime_threshold": 600,
    },
    "logging": {
        "log_file": "/var/log/rpi-telegram-bot.log",
        "max_backups": 10,
        "file_max_bytes": 10485760,
    },
    "images_dlna_basepath": "/home/pi/minidlna/",
}

settings = {}
with open("settings.json", encoding="utf-8") as f:
    settings = json.load(f)

if not settings:
    print("[ERROR] No settings file found, exiting")
    sys.exit(0)


def settings_get(key):
    return settings.get(key, default_settings[key])


logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.handlers.RotatingFileHandler(
            settings_get("logging")["log_file"],
            maxBytes=settings_get("logging")["file_max_bytes"],
            backupCount=settings_get("logging")["max_backups"],
        )
    ],
)

if "token" not in settings or not settings["token"]:
    logging.fatal(
        "The Telegram API token must be provided for the bot to work. Exiting"
    )
    sys.exit(0)

if not settings_get("whitelist"):
    logging.warning(
        "There are no user IDs on the whitelist. The bot will ignore all messages"
    )

handlers = []


def get_filtered_command_handler(command, handler):
    return CommandHandler(
        command, handler, Filters.user(user_id=settings_get("whitelist"))
    )


def ip_is_valid(ip):
    try:
        ip_address(ip)
        return True
    except ValueError:
        return False


def get_ip():
    sources = ["https://api.ipify.org", "https://ident.me", "https://ipinfo.io/ip"]
    for source in sources:
        response = requests.get(source)
        ip = response.text.strip()

        if response.ok and ip_is_valid(ip):
            return ip

    return None


def get_uptime(*args):
    return (
        subprocess.run(
            "uptime" if not args else ["uptime", *args], stdout=subprocess.PIPE
        )
        .stdout.decode("utf-8")
        .strip()
    )


def alert_restart(context):
    uptime_since = datetime.strptime(get_uptime("-s"), "%Y-%m-%d %H:%M:%S")
    diff = (datetime.now() - uptime_since).seconds

    if diff <= settings_get("connection")["uptime_threshold"]:
        for user_id in settings_get("restarts"):
            context.bot.send_message(
                chat_id=user_id,
                text=(
                    "The bot has restarted and the uptime is {} seconds.\n"
                    + "A power outage may have occurred"
                ).format(diff),
            )


def start_handler(update, context):
    context.bot.send_message(chat_id=update.effective_chat.id, text="Welcome")


def check_ip(context):
    new_ip = get_ip()
    if new_ip and new_ip != context.job.context:
        context.job.context = new_ip
        for user_id in settings_get("ip_changes"):
            context.bot.send_message(
                chat_id=user_id, text="External IP has changed.\nNew IP: " + new_ip
            )


def ip_handler(update, context):
    context.bot.send_message(
        chat_id=update.effective_chat.id, text=get_ip() or "Could not get IP",
    )


def temperature_handler(update, context):
    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=subprocess.run(["vcgencmd", "measure_temp"], stdout=subprocess.PIPE)
        .stdout.decode("utf-8")
        .strip()
        .replace("temp=", "")
        .replace("'", "°"),
    )


def uptime_handler(update, context):
    context.bot.send_message(
        chat_id=update.effective_chat.id, text=get_uptime(),
    )


def get_image_file_name(basepath, basename, file_name=None):
    extension = file_name.split(".")[-1] if file_name else "jpg"
    date = datetime.today().strftime("%Y-%m-%d")

    directory = "{}/{}".format(basepath.rstrip("/"), date)
    if not os.path.exists(directory):
        os.makedirs(directory)

    i = 0
    name = "{}/{}.{}".format(directory, basename.rstrip("/"), extension)
    while os.path.isfile(name):
        i += 1
        name = "{}/{}_{}.{}".format(directory, basename.rstrip("/"), i, extension)
    return name


def image_handler(update, context):
    photo_file_id = update.message.photo[-1].file_id
    file_name = get_image_file_name(settings_get("images_dlna_basepath"), "image")
    context.bot.get_file(photo_file_id).download(file_name)

    logging.info('Saved image at "' + file_name + '"')
    context.bot.send_message(chat_id=update.effective_chat.id, text="Received photo")


def image_file_handler(update, context):
    image = update.message.document
    file_name = get_image_file_name(
        settings_get("images_dlna_basepath"), "image", image.file_name
    )
    context.bot.get_file(image.file_id).download(file_name)

    logging.info('Saved image at "' + file_name + '"')
    context.bot.send_message(
        chat_id=update.effective_chat.id, text="Received image file"
    )


def text_handler(update, context):
    """Handles regular text messages (not commands)"""

    if "text_messages" in context.chat_data:
        context.chat_data["text_messages"] += 1

        if context.chat_data["text_messages"] >= settings_get("max_text_warning"):
            context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="This bot will not reply to regular text messages.\n"
                + "Use /help to see available commands and actions",
            )
            context.chat_data["text_messages"] = 0
    else:
        context.chat_data["text_messages"] = 0


def help_handler(update, context):
    """Handles /help commands, explaining available commands and actions"""

    with open("command_descriptions.txt") as f:
        context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="*Commands*:\n"
            + "\n".join("/" + line for line in f.readlines())
            + "\n\n*Other Actions*\n"
            + "Sending an image will save it if the"
            + " bot is configured to do so (it will reply if the image is successfully saved)\n",
            parse_mode=telegram.ParseMode.MARKDOWN,
        )


def unknown_handler(update, context):
    """Handles unknown commands"""

    context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="Unknown command.\nUse /help to see available commands and actions",
    )


handlers.append(get_filtered_command_handler("start", start_handler))

handlers.append(get_filtered_command_handler("ip", ip_handler))
handlers.append(get_filtered_command_handler("temperature", temperature_handler))
handlers.append(get_filtered_command_handler("uptime", uptime_handler))
handlers.append(get_filtered_command_handler("help", help_handler))

handlers.append(MessageHandler(Filters.text, text_handler))
handlers.append(MessageHandler(Filters.command, unknown_handler))

if settings_get("images_dlna_basepath"):
    handlers.append(MessageHandler(Filters.photo, image_handler))
    handlers.append(MessageHandler(Filters.document.image, image_file_handler))


def main():
    token = settings["token"]

    updater = Updater(token=token, use_context=True)
    dispatcher = updater.dispatcher
    job_queue = updater.job_queue

    if settings_get("restarts"):
        job_queue.run_once(alert_restart, 0)

    if settings_get("ip_changes"):
        job_queue.run_repeating(
            check_ip,
            settings_get("connection")["ip_check_interval"],
            first=0,
            context=get_ip(),
        )

    for handler in handlers:
        dispatcher.add_handler(handler)

    logging.info("Starting bot")
    updater.start_polling()

    updater.idle()


if __name__ == "__main__":
    for _ in range(settings_get("connection")["max_retries"]):
        try:
            main()
            break
        except (ConnectionError, NetworkError) as err:
            retry_timeout = settings_get("connection")["retry_timeout"]
            logging.exception(
                "Network/connection error. Retrying in {} seconds".format(retry_timeout)
            )
            sleep(retry_timeout)
