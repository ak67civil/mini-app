import os
import time
import base64
import logging
import asyncio
import pymongo
from aiohttp import web
from pyrogram import Client, filters, idle, ContinuePropagation
from pyrogram.enums import ParseMode
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


def get_state(key, default=None):
    doc = state_col.find_one({"_id": key})
    return doc["value"] if doc else default


def set_state(key, value):
    state_col.update_one({"_id": key}, {"$set": {"value": value}}, upsert=True)


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
# TEMPORARY DEBUG — logs every single incoming message, no filters at all.
# Remove this once /start is confirmed working.
# ---------------------------------------------------------------------------
@app.on_message(group=-1)
async def debug_catch_all(client, message: Message):
    uid = message.from_user.id if message.from_user else "unknown"
    logger.info(f"[DEBUG] Incoming message from user {uid}: {message.text!r}")
    raise ContinuePropagation


# ---------------------------------------------------------------------------
# /addcourse — step-by-step wizard
# ---------------------------------------------------------------------------
@app.on_message(filters.command("addcourse") & filters.private & filters.user(OWNER_ID))
async def admin_addcourse(client, message: Message):
    set_state("course_step_index", 0)
    set_state("course_pending", {})
    await message.reply_text(f"<b>Add New Course</b>\n\n{WIZARD_STEPS[0][1]}", parse_mode=ParseMode.HTML)


@app.on_message(filters.private & (filters.text | filters.photo) & filters.user(OWNER_ID))
async def admin_wizard_step(client, message: Message):
    step_index = get_state("course_step_index")
    if step_index is None or (message.text and message.text.startswith("/")):
        raise ContinuePropagation
    pending = get_state("course_pending", {})
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

    set_state("course_pending", pending)

    next_index = step_index + 1
    if next_index < len(WIZARD_STEPS):
        set_state("course_step_index", next_index)
        await message.reply_text(WIZARD_STEPS[next_index][1])
        return

    # Wizard complete — save the course
    course_id = str(int(time.time() * 1000))
    lecture_count = pending.get("lecture_count", "")
    try:
        lecture_count = int(lecture_count) if lecture_count else None
    except ValueError:
        lecture_count = None

    courses_col.insert_one({
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
    set_state("course_step_index", None)
    set_state("course_pending", {})
    await message.reply_text(f"✅ Course added (ID: <code>{course_id}</code>). It's now live in the store.", parse_mode=ParseMode.HTML)


@app.on_message(filters.command("listcourses") & filters.private & filters.user(OWNER_ID))
async def admin_listcourses(client, message: Message):
    courses = list(courses_col.find())
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
    result = courses_col.delete_one({"_id": parts[1].strip()})
    await message.reply_text("✅ Removed." if result.deleted_count else "❌ Course not found.")


@app.on_message(filters.command("start") & filters.private)
async def cmd_start(client, message: Message):
    if message.from_user.id == OWNER_ID:
        await message.reply_text(
            "<b>Course Store — Admin</b>\n\n"
            "/addcourse - Add a new course (step-by-step)\n"
            "/listcourses - View all courses\n"
            "/removecourse &lt;id&gt; - Remove a course\n\n"
            "Set your Mini App as this bot's persistent Menu Button via "
            "@BotFather → Bot Settings → Menu Button, using your store's URL.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text("👋 Welcome! Tap the store button to browse available courses.")


# ---------------------------------------------------------------------------
# Web server — serves the Mini App + JSON API
# ---------------------------------------------------------------------------
async def api_courses(request):
    courses = list(courses_col.find())
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
    } for c in courses]
    return web.json_response(payload)


async def api_image(request):
    course_id = request.match_info["course_id"]
    course = courses_col.find_one({"_id": course_id})
    if not course or not course.get("image_data"):
        return web.Response(status=404)
    image_bytes = base64.b64decode(course["image_data"])
    return web.Response(body=image_bytes, content_type=course.get("image_mime", "image/jpeg"))


async def api_config(request):
    return web.json_response({"owner_username": OWNER_USERNAME, "app_title": APP_TITLE})


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
    logger.info("Course Store Bot starting... [BUILD-CHECK: continueprop-fix-v4]")
    await idle()
    await app.stop()


if __name__ == "__main__":
    asyncio.run(main())
