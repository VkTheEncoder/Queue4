from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from helper_func.queue import Job, job_queue
from helper_func.mux   import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper       import Database as Db
from config import Config
import uuid, time, os, asyncio, re

db = Db()

async def _check_user(filt, client, message):
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

def _sanitize_name(name: str, default_ext: str) -> str:
    """Keep safe chars and ensure an extension."""
    name = name.strip()
    name = re.sub(r'[\\/]+', '', name)                    # strip path sep
    name = re.sub(r'[^A-Za-z0-9._ -]+', '', name).strip() # strip weird chars
    if not name:
        name = f"output{default_ext}"
    if '.' not in os.path.basename(name):
        name = name + default_ext
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[:160] + ext
    return name

async def _ask_for_name(client: Client, chat_id: int, mode: str, default_name: str) -> str:
    """
    Ask user for a final filename. Returns sanitized name (with extension).
    Timeout falls back to default_name.
    """
    prompt = await client.send_message(
        chat_id,
        (
            f"✍️ <b>Rename?</b>\n"
            f"Send the file name you want (no path).\n"
            f"Default: <code>{default_name}</code>\n\n"
            f"• Send <code>.</code> or <code>skip</code> to keep default."
        ),
        parse_mode=ParseMode.HTML
    )
    try:
        reply: Message = await client.listen(
            chat_id,
            filters=(filters.user(chat_id) & filters.text),
            timeout=90
        )
    except asyncio.TimeoutError:
        await prompt.edit("⏱️ No response. Using default name.", parse_mode=ParseMode.HTML)
        return default_name

    user_text = (reply.text or "").strip()
    if user_text.lower() in (".", "skip"):
        return default_name

    default_ext = os.path.splitext(default_name)[1] or ".mp4"
    safe = _sanitize_name(user_text, default_ext)
    await prompt.edit(f"✅ Using file name: <code>{safe}</code>", parse_mode=ParseMode.HTML)
    return safe

# ===== enqueue handlers =====

@Client.on_message(check_user & filters.command(["softmux"]) & filters.private)
async def enqueue_soft(client: Client, message: Message):
    chat_id = message.from_user.id
    if not db.check_video(chat_id) or not db.check_sub(chat_id):
        msg = []
        if not db.check_video(chat_id): msg.append("send a Video file")
        if not db.check_sub(chat_id):   msg.append("send a Subtitle file")
        return await client.send_message(chat_id, "❌ Please " + " and ".join(msg) + " first.", parse_mode=ParseMode.HTML)

    vid = db.get_vid_filename(chat_id)  # old-API getter
    sub = db.get_sub_filename(chat_id)  # old-API getter

    # sensible default (old saved original name if any)
    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_soft.mkv")
    final_name = await _ask_for_name(client, chat_id, "soft", default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, "soft", chat_id, vid, sub, final_name, status))
    db.erase(chat_id)  # allow new uploads immediately

@Client.on_message(check_user & filters.command(["hardmux"]) & filters.private)
async def enqueue_hard(client: Client, message: Message):
    chat_id = message.from_user.id
    if not db.check_video(chat_id) or not db.check_sub(chat_id):
        msg = []
        if not db.check_video(chat_id): msg.append("send a Video file")
        if not db.check_sub(chat_id):   msg.append("send a Subtitle file")
        return await client.send_message(chat_id, "❌ Please " + " and ".join(msg) + " first.", parse_mode=ParseMode.HTML)

    vid = db.get_vid_filename(chat_id)
    sub = db.get_sub_filename(chat_id)

    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_hard.mp4")
    final_name = await _ask_for_name(client, chat_id, "hard", default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, "hard", chat_id, vid, sub, final_name, status))
    db.erase(chat_id)

@Client.on_message(check_user & filters.command(["nosub"]) & filters.private)
async def enqueue_nosub(client: Client, message: Message):
    chat_id = message.from_user.id
    if not db.check_video(chat_id):
        return await client.send_message(chat_id, "❌ Please send a Video file first.", parse_mode=ParseMode.HTML)

    vid = db.get_vid_filename(chat_id)
    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_nosub.mp4")
    final_name = await _ask_for_name(client, chat_id, "nosub", default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, "nosub", chat_id, vid, None, final_name, status))
    db.erase(chat_id)

# ===== cancel handler =====

@Client.on_message(check_user & filters.command(["cancel"]) & filters.private)
async def cancel_job(client: Client, message: Message):
    if len(message.command) != 2:
        return await message.reply_text("Usage: /cancel <job_id>", parse_mode=ParseMode.HTML)
    target = message.command[1]

    # try removing from pending queue first
    removed = False
    temp_q  = asyncio.Queue()
    while not job_queue.empty():
        job = await job_queue.get()
        if job.job_id == target:
            removed = True
            try:
                await job.status_msg.edit(f"❌ Job <code>{target}</code> cancelled before start.", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        else:
            await temp_q.put(job)
        job_queue.task_done()
    while not temp_q.empty():
        await job_queue.put(await temp_q.get())

    if removed:
        return

    # If running, kill ffmpeg by job_id
    entry = running_jobs.get(target)
    if not entry:
        return await message.reply_text(f"No job `<code>{target}</code>` found.", parse_mode=ParseMode.HTML)

    try:
        entry["proc"].kill()
    except Exception:
        pass
    for t in entry.get("tasks", []):
        try:
            t.cancel()
        except Exception:
            pass
    await message.reply_text(f"🛑 Cancelled `<code>{target}</code>`.", parse_mode=ParseMode.HTML)

# ===== queue worker (kept compatible with your current helper_func/mux.py) =====

async def queue_worker(client: Client):
    while True:
        job = await job_queue.get()
        try:
            await job.status_msg.edit(
                f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…  "
                f"Use <code>/cancel {job.job_id}</code> to abort.",
                parse_mode=ParseMode.HTML
            )

            if job.mode == "soft":
                out_file = await softmux_vid(job.vid, job.sub, msg=job.status_msg)
            elif job.mode == "hard":
                out_file = await hardmux_vid(job.vid, job.sub, msg=job.status_msg)
            else:
                out_file = await nosub_encode(job.vid, msg=job.status_msg)

            if not out_file:
                await job.status_msg.edit("❌ Encode failed.", parse_mode=ParseMode.HTML)
                job_queue.task_done()
                continue

            # rename to desired final name (user-chosen)
            src = os.path.join(Config.DOWNLOAD_DIR, out_file)
            dst = os.path.join(Config.DOWNLOAD_DIR, job.final_name)
            try:
                os.replace(src, dst)
            except Exception:
                dst = src  # fallback

            # Upload with progress
            t0 = time.time()
            await client.send_document(
                job.chat_id,
                document=dst,
                caption=job.final_name,
                file_name=job.final_name,
                progress=progress_bar,
                progress_args=("Uploading…", job.status_msg, t0, job.job_id)
            )

            await job.status_msg.edit(f"✅ Job <code>{job.job_id}</code> done.", parse_mode=ParseMode.HTML)

            # best-effort cleanup
            for fn in (job.vid, job.sub, job.final_name):
                try:
                    if fn:
                        os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                except Exception:
                    pass

        except Exception as e:
            try:
                await job.status_msg.edit(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        finally:
            job_queue.task_done()
