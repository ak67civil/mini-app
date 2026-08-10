import os
import re
import hmac
import time
import json
import base64
import hashlib
import logging
import asyncio
import unicodedata
import pymongo
from urllib.parse import parse_qsl
from aiohttp import web
from pyrogram import Client, filters, idle, ContinuePropagation
from pyrogram.enums import ParseMode, ChatType
from pyrogram.types import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (set these as environment variables on your host)
# ---------------------------------------------------------------------------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
MONGO_URL = os.environ["MONGO_URL"]
OWNER_ID = int(os.environ["OWNER_ID"])
OWNER_USERNAME = os.environ["OWNER_USERNAME"]      # without the @, e.g. "yourusername"
APP_TITLE = os.environ.get("APP_TITLE", "Course Store")
PORT = int(os.environ.get("PORT", 8080))

app = Client("course_store_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
mongo = pymongo.MongoClient(MONGO_URL)
db = mongo["course_store_bot"]
courses_col = db["courses"]   # {_id, name, category, price, batch_start, lecture_count, subjects, description}
state_col = db["state"]
visitors_col = db["visitors"]  # {_id: user_id, first_name, username, visit_count, first_visit, last_visit}


# ---------------------------------------------------------------------------
# Diagnostic — confirms whether Pyrogram is receiving message updates at
# all. Safe to leave in permanently; it only ever logs, never intercepts.
# ---------------------------------------------------------------------------
@app.on_message()
async def log_incoming(client, message: Message):
    uid = message.from_user.id if message.from_user else "unknown"
    logger.error(f"DIAGNOSTIC: message received from {uid}: {message.text!r}")
    raise ContinuePropagation


async def get_state(key, default=None):
    doc = await asyncio.to_thread(state_col.find_one, {"_id": key})
    return doc["value"] if doc else default


async def set_state(key, value):
    await asyncio.to_thread(state_col.update_one, {"_id": key}, {"$set": {"value": value}}, upsert=True)


async def save_course(doc):
    await asyncio.to_thread(courses_col.insert_one, doc)


async def list_courses():
    return await asyncio.to_thread(lambda: list(courses_col.find()))


async def delete_course(course_id):
    result = await asyncio.to_thread(courses_col.delete_one, {"_id": course_id})
    return result.deleted_count


async def get_course(course_id):
    return await asyncio.to_thread(courses_col.find_one, {"_id": course_id})


WIZARD_STEPS = [
    ("name", "What's the course name?"),
    ("category", "Which category/exam does it belong to? (e.g. GATE, MPSC, SSC, UPSC)"),
    ("price", "What's the price? (e.g. ₹1999 — you can include the ₹ symbol or any text)"),
    ("batch_start", "When does the batch start? (e.g. Jan 2026 — send \"skip\" to leave blank)"),
    ("lecture_count", "How many lectures does it include? (send a number, or \"skip\")"),
    ("subjects", "Which subjects/topics does it cover? (short text, or \"skip\")"),
    ("description", "Write a short description for the course detail page."),
    ("photo", "Send a photo/cover image for this course (or send \"skip\" to leave it without one)."),
]


# ---------------------------------------------------------------------------
# /autocourse — scan a channel and auto-build a course entry: thumbnail,
# lecture/PDF counts, and a topic-wise breakdown, generated automatically.
# ---------------------------------------------------------------------------
_SMALL_CAPS_MAP = str.maketrans("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀsᴛᴜᴠᴡxʏᴢ", "abcdefghijklmnopqrstuvwxyz")


def extract_topic_name(caption):
    """Reads a 'Topic : X' line from a caption (case-insensitive), robust
    to stylized fonts (both 'Mathematical Alphanumeric' styles via NFKC,
    and genuine small-caps like 'ᴛᴏᴘɪᴄ:' via a direct character map)."""
    if not caption or not caption.strip():
        return None
    for raw_line in caption.strip().split("\n"):
        line = unicodedata.normalize("NFKC", raw_line.strip())
        check_line = line.translate(_SMALL_CAPS_MAP)
        m = re.match(r"(?i)^(?:topic|विषय)\s*[:：]", check_line)
        if m:
            return line[m.end():].strip()
    return None


@app.on_message(filters.command("autocourse") & filters.private & filters.user(OWNER_ID))
async def admin_autocourse_start(client, message: Message):
    await set_state("auto_step", "channel")
    await message.reply_text(
        "<b>Auto-Build Course from Channel</b>\n\n"
        "Forward any message from the batch's channel (or send its numeric "
        "chat ID). I'll scan it, pick up its thumbnail, and count videos/PDFs "
        "per topic automatically.",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.private & (filters.forwarded | filters.text) & filters.user(OWNER_ID))
async def admin_autocourse_channel(client, message: Message):
    if await get_state("auto_step") != "channel":
        raise ContinuePropagation

    chat = message.forward_from_chat
    if not chat and message.text and not message.text.startswith("/") and message.text.strip().lstrip("-").isdigit():
        try:
            chat = await client.get_chat(int(message.text.strip()))
        except Exception as e:
            await message.reply_text(f"Couldn't resolve that ID: {e}")
            return
    if not chat or chat.type not in (ChatType.SUPERGROUP, ChatType.CHANNEL, ChatType.GROUP):
        await message.reply_text("That doesn't look like a channel/group. Forward a message from it, or send its numeric ID.")
        return

    status = await message.reply_text("🔎 Scanning channel — this may take a moment for large batches...")

    thumbnail_data, thumbnail_mime = None, None
    topic_counts = {}   # topic -> {"video": n, "pdf": n}
    total_video, total_pdf = 0, 0

    try:
        # Prefer the pinned message as the thumbnail if it's a photo.
        try:
            full_chat = await client.get_chat(chat.id)
            pinned = getattr(full_chat, "pinned_message", None)
            if pinned and pinned.photo:
                path = await pinned.download()
                with open(path, "rb") as f:
                    thumbnail_data = base64.b64encode(f.read()).decode("ascii")
                thumbnail_mime = "image/jpeg"
                os.remove(path)
        except Exception as e:
            logger.warning(f"Could not check pinned message for thumbnail: {e}")

        async for msg in client.get_chat_history(chat.id):
            caption = msg.caption or msg.text or ""
            topic = extract_topic_name(caption) or "Uncategorized"

            if msg.video:
                topic_counts.setdefault(topic, {"video": 0, "pdf": 0})
                topic_counts[topic]["video"] += 1
                total_video += 1
            elif msg.document:
                topic_counts.setdefault(topic, {"video": 0, "pdf": 0})
                topic_counts[topic]["pdf"] += 1
                total_pdf += 1
            elif msg.photo and not thumbnail_data:
                # Fall back to the first photo in the channel if no pinned photo was found.
                path = await msg.download()
                with open(path, "rb") as f:
                    thumbnail_data = base64.b64encode(f.read()).decode("ascii")
                thumbnail_mime = "image/jpeg"
                os.remove(path)
    except Exception as e:
        await status.edit_text(f"❌ Scan failed: {e}")
        await set_state("auto_step", None)
        return

    if not topic_counts:
        await status.edit_text("No videos or documents found in that channel.")
        await set_state("auto_step", None)
        return

    lines = [f"Total: {total_video} video lecture(s) and {total_pdf} PDF(s) across {len(topic_counts)} topic(s).", ""]
    lines.append("Topic-wise breakdown:")
    for topic, counts in topic_counts.items():
        parts = []
        if counts["video"]:
            parts.append(f"{counts['video']} video(s)")
        if counts["pdf"]:
            parts.append(f"{counts['pdf']} PDF(s)")
        lines.append(f"- {topic}: {', '.join(parts)}")
    description = "\n".join(lines)

    await set_state("auto_pending", {
        "lecture_count": total_video,
        "subjects": ", ".join(topic_counts.keys()),
        "description": description,
        "image_data": thumbnail_data,
        "image_mime": thumbnail_mime,
    })
    await set_state("auto_step", "name")

    await status.edit_text(
        f"✅ Scan complete.\n\n{description}\n\n"
        f"{'📷 Thumbnail found and will be used.' if thumbnail_data else '⚠️ No photo found in the channel — you can add one manually later.'}\n\n"
        f"Now let's fill in the rest. What's the course name?"
    )


@app.on_message(filters.private & filters.text & filters.user(OWNER_ID))
async def admin_autocourse_fields(client, message: Message):
    step = await get_state("auto_step")
    if step not in ("name", "category", "price"):
        raise ContinuePropagation
    if message.text.startswith("/"):
        raise ContinuePropagation

    pending = await get_state("auto_pending", {})
    pending[step] = message.text.strip()
    await set_state("auto_pending", pending)

    if step == "name":
        await set_state("auto_step", "category")
        await message.reply_text("Which category/exam does it belong to? (e.g. GATE, MPSC, SSC, UPSC)")
        return
    if step == "category":
        await set_state("auto_step", "price")
        await message.reply_text("What's the price? (e.g. ₹1999)")
        return

    # step == "price" — everything is ready, save the course
    course_id = str(int(time.time() * 1000))
    await save_course({
        "_id": course_id,
        "name": pending.get("name", "Untitled Course"),
        "category": pending.get("category", "General"),
        "price": pending.get("price", "Contact for price"),
        "batch_start": "",
        "lecture_count": pending.get("lecture_count"),
        "subjects": pending.get("subjects", ""),
        "description": pending.get("description", ""),
        "image_data": pending.get("image_data"),
        "image_mime": pending.get("image_mime"),
        "created_at": time.time(),
    })
    await set_state("auto_step", None)
    await set_state("auto_pending", {})
    await message.reply_text(f"✅ Course auto-built and live (ID: <code>{course_id}</code>).", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /importsyllabus — asks for a thumbnail URL, then parses a
# "(Topic) Caption : URL" .txt export directly. URLs may be encrypted/
# obfuscated (no reliable .m3u8/.pdf extension), so this counts total
# items per topic rather than trying to split video vs PDF.
# ---------------------------------------------------------------------------
SYLLABUS_LINE_RE = re.compile(r"^\((.*?)\)\s*(.*?)\s*:\s*(https?://\S+)\s*$")


@app.on_message(filters.command("importsyllabus") & filters.private & filters.user(OWNER_ID))
async def admin_importsyllabus_start(client, message: Message):
    await set_state("syl_step", "thumbnail")
    await message.reply_text(
        "<b>Import Course from Syllabus File</b>\n\n"
        "Step 1: send me the course thumbnail image URL (a plain, normal "
        "URL — not from the file, just paste the image link directly).",
        parse_mode=ParseMode.HTML,
    )


@app.on_message(filters.private & filters.text & filters.user(OWNER_ID))
async def admin_importsyllabus_thumbnail(client, message: Message):
    if await get_state("syl_step") != "thumbnail":
        raise ContinuePropagation
    if message.text.startswith("/"):
        raise ContinuePropagation

    url = message.text.strip()
    if not url.lower().startswith("http"):
        await message.reply_text("That doesn't look like a URL. Please paste the thumbnail image link (starting with http/https).")
        return

    await set_state("syl_pending", {"image_url": url})
    await set_state("syl_step", "file")
    await message.reply_text(
        "Step 2: now send me the .txt file for this batch — the one with "
        "lines like \"(Topic) Caption : URL\". I'll count how many items "
        "each topic has."
    )


@app.on_message(filters.private & filters.document & filters.user(OWNER_ID))
async def admin_importsyllabus_file(client, message: Message):
    if await get_state("syl_step") != "file":
        raise ContinuePropagation

    status = await message.reply_text("📄 Reading file...")
    path = await message.download()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        await status.edit_text(f"❌ Couldn't read that file: {e}")
        await set_state("syl_step", None)
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    topic_counts = {}
    total_items = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = SYLLABUS_LINE_RE.match(line)
        if not m:
            continue
        topic, _caption, _url = m.group(1).strip(), m.group(2).strip(), m.group(3).strip()

        if topic.lower() == "course thumbnail":
            continue  # thumbnail was already provided manually in step 1

        topic_counts[topic] = topic_counts.get(topic, 0) + 1
        total_items += 1

    if not topic_counts:
        await status.edit_text("Couldn't find any recognizable lines in that file — check the format matches \"(Topic) Caption : URL\".")
        await set_state("syl_step", None)
        return

    desc_lines = [
        f"Total: {total_items} lecture(s)/material(s) across {len(topic_counts)} topic(s).",
        "", "Topic-wise breakdown:",
    ]
    for topic, count in topic_counts.items():
        desc_lines.append(f"- {topic}: {count} total")
    description = "\n".join(desc_lines)

    pending = await get_state("syl_pending", {})
    pending["lecture_count"] = total_items
    pending["subjects"] = ", ".join(topic_counts.keys())
    pending["description"] = description
    await set_state("syl_pending", pending)
    await set_state("syl_step", "name")

    await status.edit_text(
        f"✅ File processed.\n\n{description}\n\n"
        f"Now let's fill in the rest. What's the course name?"
    )


@app.on_message(filters.private & filters.text & filters.user(OWNER_ID))
async def admin_importsyllabus_fields(client, message: Message):
    step = await get_state("syl_step")
    if step not in ("name", "category", "price"):
        raise ContinuePropagation
    if message.text.startswith("/"):
        raise ContinuePropagation

    pending = await get_state("syl_pending", {})
    pending[step] = message.text.strip()
    await set_state("syl_pending", pending)

    if step == "name":
        await set_state("syl_step", "category")
        await message.reply_text("Which category/exam does it belong to? (e.g. GATE, MPSC, SSC, UPSC)")
        return
    if step == "category":
        await set_state("syl_step", "price")
        await message.reply_text("What's the price? (e.g. ₹1999)")
        return

    course_id = str(int(time.time() * 1000))
    await save_course({
        "_id": course_id,
        "name": pending.get("name", "Untitled Course"),
        "category": pending.get("category", "General"),
        "price": pending.get("price", "Contact for price"),
        "batch_start": "",
        "lecture_count": pending.get("lecture_count"),
        "subjects": pending.get("subjects", ""),
        "description": pending.get("description", ""),
        "image_data": None,
        "image_mime": None,
        "image_url": pending.get("image_url"),
        "created_at": time.time(),
    })
    await set_state("syl_step", None)
    await set_state("syl_pending", {})
    await message.reply_text(f"✅ Course imported and live (ID: <code>{course_id}</code>).", parse_mode=ParseMode.HTML)


# ---------------------------------------------------------------------------
# /addcourse — step-by-step wizard
# ---------------------------------------------------------------------------
@app.on_message(filters.command("addcourse") & filters.private & filters.user(OWNER_ID))
async def admin_addcourse(client, message: Message):
    await set_state("course_step_index", 0)
    await set_state("course_pending", {})
    await message.reply_text(f"<b>Add New Course</b>\n\n{WIZARD_STEPS[0][1]}", parse_mode=ParseMode.HTML)


@app.on_message(filters.private & (filters.text | filters.photo) & filters.user(OWNER_ID))
async def admin_wizard_step(client, message: Message):
    step_index = await get_state("course_step_index")
    if step_index is None or (message.text and message.text.startswith("/")):
        raise ContinuePropagation
    pending = await get_state("course_pending", {})
    field, _ = WIZARD_STEPS[step_index]

    if field == "photo":
        if message.photo:
            path = await message.download()
            try:
                with open(path, "rb") as f:
                    pending["image_data"] = base64.b64encode(f.read()).decode("ascii")
                pending["image_mime"] = "image/jpeg"
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        elif message.text and message.text.strip().lower() == "skip":
            pass
        else:
            await message.reply_text("Please send a photo, or type \"skip\" to leave it without one.")
            return
    else:
        value = (message.text or "").strip()
        if value.lower() != "skip":
            pending[field] = value

    await set_state("course_pending", pending)

    next_index = step_index + 1
    if next_index < len(WIZARD_STEPS):
        await set_state("course_step_index", next_index)
        await message.reply_text(WIZARD_STEPS[next_index][1])
        return

    # Wizard complete — save the course
    course_id = str(int(time.time() * 1000))
    lecture_count = pending.get("lecture_count", "")
    try:
        lecture_count = int(lecture_count) if lecture_count else None
    except ValueError:
        lecture_count = None

    await save_course({
        "_id": course_id,
        "name": pending.get("name", "Untitled Course"),
        "category": pending.get("category", "General"),
        "price": pending.get("price", "Contact for price"),
        "batch_start": pending.get("batch_start", ""),
        "lecture_count": lecture_count,
        "subjects": pending.get("subjects", ""),
        "description": pending.get("description", ""),
        "image_data": pending.get("image_data"),
        "image_mime": pending.get("image_mime"),
        "created_at": time.time(),
    })
    await set_state("course_step_index", None)
    await set_state("course_pending", {})
    await message.reply_text(f"✅ Course added (ID: <code>{course_id}</code>). It's now live in the store.", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("listcourses") & filters.private & filters.user(OWNER_ID))
async def admin_listcourses(client, message: Message):
    courses = await list_courses()
    if not courses:
        await message.reply_text("No courses yet. Use /addcourse to add one.")
        return
    lines = [f"<code>{c['_id']}</code> — {c['name']} ({c['category']}) — {c['price']}" for c in courses]
    await message.reply_text("<b>Courses:</b>\n\n" + "\n".join(lines), parse_mode=ParseMode.HTML)


@app.on_message(filters.command("removecourse") & filters.private & filters.user(OWNER_ID))
async def admin_removecourse(client, message: Message):
    parts = message.text.split(" ", 1)
    if len(parts) < 2 or not parts[1].strip():
        await message.reply_text("Usage: /removecourse <id>\n\nSee IDs with /listcourses.")
        return
    deleted_count = await delete_course(parts[1].strip())
    await message.reply_text("✅ Removed." if deleted_count else "❌ Course not found.")


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client, message: Message):
    if message.from_user.id == OWNER_ID:
        await message.reply_text(
            "<b>Course Store — Admin Panel</b>\n\n"
            "<b>Add a Course</b>\n"
            "/addcourse — Add a course manually, step by step\n"
            "/autocourse — Auto-build a course by scanning a channel\n"
            "/importsyllabus — Auto-build a course from a syllabus .txt file\n\n"
            "<b>Manage Courses</b>\n"
            "/listcourses — View all courses\n"
            "/removecourse &lt;id&gt; — Remove a course\n\n"
            "<b>Visitors</b>\n"
            "/visitors — See who has opened the store, and how many times\n\n"
            "Send /help for a full explanation of how each command works.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text("👋 Welcome! Tap the store button to browse available courses.")


@app.on_message(filters.command("help") & filters.private & filters.user(OWNER_ID))
async def cmd_help(client, message: Message):
    await message.reply_text(
        "<b>Course Store — How It Works</b>\n\n"
        "This bot manages the courses shown in your Mini App storefront. "
        "Add or remove courses here, and the store updates immediately — "
        "no redeploy needed.\n\n"

        "<b>Three ways to add a course</b>\n\n"

        "<u>1. /addcourse — Manual entry</u>\n"
        "Best when you want full control over every field. The bot asks "
        "you one question at a time: name, category, price, batch start "
        "date, lecture count, subjects, description, and a cover photo.\n\n"

        "<u>2. /autocourse — Scan a channel</u>\n"
        "Use this when you <b>don't have a syllabus .txt file</b> for the "
        "batch. Forward a message from the channel (or send its numeric "
        "chat ID) — the bot must already be an admin in that channel. It "
        "reads every video and document in the channel's history, groups "
        "them by the \"Topic:\" line in each caption, picks up a thumbnail "
        "(the pinned photo, or the first photo found), and generates a "
        "full topic-wise breakdown automatically. You then just confirm "
        "the name, category, and price.\n\n"

        "<u>3. /importsyllabus — Import from a .txt file</u>\n"
        "Use this when you <b>do have a syllabus .txt file</b> for the "
        "batch (lines formatted as \"(Topic) Caption : URL\"). This is "
        "faster and doesn't require the bot to be a channel admin at all "
        "— it just reads the file. Since these links are often encrypted "
        "and don't reveal whether something is a video or a PDF, this "
        "counts total items per topic rather than splitting the two. "
        "You'll first be asked for a thumbnail image URL (paste it "
        "directly — not from inside the file), then for the .txt file "
        "itself, and finally to confirm the name, category, and price.\n\n"

        "<b>Rule of thumb:</b> if a syllabus .txt file exists for the "
        "batch, use /importsyllabus — it's quicker and needs no channel "
        "access. Otherwise, use /autocourse.\n\n"

        "<b>Managing courses</b>\n"
        "/listcourses shows every course with its ID. /removecourse "
        "&lt;id&gt; deletes one. There's currently no edit command — "
        "remove and re-add if something needs to change.\n\n"

        "<b>Tracking visitors</b>\n"
        "/visitors shows the total number of unique people who have "
        "opened your store, plus the 20 most recent — their name, "
        "username, how many times they've opened it, and when they last "
        "did. This is based on Telegram's own signed visitor data, so it "
        "can't be faked.\n\n"

        "<b>The storefront itself</b>\n"
        "Students open the Mini App from your bot's Menu Button (set via "
        "@BotFather → Bot Settings → Menu Button). They browse by "
        "category, tap a course for full details, and tapping "
        "\"Message to Purchase\" opens a direct chat with you — you "
        "handle payment and access yourself, the same as before.",
        parse_mode=ParseMode.HTML,
    )


# ---------------------------------------------------------------------------
# Web server — serves the Mini App + JSON API
# ---------------------------------------------------------------------------
async def api_courses(request):
    courses = await list_courses()
    payload = [{
        "id": c["_id"],
        "name": c["name"],
        "category": c["category"],
        "price": c["price"],
        "batch_start": c.get("batch_start", ""),
        "lecture_count": c.get("lecture_count"),
        "subjects": c.get("subjects", ""),
        "description": c.get("description", ""),
        "has_image": bool(c.get("image_data")),
        "image_url": c.get("image_url"),
    } for c in courses]
    return web.json_response(payload)


async def api_image(request):
    course_id = request.match_info["course_id"]
    course = await get_course(course_id)
    if not course or not course.get("image_data"):
        return web.Response(status=404)
    image_bytes = base64.b64decode(course["image_data"])
    return web.Response(body=image_bytes, content_type=course.get("image_mime", "image/jpeg"))


async def api_config(request):
    return web.json_response({"owner_username": OWNER_USERNAME, "app_title": APP_TITLE})


def verify_telegram_init_data(init_data: str) -> dict | None:
    """Verifies that initData actually came from Telegram (signed with the
    bot token), per Telegram's official WebApp validation algorithm. Returns
    the parsed user dict if valid, or None if the signature doesn't match."""
    try:
        parsed = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None

    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    computed_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        return None

    user_raw = parsed.get("user")
    if not user_raw:
        return None
    try:
        return json.loads(user_raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def api_visit(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"ok": False}, status=400)

    init_data = body.get("initData", "")
    user = verify_telegram_init_data(init_data)
    if not user or "id" not in user:
        return web.json_response({"ok": False}, status=403)

    await asyncio.to_thread(
        visitors_col.update_one,
        {"_id": user["id"]},
        {
            "$set": {
                "first_name": user.get("first_name", ""),
                "username": user.get("username", ""),
                "last_visit": time.time(),
            },
            "$setOnInsert": {"first_visit": time.time()},
            "$inc": {"visit_count": 1},
        },
        upsert=True,
    )
    return web.json_response({"ok": True})


@app.on_message(filters.command("visitors") & filters.private & filters.user(OWNER_ID))
async def admin_visitors(client, message: Message):
    total = await asyncio.to_thread(visitors_col.count_documents, {})
    if not total:
        await message.reply_text("No one has opened the store yet.")
        return

    recent = await asyncio.to_thread(lambda: list(visitors_col.find().sort("last_visit", -1).limit(20)))
    lines = []
    for v in recent:
        uname = f"@{v['username']}" if v.get("username") else "no username"
        last = time.strftime("%d %b, %H:%M", time.localtime(v["last_visit"]))
        lines.append(f"• {v.get('first_name', 'Unknown')} ({uname}) — {v.get('visit_count', 1)} visit(s), last: {last}")

    await message.reply_text(
        f"<b>Store Visitors</b>\n\n"
        f"Total unique visitors: <b>{total}</b>\n\n"
        f"Most recent (up to 20):\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


WEBAPP_DIR = os.path.join(os.path.dirname(__file__), "webapp")


async def serve_index(request):
    index_path = os.path.join(WEBAPP_DIR, "index.html")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return web.Response(text=f.read(), content_type="text/html")
    except FileNotFoundError:
        return web.Response(
            status=500,
            text=f"index.html not found at {index_path} — check it was committed to the webapp/ folder.",
        )


def build_web_app():
    web_app = web.Application()
    web_app.router.add_get("/", serve_index)
    web_app.router.add_get("/api/courses", api_courses)
    web_app.router.add_get("/api/config", api_config)
    web_app.router.add_get("/api/image/{course_id}", api_image)
    web_app.router.add_post("/api/visit", api_visit)
    return web_app


async def start_web_server():
    web_app = build_web_app()
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server running on port {PORT}")


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
async def main():
    await app.start()
    asyncio.create_task(start_web_server())
    logger.info("Course Store Bot starting...")
    await idle()
    await app.stop()


if __name__ == "__main__":
    app.run(main())
