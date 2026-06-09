from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    CommandHandler, ContextTypes, filters
)
import requests
import re
import os
import io
import time
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]

# ─── API endpoints ────────────────────────────────────────────────
AIRSONGS_API = "https://airsongsapi.vercel.app"
YT_API = "https://yt--air--78q4gfqgnf5j.code.run"  # your hosted yt service

# ─── In-memory state ──────────────────────────────────────────────
# user settings: {user_id: "airsongs" | "youtube"}
user_source = {}
# result cache: {key: result_dict}
result_cache = {}
cache_counter = [0]


def get_source(uid):
    return user_source.get(uid, "airsongs")


def cache_result(result):
    cache_counter[0] += 1
    key = str(cache_counter[0])
    result_cache[key] = result
    # keep memory bounded
    if cache_counter[0] > 500:
        result_cache.pop(str(cache_counter[0] - 500), None)
    return key


# ─── Utility ──────────────────────────────────────────────────────
def fmt_dur(sec):
    if not sec:
        return "?"
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m2 = divmod(m, 60)
    return f"{h}:{m2:02}:{s:02}" if h else f"{m}:{s:02}"


def safe_name(text):
    return re.sub(r"[^a-zA-Z0-9 _\-]", "", text or "").strip() or "audio"


# ─── AirSongs search ──────────────────────────────────────────────
def airsongs_search(query, limit=6):
    r = requests.get(f"{AIRSONGS_API}/result/", params={"query": query}, timeout=15)
    data = r.json()
    if not isinstance(data, list):
        return []
    return data[:limit]


def airsongs_audio_bytes(song):
    media_url = song.get("media_url")
    if not media_url:
        return None, None
    r = requests.get(media_url, timeout=60)
    fname = f"{safe_name(song.get('song', 'song'))}.m4a"
    return r.content, fname


# ─── YouTube search (InnerTube) ───────────────────────────────────
def yt_search(query, limit=6):
    url = "https://www.youtube.com/youtubei/v1/search?prettyPrint=false"
    payload = {
        "context": {"client": {"clientName": "WEB", "clientVersion": "2.20240101.00.00"}},
        "query": query,
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "X-YouTube-Client-Name": "1",
        "X-YouTube-Client-Version": "2.20240101.00.00",
    }
    r = requests.post(url, json=payload, headers=headers, timeout=15)
    data = r.json()
    videos = []
    try:
        contents = (data["contents"]["twoColumnSearchResultsRenderer"]
                    ["primaryContents"]["sectionListRenderer"]["contents"])
        for section in contents:
            items = section.get("itemSectionRenderer", {}).get("contents", [])
            for item in items:
                v = item.get("videoRenderer")
                if not v:
                    continue
                vid_id = v.get("videoId")
                title = v.get("title", {}).get("runs", [{}])[0].get("text", "")
                channel = v.get("ownerText", {}).get("runs", [{}])[0].get("text", "Unknown")
                dur_text = v.get("lengthText", {}).get("simpleText", "?")
                dur_sec = 0
                try:
                    parts = dur_text.split(":")
                    if len(parts) == 2:
                        dur_sec = int(parts[0]) * 60 + int(parts[1])
                    elif len(parts) == 3:
                        dur_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                except Exception:
                    pass
                videos.append({
                    "id": vid_id, "title": title, "channel": channel,
                    "duration": dur_sec, "duration_str": dur_text,
                })
                if len(videos) >= limit:
                    return videos
    except Exception as e:
        log.error(f"YT parse error: {e}")
    return videos


def yt_request_job(video_id, chat_id):
    """Ask the hosted yt service to deliver audio to the chat directly (async)."""
    try:
        r = requests.post(
            YT_API + "/download",
            json={
                "id": video_id,
                "mode": "audio",
                "telegram": True,
                "chat_id": chat_id,
            },
            timeout=30,
        )
        data = r.json()
        if data.get("status") == "success":
            return data.get("job")
        log.error(f"yt service failed: {data}")
        return None
    except Exception as e:
        log.error(f"yt_request_job error: {e}")
        return None


def yt_audio_via_loader(video_id, title):
    """Fallback: loader.to conversion."""
    try:
        r = requests.post(
            "https://loader.to/api/button/",
            data={"url": f"https://www.youtube.com/watch?v={video_id}", "f": "mp3", "lang": "en"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        token = r.json().get("id")
        if not token:
            return None, None
        for _ in range(15):
            time.sleep(4)
            p = requests.get(
                f"https://loader.to/api/progress/?id={token}",
                headers={"User-Agent": "Mozilla/5.0"}, timeout=15,
            ).json()
            if p.get("success") == 1 and p.get("download_url"):
                audio = requests.get(p["download_url"], timeout=120,
                                     headers={"User-Agent": "Mozilla/5.0"})
                return audio.content, f"{safe_name(title)}.mp3"
        return None, None
    except Exception as e:
        log.error(f"loader.to error: {e}")
        return None, None


# ─── Keyboards ────────────────────────────────────────────────────
def settings_keyboard(uid):
    src = get_source(uid)
    a = "✅ AirSongs" if src == "airsongs" else "AirSongs"
    y = "✅ YouTube" if src == "youtube" else "YouTube"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(a, callback_data="src_airsongs")],
        [InlineKeyboardButton(y, callback_data="src_youtube")],
    ])


def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⚙️ Settings", callback_data="open_settings")],
    ])


# ─── Command handlers ─────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"🎵 QuantX Music Bot\n\n"
        f"Just type any song or video name to search.\n"
        f"Current source: {get_source(uid).title()}\n\n"
        f"Use ⚙️ Settings to switch between AirSongs and YouTube.",
        reply_markup=main_menu_keyboard(),
    )


# ─── Message handler (search by name) ─────────────────────────────
async def handle_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    query = update.message.text.strip()
    if not query:
        return

    source = get_source(uid)
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    if source == "airsongs":
        try:
            songs = airsongs_search(query)
        except Exception as e:
            log.error(f"airsongs search: {e}")
            await update.message.reply_text("❌ Search failed. Try again.")
            return
        if not songs:
            await update.message.reply_text("❌ No songs found.")
            return
        buttons = []
        for song in songs:
            key = cache_result({"type": "airsongs", "data": song})
            label = f"🎵 {song.get('song')} — {song.get('primary_artists', '')}"[:60]
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick_{key}")])
        await update.message.reply_text(
            f"🔍 Results for \"{query}\":",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    else:  # youtube
        try:
            videos = yt_search(query)
        except Exception as e:
            log.error(f"yt search: {e}")
            await update.message.reply_text("❌ Search failed. Try again.")
            return
        if not videos:
            await update.message.reply_text("❌ No videos found.")
            return
        buttons = []
        for v in videos:
            key = cache_result({"type": "youtube", "data": v})
            label = f"🎬 {v['title']} [{v['duration_str']}]"[:60]
            buttons.append([InlineKeyboardButton(label, callback_data=f"pick_{key}")])
        await update.message.reply_text(
            f"🔍 Results for \"{query}\":",
            reply_markup=InlineKeyboardMarkup(buttons),
        )


# ─── Callback handler ─────────────────────────────────────────────
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    chat_id = query.message.chat.id
    data = query.data

    # settings
    if data == "open_settings":
        await query.answer()
        await query.message.reply_text("⚙️ Choose your search source:",
                                       reply_markup=settings_keyboard(uid))
        return

    if data == "src_airsongs":
        user_source[uid] = "airsongs"
        await query.answer("Source set to AirSongs")
        await query.edit_message_reply_markup(reply_markup=settings_keyboard(uid))
        return

    if data == "src_youtube":
        user_source[uid] = "youtube"
        await query.answer("Source set to YouTube")
        await query.edit_message_reply_markup(reply_markup=settings_keyboard(uid))
        return

    # pick a result → send audio
    if data.startswith("pick_"):
        key = data.replace("pick_", "")
        result = result_cache.get(key)
        if not result:
            await query.answer("❌ Session expired. Search again.", show_alert=True)
            return

        await query.answer("⏳ Preparing audio...")
        await context.bot.send_chat_action(chat_id=chat_id, action="upload_audio")

        if result["type"] == "airsongs":
            song = result["data"]
            await query.message.reply_text(f"⏳ Fetching {song.get('song')}...")
            try:
                audio, fname = airsongs_audio_bytes(song)
            except Exception as e:
                log.error(f"airsongs audio: {e}")
                audio, fname = None, None
            if not audio:
                await query.message.reply_text("❌ Audio not available.")
                return
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=io.BytesIO(audio),
                filename=fname,
                title=song.get("song", ""),
                performer=song.get("primary_artists", ""),
                duration=int(song.get("duration") or 0),
            )
        else:  # youtube
            v = result["data"]
            job = yt_request_job(v["id"], chat_id)
            if job:
                await query.message.reply_text(
                    f"✅ Download started: {v['title']}\n"
                    f"Job ID: {job}\n\n"
                    f"The audio will arrive automatically when ready."
                )
                return
            # fallback: loader.to (delivers inline)
            await query.message.reply_text(
                f"⏳ Service busy, trying fallback for {v['title']}..."
            )
            audio, fname = yt_audio_via_loader(v["id"], v["title"])
            if not audio:
                await query.message.reply_text("❌ Download failed. Try another.")
                return
            size_mb = len(audio) / 1024 / 1024
            if size_mb > 49:
                await query.message.reply_text(
                    f"❌ File too large ({size_mb:.1f}MB). Try a shorter track."
                )
                return
            await context.bot.send_audio(
                chat_id=chat_id,
                audio=io.BytesIO(audio),
                filename=fname,
                title=v["title"],
                performer=v["channel"],
                duration=int(v.get("duration") or 0),
            )
        return

    await query.answer("Unknown action.")


# ─── Main ─────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search))
    app.add_handler(CallbackQueryHandler(on_callback))
    print("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
