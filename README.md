# Course Store — Telegram Mini App

A branded, in-Telegram storefront where you manage courses via bot commands,
and students browse them like a normal shopping app — categories, full
course details, and a "Message to Purchase" button that opens a direct
chat with you so you can close the sale yourself.

## Files
- `bot.py` — Pyrogram bot (course management) + web server (serves the Mini App and its API)
- `webapp/index.html` — the Mini App itself (single file, no build step)
- `requirements.txt`, `Procfile` (type: `web`, not `worker` — this app needs to receive HTTP traffic), `runtime.txt`

## Environment Variables

| Variable | Where to get it |
|---|---|
| `API_ID` | https://my.telegram.org |
| `API_HASH` | https://my.telegram.org |
| `BOT_TOKEN` | @BotFather → `/newbot` |
| `MONGO_URL` | Your MongoDB connection string |
| `OWNER_ID` | Your Telegram numeric user ID (@userinfobot) |
| `OWNER_USERNAME` | Your Telegram @username, **without** the @ — this is who "Message to Purchase" contacts |
| `APP_TITLE` | Optional — the store's display name (defaults to "Course Store") |

Heroku sets `PORT` automatically — you don't need to add it yourself.

## First-time setup

1. Deploy this app (as a `web` dyno — the Procfile is already set to `web:`).
2. Once it's running, copy your app's public URL, e.g.
   `https://your-app-name.herokuapp.com`
3. Open @BotFather → `/mybots` → select your bot → **Bot Settings** →
   **Menu Button** → **Configure Menu Button** → paste your app's URL.
   This gives your bot a persistent "Open Store" button next to the message box.
4. DM your bot `/start` — as the owner, you'll see the admin command list.
5. Add your first course with `/addcourse` — it'll walk you through name,
   category, price, batch start date, lecture count, subjects, and description.
6. Open the Menu Button in Telegram to see your store live.

## Admin Commands

| Command | What it does |
|---|---|
| `/addcourse` | Add a new course, step by step |
| `/listcourses` | View all courses with their IDs |
| `/removecourse <id>` | Remove a course |

## How students use it

- They tap the store's Menu Button in your bot.
- They browse by category, tap a course to see full details.
- Tapping **"Message to Purchase"** opens a chat with you (`OWNER_USERNAME`),
  with a pre-filled message naming the course — you handle payment and
  access yourself, exactly as before.

## Notes

- New courses appear in the store immediately — no redeploy needed, since
  the app reads live from the database.
- The Mini App's look (admit-card/ledger inspired: stamped batch codes,
  ticket-stub cards) lives entirely in `webapp/index.html` — edit the
  `<style>` block there if you want to adjust colors or layout.
