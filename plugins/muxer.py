# plugins/muxer.py
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from helper_func.queue import Job, job_queue
from helper_func.mux import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper import Database as Db
from config import Config
import uuid, time, os, asyncio, re, logging

log = logging.getLogger("muxer")
db = Db()

# Accept both "/" and "!" unless overridden in Config
PREFIXES = getattr(Config, "COMMAND_PREFIXES", ["/", "!"])

# ---------- auth (checked inside handlers so filters don’t block dispatch) ----------
def _is_authorized(message: Message) -> bool:
    allowed = getattr(Config, "ALLOWED_USERS", None)
    if not allowed or allowed == "*" or allowed == ["*"]:
        return True
    try:
        uid = message.from_user.id if message.from_user else None
        allowed_str = {str(x) for x in allowed}
        return uid is not None and (str(uid) in allowed_str or uid in allowed)
    except Exception:
        return False

async def _ensure_auth(message: Message) -> bool:
    if _is_authorized(message):
        return True
    await message.reply_text("❌ You’re not allowed to use this bot.")
    return False

# ---------- rename prompt (reply-based; safe on v1/v2) ----------
PENDING_RENAME = {}  # chat_id -> {"future": asyncio.Future, "prompt_id": int, "default": str}

def _sanitize_name(name: str, default_ext: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[\\/]+', '', name)
    name = re.sub(r'[^A-Za-z0-9._ -]+', '', name).strip()
    if not name:
        name = f"output{default_ext}"
    if '.' not in os.path.basename(name):
        name = name + default_ext
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[:160] + ext
    return name

async def _ask_for_name(app: Client, chat_id: int, default_name: str, timeout: int = 90) -> str:
    prompt = await app.send_message(
        chat_id,
        ("✍️ <b>Rename?</b>\n"
         "Reply to <i>this message</i> with the file name you want (no path).\n"
         f"Default: <code>{default_name}</code>\n\n"
         "• Send <code>.</code> or <code>skip</code> to keep default."),
        parse_mode=ParseMode.HTML
    )

    # only one pending per chat
    old = PENDING_RENAME.get(chat_id)
    if old:
        fut = old.get("future")
        if fut and not fut.done():
            fut.set_result(None)
        PENDING_RENAME.pop(chat_id, None)

    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    PENDING_RENAME[chat_id] = {"future": fut, "prompt_id": prompt.id, "default": default_name}

    try:
        user_text = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        try:
            await prompt.edit("⏱️ No response. Using default name.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        PENDING_RENAME.pop(chat_id, None)
        return default_name

    if not user_text or user_text.lower() in (".", "skip"):
        try:
            await prompt.edit(f"✅ Using default: <code>{default_name}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        return default_name

    default_ext = os.path.splitext(default_name)[1] or ".mp4"
    safe = _sanitize_name(user_text, default_ext)
    try:
        await prompt.edit(f"✅ Using file name: <code>{safe}</code>", parse_mode=ParseMode.HTML)
    except Exception:
        pass
    return safe

@Client.on_message(filters.text)
async def _capture_rename_response(client: Client, message: Message):
    # capture only if it’s a reply to our prompt (works in private or group)
    entry = PENDING_RENAME.get(message.chat.id)
    if not entry:
        return
    if not message.reply_to_message or message.reply_to_message.id != entry.get("prompt_id"):
        return
    fut: asyncio.Future = entry.get("future")
    if fut and not fut.done():
        fut.set_result(message.text.strip() if message.text else "")
    PENDING_RENAME.pop(message.chat.id, None)

# ---------- quick health check ----------
@Client.on_message(filters.command(["ping"], prefixes=PREFIXES))
async def ping(client: Client, message: Message):
    if not await _ensure_auth(message): return
    await message.reply_text("pong ✅")

# ---------- command handlers (no filters.private; will work anywhere) ----------
@Client.on_message(filters.command(["softmux"], prefixes=PREFIXES))
async def enqueue_soft(client: Client, message: Message):
    if not await _ensure_auth(message): return
    chat_id = message.from_user.id if message.from_user else message.chat.id

    if not db.check_video(chat_id) or not db.check_sub(chat_id):
        msg = []
        if not db.check_video(chat_id): msg.append("send a Video file")
        if not db.check_sub(chat_id):   msg.append("send a Subtitle file")
        return await client.send_message(chat_id, "❌ Please " + " and ".join(msg) + " first.", parse_mode=ParseMode.HTML)

    vid = db.get_vid_filename(chat_id)
    sub = db.get_sub_filename(chat_id)

    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_soft.mkv")
    final_name = await _ask_for_name(client, chat_id, default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id, f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )
    await job_queue.put(Job(job_id, "soft", chat_id, vid, sub, final_name, status))
    db.erase(chat_id)

@Client.on_message(filters.command(["hardmux"], prefixes=PREFIXES))
async def enqueue_hard(client: Client, message: Message):
    if not await _ensure_auth(message): return
    chat_id = message.from_user.id if message.from_user else message.chat.id

    if not db.check_video(chat_id) or not db.check_sub(chat_id):
        msg = []
        if not db.check_video(chat_id): msg.append("send a Video file")
        if not db.check_sub(chat_id):   msg.append("send a Subtitle file")
        return await client.send_message(chat_id, "❌ Please " + " and ".join(msg) + " first.", parse_mode=ParseMode.HTML)

    vid = db.get_vid_filename(chat_id)
    sub = db.get_sub_filename(chat_id)

    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_hard.mp4")
    final_name = await _ask_for_name(client, chat_id, default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id, f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )
    await job_queue.put(Job(job_id, "hard", chat_id, vid, sub, final_name, status))
    db.erase(chat_id)

@Client.on_message(filters.command(["nosub"], prefixes=PREFIXES))
async def enqueue_nosub(client: Client, message: Message):
    if not await _ensure_auth(message): return
    chat_id = message.from_user.id if message.from_user else message.chat.id

    if not db.check_video(chat_id):
        return await client.send_message(chat_id, "❌ Please send a Video file first.", parse_mode=ParseMode.HTML)

    vid = db.get_vid_filename(chat_id)
    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_nosub.mp4")
    final_name = await _ask_for_name(client, chat_id, default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id, f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )
    await job_queue.put(Job(job_id, "nosub", chat_id, vid, None, final_name, status))
    db.erase(chat_id)

# ---------- cancel current ----------
@Client.on_message(filters.command(["cancel"], prefixes=PREFIXES))
async def cancel_current(client: Client, message: Message):
    if not await _ensure_auth(message): return
    chat_id = message.chat.id
    entry = running_jobs.get(chat_id)
    if not entry:
        return await message.reply_text("ℹ️ Nothing to cancel.")
    proc = entry.get("proc")
    if proc and (getattr(proc, "returncode", None) is None):
        try:
            proc.terminate()
        except Exception:
            pass
    running_jobs.pop(chat_id, None)
    await message.reply_text("🛑 Canceled current encode.")

# ---------- queue worker ----------
async def queue_worker(client: Client):
    while True:
        job: Job = await job_queue.get()
        try:
            await job.status_msg.edit(
                f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…",
                parse_mode=ParseMode.HTML
            )
            if job.mode == "soft":
                out_basename = await softmux_vid(job.vid, job.sub, msg=job.status_msg)
            elif job.mode == "hard":
                out_basename = await hardmux_vid(job.vid, job.sub, msg=job.status_msg)
            else:
                out_basename = await nosub_encode(job.vid, msg=job.status_msg)

            if not out_basename:
                await job.status_msg.edit("❌ Encode failed.", parse_mode=ParseMode.HTML)
                job_queue.task_done()
                continue

            src = os.path.join(Config.DOWNLOAD_DIR, out_basename)
            dst = os.path.join(Config.DOWNLOAD_DIR, job.final_name)
            try:
                if os.path.abspath(src) != os.path.abspath(dst):
                    os.replace(src, dst)
            except Exception:
                dst = src

            t0 = time.time()
            async def _progress(current, total):
                await progress_bar(current, total, 'Uploading…', job.status_msg, t0, job.job_id)

            await client.send_document(
                chat_id=job.chat_id,
                document=dst,
                caption=job.final_name,
                file_name=job.final_name,
                progress=_progress
            )

            await job.status_msg.edit(f"✅ Job <code>{job.job_id}</code> done.", parse_mode=ParseMode.HTML)

            for fn in (job.vid, job.sub, job.final_name):
                try:
                    if fn:
                        path = os.path.join(Config.DOWNLOAD_DIR, fn)
                        if os.path.exists(path):
                            os.remove(path)
                except Exception:
                    pass

        except Exception as e:
            try:
                await job.status_msg.edit(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        finally:
            job_queue.task_done()
