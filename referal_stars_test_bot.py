from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import certifi
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
    mongodb_uri=os.getenv("MONGODB_URI", "mongodb+srv://bmurodova550_db_user:javohir1234@kinobot1.vlz17q5.mongodb.net/?appName=kinobot1"),
    mongodb_db=os.getenv("MONGODB_DB", "referral_stars_bot"),
    webhook_secret=os.getenv("WEBHOOK_SECRET", "secret123"),
    public_base_url=os.getenv("PUBLIC_BASE_URL", ""),
    admin_ids=csv_ints(os.getenv("ADMIN_IDS", "6968399046")),
    bot_username=os.getenv("BOT_USERNAME", "java_free_things_bot"),
    default_emoji_id=os.getenv("DEFAULT_EMOJI_ID", "5458794766248459827"),
    star_emoji_id=os.getenv("STAR_EMOJI_ID", "5283231578523204600"),
)

if not settings.bot_token:
    raise RuntimeError("BOT_TOKEN Render Environment ichida yozilishi kerak.")


bot = TeleBot(settings.bot_token, parse_mode="HTML", threaded=False)
app = Flask(__name__)
client = MongoClient(
    settings.mongodb_uri,
    tls=True,
    tlsCAFile=certifi.where(),
    serverSelectionTimeoutMS=20000,
)
db = client[settings.mongodb_db]

users = db.users
channels = db.mandatory_channels
withdrawals = db.withdrawals

state: dict[int, dict[str, Any]] = {}
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


def btn(text: str, *, style: str = "primary", **kwargs) -> StyledInlineKeyboardButton:
    return StyledInlineKeyboardButton(
        text=text,
        style=style,
        icon_custom_emoji_id=settings.default_emoji_id or None,
        **kwargs,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def star() -> str:
    emoji_id = settings.star_emoji_id or settings.default_emoji_id
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">⭐</tg-emoji>'
    return "⭐"


def is_admin(user_id: int) -> bool:
    return bool(settings.admin_ids) and user_id in settings.admin_ids


def ensure_indexes() -> None:
    users.create_index("telegram_id", unique=True)
    users.create_index([("balance", DESCENDING)])
    channels.create_index("chat_id", unique=True)
    withdrawals.create_index([("status", ASCENDING), ("created_at", DESCENDING)])


def get_bot_username() -> str:
    if settings.bot_username:
        return settings.bot_username.lstrip("@")
    return bot.get_me().username


def ref_link(user_id: int) -> str:
    return f"https://t.me/{get_bot_username()}?start=ref_{user_id}"


def save_user(message: Message, referred_by: int | None = None) -> dict:
    payload = {
        "telegram_id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "last_name": message.from_user.last_name,
        "updated_at": utcnow(),
    }
    on_insert: dict[str, Any] = {
        "balance": 0,
        "earned_total": 0,
        "withdrawn_total": 0,
        "referrals_count": 0,
        "referral_rewarded": False,
        "created_at": utcnow(),
    }
    if referred_by and referred_by != message.from_user.id:
        on_insert["referred_by"] = referred_by
    return users.find_one_and_update(
        {"telegram_id": message.from_user.id},
        {"$set": payload, "$setOnInsert": on_insert},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )


def name_of(user: dict) -> str:
    return user.get("first_name") or user.get("username") or str(user.get("telegram_id"))


def normalize_channel(text: str) -> tuple[str, str]:
    value = text.strip()
    if value.startswith("https://t.me/"):
        username = value.removeprefix("https://t.me/").split("/", 1)[0]
        return f"@{username}", f"https://t.me/{username}"
    if value.startswith("t.me/"):
        username = value.removeprefix("t.me/").split("/", 1)[0]
        return f"@{username}", f"https://t.me/{username}"
    if value.startswith("@"):
        return value, f"https://t.me/{value[1:]}"
    return value, value


def bot_is_admin(chat_id: str | int) -> bool:
    try:
        member = bot.get_chat_member(chat_id, bot.get_me().id)
        return member.status in {"administrator", "creator"}
    except ApiTelegramException:
        return False


def user_is_subscribed(user_id: int) -> bool:
    active = list(channels.find({"active": True}))
    if not active:
        return True
    for channel in active:
        try:
            member = bot.get_chat_member(channel["chat_id"], user_id)
            if member.status in {"left", "kicked"}:
                return False
        except ApiTelegramException:
            return False
    return True


def subscribe_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for channel in channels.find({"active": True}).sort("created_at", ASCENDING):
        title = channel.get("title") or str(channel.get("chat_id"))
        url = channel.get("invite_link") or channel.get("url")
        if url:
            markup.add(btn(f"📌 {title}", url=url, style="success"))
    markup.add(btn("✅ Obunani tekshirish", callback_data="user:check_sub", style="primary"))
    return markup


def user_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        btn("⭐ Balans", callback_data="user:balance", style="primary"),
        btn("🔗 Referal link", callback_data="user:ref", style="success"),
        btn("💸 Stars yechish", callback_data="user:withdraw", style="success"),
        btn("📊 Statistika", callback_data="user:stats", style="primary"),
        btn("✅ Obunani tekshirish", callback_data="user:check_sub", style="primary"),
    )
    return markup


def admin_keyboard() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        btn("📊 Statistika", callback_data="admin:stats", style="primary"),
        btn("💸 Yechishlar", callback_data="admin:withdrawals", style="success"),
        btn("📈 Yechilgan statistika", callback_data="admin:withdraw_stats", style="primary"),
        btn("📌 Obuna qo'shish", callback_data="admin:add_channel", style="success"),
        btn("🧾 Obunalar", callback_data="admin:list_channels", style="primary"),
        btn("🗑 Obuna o'chirish", callback_data="admin:del_channel", style="danger"),
        btn("👥 Userlarga xabar", callback_data="admin:broadcast", style="success"),
        btn("❌ Yopish", callback_data="admin:close", style="danger"),
    )
    return markup


def withdraw_keyboard(withdraw_id: ObjectId) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        btn("✅ To'landi", callback_data=f"withdraw:approve:{withdraw_id}", style="success"),
        btn("❌ Rad etish", callback_data=f"withdraw:reject:{withdraw_id}", style="danger"),
    )
    return markup


def reward_referrer(user_id: int) -> bool:
    user = users.find_one({"telegram_id": user_id}) or {}
    referrer_id = user.get("referred_by")
    if not referrer_id or user.get("referral_rewarded"):
        return False
    if not user_is_subscribed(user_id):
        return False
    changed = users.update_one(
        {"telegram_id": user_id, "referral_rewarded": {"$ne": True}},
        {"$set": {"referral_rewarded": True, "rewarded_at": utcnow()}},
    )
    if changed.modified_count != 1:
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
        bot.send_message(referrer_id, f"{star()} <b>Yangi referal!</b>\nBalansingizga <b>1 Stars</b> qo'shildi.")
    except ApiTelegramException:
        pass
    return True


def send_subscribe(chat_id: int) -> None:
    bot.send_message(
        chat_id,
        "🔐 <b>Majburiy obuna</b>\n\nBotdan foydalanish uchun avval kanallarga obuna bo'ling.",
        reply_markup=subscribe_keyboard(),
        disable_web_page_preview=True,
    )


def send_panel(chat_id: int, user: dict) -> None:
    bot.send_message(
        chat_id,
        (
            f"👋 <b>Hush kelibsiz, {name_of(user)}!</b>\n\n"
            f"{star()} Referal narxi: <b>{REFERRAL_REWARD} Stars</b>\n"
            f"💸 Minimum yechish: <b>{MIN_WITHDRAW} Stars</b>\n\n"
            "Quyidagi paneldan foydalaning."
        ),
        reply_markup=user_keyboard(),
        disable_web_page_preview=True,
    )


def configure_webhook() -> dict[str, Any]:
    if not settings.public_base_url:
        return {"ok": False, "error": "PUBLIC_BASE_URL yozilmagan"}
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
    user = save_user(message, referred_by)
    if not user_is_subscribed(message.from_user.id):
        send_subscribe(message.chat.id)
        return
    reward_referrer(message.from_user.id)
    send_panel(message.chat.id, user)


@bot.message_handler(commands=["admin"])
def admin(message: Message) -> None:
    if message.chat.type != "private" or not is_admin(message.from_user.id):
        return
    save_user(message)
    state.pop(message.from_user.id, None)
    bot.send_message(message.chat.id, "🛠 <b>Admin panel</b>", reply_markup=admin_keyboard())


@bot.callback_query_handler(func=lambda call: call.data.startswith("user:"))
def user_callbacks(call) -> None:
    user = users.find_one({"telegram_id": call.from_user.id})
    if not user:
        bot.answer_callback_query(call.id, "Avval /start bosing.", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    if action == "check_sub":
        if user_is_subscribed(call.from_user.id):
            reward_referrer(call.from_user.id)
            bot.answer_callback_query(call.id, "✅ Obuna tasdiqlandi.")
            bot.edit_message_text(
                f"✅ Obuna tasdiqlandi.\n\n👋 <b>Hush kelibsiz, {name_of(user)}!</b>",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=user_keyboard(),
            )
        else:
            bot.answer_callback_query(call.id, "Avval kanallarga obuna bo'ling.", show_alert=True)
        return
    if not user_is_subscribed(call.from_user.id):
        bot.answer_callback_query(call.id, "Avval majburiy obunani bajaring.", show_alert=True)
        return
    if action == "balance":
        text = (
            f"{star()} <b>Balansingiz</b>\n\n"
            f"Joriy balans: <b>{user.get('balance', 0)} Stars</b>\n"
            f"Jami ishlangan: <b>{user.get('earned_total', 0)} Stars</b>\n"
            f"Jami yechilgan: <b>{user.get('withdrawn_total', 0)} Stars</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=user_keyboard())
    elif action == "ref":
        text = (
            "🔗 <b>Sizning referal linkingiz</b>\n\n"
            f"<code>{ref_link(call.from_user.id)}</code>\n\n"
            "Har bir do'stingiz obuna bo'lsa <b>1 Stars</b> olasiz."
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=user_keyboard())
    elif action == "stats":
        text = (
            "📊 <b>Mening statistikam</b>\n\n"
            f"👥 Referallar: <b>{user.get('referrals_count', 0)}</b>\n"
            f"{star()} Balans: <b>{user.get('balance', 0)} Stars</b>\n"
            f"💸 Yechilgan: <b>{user.get('withdrawn_total', 0)} Stars</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=user_keyboard())
    elif action == "withdraw":
        balance = int(user.get("balance", 0))
        if balance < MIN_WITHDRAW:
            bot.answer_callback_query(call.id, f"Minimum yechish {MIN_WITHDRAW} Stars.", show_alert=True)
            return
        state[call.from_user.id] = {"step": "withdraw_amount"}
        bot.send_message(call.message.chat.id, f"💸 Miqdorni yuboring.\nBalans: <b>{balance} Stars</b>")
        bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin:"))
def admin_callbacks(call) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.", show_alert=True)
        return
    action = call.data.split(":", 1)[1]
    state.pop(call.from_user.id, None)
    if action == "close":
        bot.delete_message(call.message.chat.id, call.message.message_id)
    elif action == "stats":
        refs = sum(u.get("referrals_count", 0) for u in users.find({}, {"referrals_count": 1}))
        earned = sum(u.get("earned_total", 0) for u in users.find({}, {"earned_total": 1}))
        text = (
            "📊 <b>Bot statistikasi</b>\n\n"
            f"👥 Foydalanuvchilar: <b>{users.count_documents({})}</b>\n"
            f"🔗 Referallar: <b>{refs}</b>\n"
            f"{star()} Jami ishlangan: <b>{earned} Stars</b>\n"
            f"📌 Obunalar: <b>{channels.count_documents({'active': True})}</b>"
        )
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
    elif action == "withdraw_stats":
        paid = list(withdrawals.find({"status": "approved"}))
        total = sum(int(w.get("amount", 0)) for w in paid)
        pending = withdrawals.count_documents({"status": "pending"})
        text = f"📈 <b>Yechilgan statistika</b>\n\n✅ To'langan: <b>{len(paid)}</b>\n⏳ Kutilayotgan: <b>{pending}</b>\n💸 Jami: <b>{total} Stars</b>"
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
    elif action == "withdrawals":
        pending = list(withdrawals.find({"status": "pending"}).sort("created_at", ASCENDING).limit(10))
        if not pending:
            bot.send_message(call.message.chat.id, "💸 Kutilayotgan yechish so'rovlari yo'q.", reply_markup=admin_keyboard())
        for item in pending:
            user = users.find_one({"telegram_id": item["user_id"]}) or {}
            text = (
                "💸 <b>Yechish so'rovi</b>\n\n"
                f"👤 User: <code>{item['user_id']}</code> @{user.get('username', '-')}\n"
                f"{star()} Miqdor: <b>{item['amount']} Stars</b>\n"
                f"📝 Ma'lumot: <code>{item.get('payout_info', '-')}</code>"
            )
            bot.send_message(call.message.chat.id, text, reply_markup=withdraw_keyboard(item["_id"]))
    elif action == "add_channel":
        state[call.from_user.id] = {"step": "add_channel"}
        bot.send_message(call.message.chat.id, "📌 Kanal username/link yuboring. Masalan: @kanal")
    elif action == "list_channels":
        lines = ["🧾 <b>Majburiy obunalar</b>"]
        for channel in channels.find({"active": True}).sort("created_at", ASCENDING):
            status = "✅ admin" if channel.get("bot_admin") else "⚠️ admin emas"
            lines.append(f"\n<code>{channel['chat_id']}</code> - {channel.get('title', 'Kanal')} - {status}")
        if len(lines) == 1:
            lines.append("\nHozircha kanal yo'q.")
        bot.edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=admin_keyboard())
    elif action == "del_channel":
        state[call.from_user.id] = {"step": "del_channel"}
        bot.send_message(call.message.chat.id, "🗑 O'chiriladigan kanal username yoki ID yuboring.")
    elif action == "broadcast":
        state[call.from_user.id] = {"step": "broadcast"}
        bot.send_message(call.message.chat.id, "👥 Hamma foydalanuvchilarga yuboriladigan xabarni jo'nating.")


@bot.callback_query_handler(func=lambda call: call.data.startswith("withdraw:"))
def withdraw_callbacks(call) -> None:
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Ruxsat yo'q.", show_alert=True)
        return
    _, action, withdraw_id = call.data.split(":", 2)
    try:
        object_id = ObjectId(withdraw_id)
    except Exception:
        bot.answer_callback_query(call.id, "ID xato.", show_alert=True)
        return
    item = withdrawals.find_one({"_id": object_id, "status": "pending"})
    if not item:
        bot.answer_callback_query(call.id, "So'rov topilmadi.", show_alert=True)
        return
    if action == "approve":
        withdrawals.update_one({"_id": object_id}, {"$set": {"status": "approved", "approved_at": utcnow(), "admin_id": call.from_user.id}})
        users.update_one({"telegram_id": item["user_id"]}, {"$inc": {"withdrawn_total": int(item["amount"])}})
        bot.answer_callback_query(call.id, "To'landi.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        try:
            bot.send_message(item["user_id"], f"✅ <b>{item['amount']} Stars</b> yechish so'rovingiz to'landi.")
        except ApiTelegramException:
            pass
    elif action == "reject":
        withdrawals.update_one({"_id": object_id}, {"$set": {"status": "rejected", "rejected_at": utcnow(), "admin_id": call.from_user.id}})
        users.update_one({"telegram_id": item["user_id"]}, {"$inc": {"balance": int(item["amount"])}})
        bot.answer_callback_query(call.id, "Rad etildi.")
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        try:
            bot.send_message(item["user_id"], f"❌ So'rov rad etildi. <b>{item['amount']} Stars</b> balansga qaytarildi.")
        except ApiTelegramException:
            pass


@bot.message_handler(content_types=["text", "photo", "video", "document", "animation", "sticker"])
def message_router(message: Message) -> None:
    if message.chat.type != "private":
        return
    save_user(message)
    current = state.get(message.from_user.id)
    if is_admin(message.from_user.id) and current and current.get("step") in {"add_channel", "del_channel", "broadcast"}:
        handle_admin_message(message)
        return
    if current and current.get("step") in {"withdraw_amount", "withdraw_info"}:
        handle_user_message(message)
        return
    if not user_is_subscribed(message.from_user.id):
        send_subscribe(message.chat.id)
        return
    user = users.find_one({"telegram_id": message.from_user.id}) or {}
    send_panel(message.chat.id, user)


def handle_user_message(message: Message) -> None:
    current = state.get(message.from_user.id) or {}
    user = users.find_one({"telegram_id": message.from_user.id}) or {}
    if current.get("step") == "withdraw_amount":
        try:
            amount = int((message.text or "").strip())
        except ValueError:
            bot.send_message(message.chat.id, "Miqdor raqam bo'lishi kerak.")
            return
        balance = int(user.get("balance", 0))
        if amount < MIN_WITHDRAW:
            bot.send_message(message.chat.id, f"Minimum yechish {MIN_WITHDRAW} Stars.")
            return
        if amount > balance:
            bot.send_message(message.chat.id, f"Balansingizda {balance} Stars bor.")
            return
        state[message.from_user.id] = {"step": "withdraw_info", "amount": amount}
        bot.send_message(message.chat.id, "Stars qabul qilish uchun username yoki ma'lumot yuboring.")
        return
    if current.get("step") == "withdraw_info":
        amount = int(current["amount"])
        changed = users.update_one(
            {"telegram_id": message.from_user.id, "balance": {"$gte": amount}},
            {"$inc": {"balance": -amount}, "$set": {"updated_at": utcnow()}},
        )
        if changed.modified_count != 1:
            state.pop(message.from_user.id, None)
            bot.send_message(message.chat.id, "Balans yetarli emas.", reply_markup=user_keyboard())
            return
        doc = {
            "user_id": message.from_user.id,
            "amount": amount,
            "payout_info": message.text or "-",
            "status": "pending",
            "created_at": utcnow(),
        }
        inserted = withdrawals.insert_one(doc)
        state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, "✅ Yechish so'rovi adminga yuborildi.", reply_markup=user_keyboard())
        for admin_id in settings.admin_ids:
            try:
                bot.send_message(
                    admin_id,
                    f"💸 <b>Yangi yechish so'rovi</b>\n\n👤 User: <code>{message.from_user.id}</code>\n{star()} Miqdor: <b>{amount} Stars</b>\n📝 Ma'lumot: <code>{message.text or '-'}</code>",
                    reply_markup=withdraw_keyboard(inserted.inserted_id),
                )
            except ApiTelegramException:
                pass


def handle_admin_message(message: Message) -> None:
    current = state.get(message.from_user.id) or {}
    step = current.get("step")
    if step == "add_channel":
        chat_id, url = normalize_channel(message.text or "")
        title = chat_id
        admin_ok = bot_is_admin(chat_id)
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
                    "bot_admin": admin_ok,
                    "active": True,
                    "updated_at": utcnow(),
                },
                "$setOnInsert": {"created_at": utcnow()},
            },
            upsert=True,
        )
        state.pop(message.from_user.id, None)
        status = "✅ Bot kanalda admin." if admin_ok else "⚠️ Botni kanalda admin qiling."
        bot.send_message(message.chat.id, f"📌 Kanal qo'shildi: <code>{chat_id}</code>\n{status}", reply_markup=admin_keyboard())
    elif step == "del_channel":
        chat_id, _ = normalize_channel(message.text or "")
        result = channels.update_one({"chat_id": chat_id}, {"$set": {"active": False, "updated_at": utcnow()}})
        state.pop(message.from_user.id, None)
        text = "🗑 Kanal o'chirildi." if result.matched_count else "Kanal topilmadi."
        bot.send_message(message.chat.id, text, reply_markup=admin_keyboard())
    elif step == "broadcast":
        ok = 0
        fail = 0
        for user in users.find({}, {"telegram_id": 1}):
            try:
                bot.copy_message(user["telegram_id"], message.chat.id, message.message_id)
                ok += 1
            except ApiTelegramException:
                fail += 1
        state.pop(message.from_user.id, None)
        bot.send_message(message.chat.id, f"👥 Xabar yuborildi.\n✅ {ok} ta\n❌ {fail} ta", reply_markup=admin_keyboard())


@app.get("/")
def home():
    return {"ok": True, "service": "Referral Stars Bot", "setup_webhook": "/setup-webhook", "webhook_info": "/webhook-info"}


@app.post(f"/webhook/{settings.webhook_secret}")
def telegram_webhook():
    if request.headers.get("content-type") != "application/json":
        abort(403)
    update = Update.de_json(request.get_data().decode("utf-8"))
    bot.process_new_updates([update])
    return {"ok": True}


@app.get("/setup-webhook")
@app.get("/setup-webhook/")
@app.get(f"/setup-webhook/{settings.webhook_secret}")
@app.get(f"/setup-webhook/{settings.webhook_secret}/")
def setup_webhook():
    result = configure_webhook()
    return result, 200 if result.get("ok") else 400


@app.get("/webhook-info")
@app.get("/webhook-info/")
@app.get(f"/webhook-info/{settings.webhook_secret}")
@app.get(f"/webhook-info/{settings.webhook_secret}/")
def webhook_info():
    return bot.get_webhook_info().to_dict()


@app.cli.command("set-webhook")
def set_webhook():
    result = configure_webhook()
    if not result.get("ok"):
        raise RuntimeError(result["error"])
    print(f"Webhook o'rnatildi: {result['webhook']}")


def start_polling() -> None:
    ensure_indexes()
    bot.remove_webhook()
    print("Polling ishga tushdi. To'xtatish uchun CTRL+C bosing.")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30, allowed_updates=["message", "callback_query"])


@app.cli.command("run-polling")
def run_polling():
    start_polling()


if __name__ == "__main__":
    if os.getenv("RUN_MODE", "flask").lower() == "polling":
        start_polling()
    else:
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
