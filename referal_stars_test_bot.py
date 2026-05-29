from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from bson import ObjectId
from dotenv import load_dotenv
from flask import Flask, abort, request
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from telebot import TeleBot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, Update


load_dotenv()


def csv_ints(value: str) -> set[int]:
    result: set[int] = set()
    for item in value.split(","):
        item = item.strip()
        if item:
            result.add(int(item))
    return result


@dataclass(frozen=True)
class Settings:
    bot_token: str
    mongodb_uri: str
    mongodb_db: str
    webhook_secret: str
    public_base_url: str
    admin_ids: set[int]
    bot_username: str
    default_emoji_id: str
    star_emoji_id: str


settings = Settings(
    bot_token=os.getenv("BOT_TOKEN", "8936595051:AAHhoyn2O3bd9IRsqogmA61Olky9EGCKE-M"),
    mongodb_uri=os.getenv("MONGODB_URI", "mongodb+srv://bmurodova550_db_user:javohir1234@kino1b.320mywf.mongodb.net/?appName=kino1b"),
    mongodb_db=os.getenv("MONGODB_DB", "referral_stars_bot"),
    webhook_secret=os.getenv("WEBHOOK_SECRET", "change-this-secret"),
    public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
    admin_ids=csv_ints(os.getenv("ADMIN_IDS", "6968399046")),
    bot_username=os.getenv("BOT_USERNAME", "java_free_things_bot"),
    default_emoji_id=os.getenv("DEFAULT_EMOJI_ID", "5458794766248459827"),
    star_emoji_id=os.getenv("STAR_EMOJI_ID", "5283231578523204600"),
)

if not settings.bot_token:
    raise RuntimeError("BOT_TOKEN .env ichida yozilishi kerak.")


bot = TeleBot(settings.bot_token, parse_mode="HTML", threaded=False)
app = Flask(__name__)
client = MongoClient(settings.mongodb_uri)
db = client[settings.mongodb_db]

users = db.users
channels = db.mandatory_channels
withdrawals = db.withdrawals

admin_state: dict[int, dict[str, Any]] = {}
webhook_ready = False

REFERRAL_REWARD = 1
MIN_WITHDRAW = 15


class StyledInlineKeyboardButton(InlineKeyboardButton):
    def __init__(
        self,
        text: str,
        *,
        style: str | None = None,
        icon_custom_emoji_id: str | None = None,
        **kwargs,
    ):
        super().__init__(text=text, **kwargs)
        self.style = style
        self.icon_custom_emoji_id = icon_custom_emoji_id

    def to_dict(self):
        data = super().to_dict()
        if self.style:
            data["style"] = self.style
        if self.icon_custom_emoji_id:
            data["icon_custom_emoji_id"] = self.icon_custom_emoji_id
        return data


def ibutton(text: str, *, style: str = "primary", icon_custom_emoji_id: str | None = None, **kwargs):
    return StyledInlineKeyboardButton(
        text=text,
        style=style,
        icon_custom_emoji_id=icon_custom_emoji_id or settings.default_emoji_id or None,
        **kwargs,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def tg_emoji(fallback: str, emoji_id: str) -> str:
    if not emoji_id:
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def star_icon() -> str:
    return tg_emoji("⭐", settings.star_emoji_id or settings.default_emoji_id)


def is_admin(user_id: int) -> bool:
    return bool(settings.admin_ids) and user_id in settings.admin_ids


def ensure_indexes() -> None:
    users.create_index("telegram_id", unique=True)
    users.create_index([("balance", DESCENDING)])
    channels.create_index("chat_id", unique=True)
    withdrawals.create_index([("status", ASCENDING), ("created_at", DESCENDING)])


def bot_username() -> str:
    if settings.bot_username:
        return settings.bot_username.lstrip("@")
    return bot.get_me().username


def referral_link(user_id: int) -> str:
    return f"https://t.me/{bot_username()}?start=ref_{user_id}"


def save_user(message: Message, referred_by: int | None = None) -> dict:
    payload = {
        "telegram_id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "last_name": message.from_user.last_name,
        "updated_at": utcnow(),
    }
    set_on_insert: dict[str, Any] = {
        "balance": 0,
        "earned_total": 0,
        "withdrawn_total": 0,
        "referrals_count": 0,
        "referral_rewarded": False,
        "created_at": utcnow(),
    }
    if referred_by and referred_by != message.from_user.id:
        set_on_insert["referred_by"] = referred_by

    return users.find_one_and_update(
        {"telegram_id": message.from_user.id},
        {"$set": payload, "$setOnInsert": set_on_insert},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def user_name(user: dict | Message) -> str:
    if isinstance(user, dict):
        return user.get("first_name") or user.get("username") or str(user.get("telegram_id"))
    return user.from_user.first_name or user.from_user.username or str(user.from_user.id)


def has_active_channels() -> bool:
    return channels.count_documents({"active": True}) > 0


def subscribe_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for channel in channels.find({"active": True}).sort("created_at", ASCENDING):
        title = channel.get("title") or str(channel.get("chat_id"))
        url = channel.get("invite_link") or channel.get("url")
        if url:
            markup.add(ibutton(f"📌 {title}", url=url, style="success"))
    markup.add(ibutton("✅ Obunani tekshirish", callback_data="user:check_sub", style="primary"))
    return markup


def user_panel_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        ibutton("⭐ Balans", callback_data="user:balance", style="primary"),
        ibutton("🔗 Referal link", callback_data="user:ref", style="success"),
        ibutton("💸 Stars yechish", callback_data="user:withdraw", style="success"),
        ibutton("📊 Mening statistikam", callback_data="user:stats", style="primary"),
        ibutton("📌 Obunani tekshirish", callback_data="user:check_sub", style="primary"),
    )
    return markup


def admin_panel_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        ibutton("📊 Statistika", callback_data="admin:stats", style="primary"),
        ibutton("💸 Yechish so'rovlari", callback_data="admin:withdrawals", style="success"),
        ibutton("📈 Yechilgan statistika", callback_data="admin:withdraw_stats", style="primary"),
        ibutton("📌 Obuna qo'shish", callback_data="admin:add_channel", style="success"),
        ibutton("🧾 Obunalar", callback_data="admin:list_channels", style="primary"),
        ibutton("🗑 Obuna o'chirish", callback_data="admin:del_channel", style="danger"),
        ibutton("👥 Userlarga xabar", callback_data="admin:broadcast", style="success"),
        ibutton("❌ Yopish", callback_data="admin:close", style="danger"),
    )
    return markup


def withdraw_action_keyboard(withdrawal_id) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        ibutton("✅ To'landi", callback_data=f"withdraw:approve:{withdrawal_id}", style="success"),
        ibutton("❌ Rad etish", callback_data=f"withdraw:reject:{withdrawal_id}", style="danger"),
    )
    return markup


def normalize_channel(raw: str) -> tuple[str, str]:
    text = raw.strip()
    if text.startswith("https://t.me/"):
        username = text.removeprefix("https://t.me/").split("/", 1)[0]
        return f"@{username}", f"https://t.me/{username}"
    if text.startswith("t.me/"):
        username = text.removeprefix("t.me/").split("/", 1)[0]
        return f"@{username}", f"https://t.me/{username}"
    if text.startswith("@"):
        return text, f"https://t.me/{text[1:]}"
    return text, text


def bot_is_admin(chat_id: str | int) -> bool:
    try:
        member = bot.get_chat_member(chat_id, bot.get_me().id)
        return member.status in {"administrator", "creator"}
    except ApiTelegramException:
        return False


def user_is_subscribed(user_id: int) -> bool:
    active_channels = list(channels.find({"active": True}))
    if not active_channels:
        return True
    for channel in active_channels:
        try:
            member = bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in {"left", "kicked"}:
                return False
        except ApiTelegramException:
            return False
    return True


def maybe_reward_referrer(user_id: int) -> bool:
    user = users.find_one({"telegram_id": user_id}) or {}
    referrer_id = user.get("referred_by")
    if not referrer_id or user.get("referral_rewarded"):
        return False
    if not user_is_subscribed(user_id):
        return False

    result = users.update_one(
        {"telegram_id": user_id, "referral_rewarded": {"$ne": True}},
        {"$set": {"referral_rewarded": True, "rewarded_at": utcnow()}},
    )
    if result.modified_count != 1:
        return False

    users.update_one(
        {"telegram_id": referrer_id},
        {
            "$inc": {
                "balance": REFERRAL_REWARD,
                "earned_total": REFERRAL_REWARD,
                "referrals_count": 1,
            },
            "$set": {"updated_at": utcnow()},
        },
    )
    try:
        bot.send_message(
            referrer_id,
            f"{star_icon()} <b>Yangi referal!</b>\n\nBalansingizga <b>{REFERRAL_REWARD} Stars</b> qo'shildi.",
        )
    except ApiTelegramException:
        pass
    return True


def send_user_panel(chat_id: int, user: dict) -> None:
    name = user_name(user)
    text = (
        f"👋 <b>Hush kelibsiz, {name}!</b>\n\n"
        f"{star_icon()} Har bir referal narxi: <b>{REFERRAL_REWARD} Stars</b>\n"
        f"💸 Minimum yechish: <b>{MIN_WITHDRAW} Stars</b>\n\n"
        "Quyidagi paneldan foydalaning."
    )
    bot.send_message(chat_id, text, reply_markup=user_panel_keyboard(), disable_web_page_preview=True)


def send_subscribe(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "🔐 <b>Majburiy obuna</b>\n\nBotdan foydalanish uchun avval kanallarga obuna bo'ling.",
        reply_markup=subscribe_keyboard(),
        disable_web_page_preview=True,
    )


def configure_webhook() -> dict[str, Any]:
    if not settings.public_base_url:
        return {"ok": False, "error": "PUBLIC_BASE_URL env yozilmagan"}
    url = f"{settings.public_base_url.rstrip('/')}/webhook/{settings.webhook_secret}"
    bot.remove_webhook()
    bot.set_webhook(url=url, allowed_updates=["message", "callback_query"])
    return {"ok": True, "webhook": url}


@app.before_request
def prepare_once():
    global webhook_ready
    if not app.config.get("INDEXES_READY"):
        ensure_indexes()
        app.config["INDEXES_READY"] = True
    if not webhook_ready and settings.public_base_url:
        configure_webhook()
        webhook_ready = True


@bot.message_handler(commands=["start"])
def start(message: Message) -> None:
    if message.chat.type != "private":
        return

    referred_by = None
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("ref_"):
        try:
            referred_by = int(parts[1].removeprefix("ref_"))
        except ValueError:
            referred_by = None

    user = save_user(message, referred_by=referred_by)
    if not user_is_subscribed(message.from_user.id):
        send_subscribe(message.chat.id)
        return

    maybe_reward_referrer(message.from_user.id)
    send_user_panel(message.chat.id, user)


@bot.message_handler(commands=["admin"])
def admin(message: Message) -> None:
    if message.chat.type != "private" or not is_admin(message.from_user.id):
        return
    save_user(message)
    admin_state.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_panel_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("user:"))
def user_callbacks(call) -> None:
    user = users.find_one({"telegram_id": call.from_user.id})
    if not user:
        bot.answer_callback_query(call.id, "Avval /start bosing.", show_alert=True)
        return

    action = call.data.split(":", 1)[1]

    if action == "check_sub":
        if user_is_subscribed(call.from_user.id):
            maybe_reward_referrer(call.from_user.id)
            bot.answer_callback_query(call.id, "✅ Obuna tasdiqlandi.")
            bot.edit_message_text(
                f"✅ Obuna tasdiqlandi.\n\n👋 <b>Hush kelibsiz, {user_name(user)}!</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=user_panel_keyboard(),
            )
        else:
            bot.answer_callback_query(call.id, "Avval kanallarga obuna bo'ling.", show_alert=True)
        return

    if not user_is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Avval majburiy obunani bajaring.", show_alert=True)
        return

    if action == "balance":
        text = (
            f"{star_icon()} <b>Balansingiz</b>\n\n"
            f"Joriy balans: <b>{user.get('balance', 0)} Stars</b>\n"
            f"Jami ishlangan: <b>{user.get('earned_total', 0)} Stars</b>\n"
            f"Jami yechilgan: <b>{user.get('withdrawn_total', 0)} Stars</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=user_panel_keyboard())
        return

    if action == "ref":
        text = (
            "🔗 <b>Sizning referal linkingiz</b>\n\n"
            f"<code>{referral_link(call.from_user.id)}</code>\n\n"
            f"Har bir do'stingiz uchun <b>{REFERRAL_REWARD} Stars</b> olasiz."
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=user_panel_keyboard())
        return

    if action == "stats":
        text = (
            "📊 <b>Mening statistikam</b>\n\n"
            f"👥 Referallar: <b>{user.get('referrals_count', 0)}</b>\n"
            f"{star_icon()} Balans: <b>{user.get('balance', 0)} Stars</b>\n"
            f"💸 Yechilgan: <b>{user.get('withdrawn_total', 0)} Stars</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=user_panel_keyboard())
        return

    if action == "withdraw":
        balance = int(user.get("balance", 0))
        if balance < MIN_WITHDRAW:
            bot.answer_callback_query(call.id, f"Minimum yechish {MIN_WITHDRAW} Stars.", show_alert=True)
            return
        admin_state[call.from_user.id] = {"step": "withdraw_amount"}
        bot.send_message(
            call.message.chat.id,
            f"💸 Yechmoqchi bo'lgan Stars miqdorini yuboring.\n\nBalans: <b>{balance} Stars</b>\nMinimum: <b>{MIN_WITHDRAW} Stars</b>",
        )
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin:"))
def admin_callbacks(call) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.", show_alert=True)
        return

    action = call.data.split(":", 1)[1]
    admin_state.pop(call.from_user.id, None)

    if action == "close":
        bot.delete_message(call.message.chat.id, call.message.message_id)
        return

    if action == "stats":
        text = (
            "📊 <b>Bot statistikasi</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{users.count_documents({})}</b>\n"
            f"👥 Referallar: <b>{sum(u.get('referrals_count', 0) for u in users.find({}, {'referrals_count': 1}))}</b>\n"
            f"{star_icon()} Jami ishlangan: <b>{sum(u.get('earned_total', 0) for u in users.find({}, {'earned_total': 1}))} Stars</b>\n"
            f"📌 Majburiy obunalar: <b>{channels.count_documents({'active': True})}</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=admin_panel_keyboard())
        return

    if action == "withdraw_stats":
        paid = list(withdrawals.find({"status": "approved"}))
        pending = withdrawals.count_documents({"status": "pending"})
        total_paid = sum(int(w.get("amount", 0)) for w in paid)
        text = (
            "📈 <b>Stars yechish statistikasi</b>\n\n"
            f"✅ To'langan so'rovlar: <b>{len(paid)}</b>\n"
            f"⏳ Kutilayotgan so'rovlar: <b>{pending}</b>\n"
            f"💸 Jami yechilgan: <b>{total_paid} Stars</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=admin_panel_keyboard())
        return

    if action == "withdrawals":
        pending = list(withdrawals.find({"status": "pending"}).sort("created_at", ASCENDING).limit(10))
        if not pending:
            bot.answer_callback_query(call.id, "Kutilayotgan so'rov yo'q.")
            bot.send_message(call.message.chat.id, "💸 Kutilayotgan yechish so'rovlari yo'q.", reply_markup=admin_panel_keyboard())
            return
        for item in pending:
            user = users.find_one({"telegram_id": item["user_id"]}) or {}
            text = (
                "💸 <b>Yechish so'rovi</b>\n\n"
                f"👤 User: <code>{item['user_id']}</code> @{user.get('username', '-')}\n"
                f"{star_icon()} Miqdor: <b>{item['amount']} Stars</b>\n"
                f"📝 Ma'lumot: <code>{item.get('payout_info', '-')}</code>"
            )
            bot.send_message(call.message.chat.id, text, reply_markup=withdraw_action_keyboard(item["_id"]))
        bot.answer_callback_query(call.id)
        return

    if action == "add_channel":
        admin_state[call.from_user.id] = {"step": "add_channel"}
        bot.send_message(call.message.chat.id, "📌 Majburiy kanal username/linkini yuboring. Masalan: @kanal")
        bot.answer_callback_query(call.id)
        return

    if action == "list_channels":
        lines = ["🧾 <b>Majburiy obunalar</b>"]
        for channel in channels.find({"active": True}).sort("created_at", ASCENDING):
            status = "✅ admin" if channel.get("bot_admin") else "⚠️ admin emas"
            lines.append(f"\n<code>{channel['chat_id']}</code> - {channel.get('title', 'Kanal')} - {status}")
        if len(lines) == 1:
            lines.append("\nHozircha kanal yo'q.")
        bot.edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=admin_panel_keyboard())
        return

    if action == "del_channel":
        admin_state[call.from_user.id] = {"step": "del_channel"}
        bot.send_message(call.message.chat.id, "🗑 O'chiriladigan kanal username yoki ID sini yuboring.")
        bot.answer_callback_query(call.id)
        return

    if action == "broadcast":
        admin_state[call.from_user.id] = {"step": "broadcast"}
        bot.send_message(call.message.chat.id, "👥 Hamma foydalanuvchilarga yuboriladigan xabarni jo'nating.")
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("withdraw:"))
def withdraw_callbacks(call) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.", show_alert=True)
        return

    _, action, withdrawal_id = call.data.split(":", 2)
    try:
        withdrawal_object_id = ObjectId(withdrawal_id)
    except Exception:
        bot.answer_callback_query(call.id, "So'rov ID xato.", show_alert=True)
        return

    item = withdrawals.find_one({"_id": withdrawal_object_id, "status": "pending"})
    if not item:
        bot.answer_callback_query(call.id, "So'rov topilmadi yoki yopilgan.", show_alert=True)
        return

    if action == "approve":
        withdrawals.update_one(
            {"_id": withdrawal_object_id},
            {"$set": {"status": "approved", "approved_at": utcnow(), "admin_id": call.from_user.id}},
        )
        users.update_one(
            {"telegram_id": item["user_id"]},
            {"$inc": {"withdrawn_total": int(item["amount"])}, "$set": {"updated_at": utcnow()}},
        )
        bot.answer_callback_query(call.id, "To'landi deb belgilandi.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        try:
            bot.send_message(item["user_id"], f"✅ <b>{item['amount']} Stars</b> yechish so'rovingiz to'landi.")
        except ApiTelegramException:
            pass
        return

    if action == "reject":
        withdrawals.update_one(
            {"_id": withdrawal_object_id},
            {"$set": {"status": "rejected", "rejected_at": utcnow(), "admin_id": call.from_user.id}},
        )
        users.update_one(
            {"telegram_id": item["user_id"]},
            {"$inc": {"balance": int(item["amount"])}, "$set": {"updated_at": utcnow()}},
        )
        bot.answer_callback_query(call.id, "Rad etildi, Stars balansga qaytarildi.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        try:
            bot.send_message(item["user_id"], f"❌ Yechish so'rovingiz rad etildi. <b>{item['amount']} Stars</b> balansga qaytarildi.")
        except ApiTelegramException:
            pass


@bot.message_handler(content_types=["text", "photo", "video", "document", "animation", "sticker"])
def text_router(message: Message) -> None:
    if message.chat.type != "private":
        return
    save_user(message)

    if is_admin(message.from_user.id) and handle_admin_state(message):
        return
    if handle_user_state(message):
        return

    if not user_is_subscribed(message.from_user.id):
        send_subscribe(message.chat.id)
        return
    user = users.find_one({"telegram_id": message.from_user.id}) or {}
    send_user_panel(message.chat.id, user)


def handle_user_state(message: Message) -> bool:
    state = admin_state.get(message.from_user.id)
    if not state:
        return False

    user = users.find_one({"telegram_id": message.from_user.id}) or {}
    step = state.get("step")

    if step == "withdraw_amount":
        try:
            amount = int((message.text or "").strip())
        except ValueError:
            bot.send_message(message.chat.id, "Miqdor raqam bo'lishi kerak.")
            return True
        balance = int(user.get("balance", 0))
        if amount < MIN_WITHDRAW:
            bot.send_message(message.chat.id, f"Minimum yechish {MIN_WITHDRAW} Stars.")
            return True
        if amount > balance:
            bot.send_message(message.chat.id, f"Balansingizda faqat {balance} Stars bor.")
            return True
        admin_state[message.from_user.id] = {"step": "withdraw_info", "amount": amount}
        bot.send_message(message.chat.id, "Stars qabul qilish uchun ma'lumot yuboring. Masalan: username yoki izoh.")
        return True

    if step == "withdraw_info":
        amount = int(state["amount"])
        result = users.update_one(
            {"telegram_id": message.from_user.id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}, "$set": {"updated_at": utcnow()}},
        )
        if result.modified_count != 1:
            admin_state.pop(message.from_user.id, None)
            bot.send_message(message.chat.id, "Balans yetarli emas.", reply_markup=user_panel_keyboard())
            return True

        item = {
            "user_id": message.from_user.id,
            "amount": amount,
            "payout_info": message.text or "-",
            "status": "pending",
            "created_at": utcnow(),
        }
        inserted = withdrawals.insert_one(item)
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, "✅ Yechish so'rovingiz adminga yuborildi.", reply_markup=user_panel_keyboard())
        for admin_id in settings.admin_ids:
            try:
                bot.send_message(
                    admin_id,
                    (
                        "💸 <b>Yangi yechish so'rovi</b>\n\n"
                        f"👤 User: <code>{message.from_user.id}</code> @{message.from_user.username or '-'}\n"
                        f"{star_icon()} Miqdor: <b>{amount} Stars</b>\n"
                        f"📝 Ma'lumot: <code>{message.text or '-'}</code>"
                    ),
                    reply_markup=withdraw_action_keyboard(inserted.inserted_id),
                )
            except ApiTelegramException:
                pass
        return True

    return False


def handle_admin_state(message: Message) -> bool:
    state = admin_state.get(message.from_user.id)
    if not state:
        return False
    step = state.get("step")

    if step == "add_channel":
        chat_id, url = normalize_channel(message.text or "")
        title = chat_id
        bot_admin = bot_is_admin(chat_id)
        try:
            chat = bot.get_chat(chat_id)
            title = chat.title or chat.username or chat_id
            if chat.username:
                url = f"https://t.me/{chat.username}"
        except ApiTelegramException:
            pass
        channels.update_one(
            {"chat_id": chat_id},
            {
                "$set": {
                    "chat_id": chat_id,
                    "title": title,
                    "url": url,
                    "invite_link": url,
                    "bot_admin": bot_admin,
                    "active": True,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )
        admin_state.pop(message.from_user.id, None)
        status = "✅ Bot kanalda admin." if bot_admin else "⚠️ Botni kanalda admin qiling."
        bot.send_message(message.chat.id, f"📌 Kanal qo'shildi: <code>{chat_id}</code>\n{status}", reply_markup=admin_panel_keyboard())
        return True

    if step == "del_channel":
        chat_id, _ = normalize_channel(message.text or "")
        result = channels.update_one({"chat_id": chat_id}, {"$set": {"active": False, "updated_at": utcnow()}})
        admin_state.pop(message.from_user.id, None)
        text = "🗑 Kanal o'chirildi." if result.matched_count else "Kanal topilmadi."
        bot.send_message(message.chat.id, text, reply_markup=admin_panel_keyboard())
        return True

    if step == "broadcast":
        ok = 0
        fail = 0
        for user in users.find({}, {"telegram_id": 1}):
            try:
                bot.copy_message(user["telegram_id"], message.chat.id, message.message_id)
                ok += 1
            except ApiTelegramException:
                fail += 1
        admin_state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"👥 Xabar yuborildi.\n✅ {ok} ta\n❌ {fail} ta", reply_markup=admin_panel_keyboard())
        return True

    return False

@app.get("/")
def home():
    return {"ok": True, "service": "Referral Stars Bot"}


@app.post(f"/webhook/{settings.webhook_secret}")
def telegram_webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)

    update = Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return {"ok": True}


@app.get("/setup-webhook")
@app.get("/setup-webhook/")
def setup_webhook():
    if not settings.public_base_url:
        return {"ok": False, "error": "PUBLIC_BASE_URL yozilmagan"}, 400

    url = f"{settings.public_base_url.rstrip('/')}/webhook/{settings.webhook_secret}"

    bot.remove_webhook()
    bot.set_webhook(
        url=url,
        allowed_updates=["message", "callback_query"],
    )

    return {"ok": True, "webhook": url}


@app.get("/webhook-info")
@app.get("/webhook-info/")
def webhook_info():
    return bot.get_webhook_info().to_dict()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))


        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))

