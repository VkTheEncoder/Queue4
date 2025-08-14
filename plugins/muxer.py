# plugins/muxer.py
from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from helper_func.queue import Job, job_queue
from helper_func.mux import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper import Database as Db
from config import Config
import uuid
import time
import os
import asyncio
import re

db = Db()

# ---------------------------
# Access control
# ---------------------------
async def _check_user(filt, client, message):
    try:
        return str(message.from_user.id) in Config.ALLOWED_USERS
    except Exception:
        return False

check_user = filters.create(_check_user)

# ---------------------------
# Rename prompt (reply-based)
# ---------------------------
# We can't use client.listen here, so we use a "pending rename" map.
# When we prompt, we store {chat_id: {...}} and wait on an asyncio.Future.
# The text handler below fulfills the future when the user replies.

PENDING_RENAME = {}  # chat_id -> {"future": asyncio.Future, "prompt_id": int, "default": str}

def _sanitize_name(name: str, default_ext: str) -> str:
    name = (name or "").strip()
    name = re.sub(r'[\\/]+', '', name)                    # strip path separators
    name = re.sub(r'[^A-Za-z0-9._ -]+', '', name).strip() # keep letters, numbers, dot, dash, underscore, space
    if not name:
        name = f"output{default_ext}"
    if '.' not in os.path.basename(name):
        name = name + default_ext
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[:160] + ext
    return name

async def _ask_for_name(app: Client, chat_id: int, default_name: str, timeout: int = 90) -> str:
    # Ask user and wait for a reply to this prompt
    prompt = await app.send_message(
        chat_id,
        (
            "✍️ <b>Rename?</b>\n"
            "Reply to <i>this message</i> with the file name you want (no path).\n"
            f"Default: <code>{default_name}</code>\n\n"
            "• Send <code>.</code> or <code>skip</code> to keep default."
        ),
        parse_mode=ParseMode.HTML
    )

    # ensure only one pending per chat
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
        # timeout → use default
        try:
            await prompt.edit("⏱️ No response. Using default name.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
        PENDING_RENAME.pop(chat_id, None)
        return default_name
    finally:
        # we won't pop here; the handler pops when fulfilling
        pass

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

@Client.on_message(check_user & filters.text & filters.private)
async def _capture_rename_response(client: Client, message: Message):
    chat_id = message.chat.id
    entry = PENDING_RENAME.get(chat_id)
    if not entry:
        return
    # must be a reply to our prompt
    if not message.reply_to_message or message.reply_to_message.id != entry.get("prompt_id"):
        return
    fut: asyncio.Future = entry.get("future")
    if fut and not fut.done():
        fut.set_result(message.text.strip() if message.text else "")
    # cleanup
    PENDING_RENAME.pop(chat_id, None)

# ---------------------------
# Command handlers
# ---------------------------

@Client.on_message(check_user & filters.command(["softmux"]) & filters.private)
async def enqueue_soft(client: Client, message: Message):
    chat_id = message.from_user.id

    if not db.check_video(chat_id) or not db.check_sub(chat_id):
        msg = []
        if not db.check_video(chat_id): msg.append("send a Video file")
        if not db.check_sub(chat_id):   msg.append("send a Subtitle file")
        return await client.send_message(chat_id, "❌ Please " + " and ".join(msg) + " first.", parse_mode=ParseMode.HTML)

    # Old repo getters (your dbhelper also has shims now)
    vid = db.get_vid_filename(chat_id)
    sub = db.get_sub_filename(chat_id)

    default_final = db.get_filename(chat_id) or (os.path.splitext(vid)[0] + "_soft.mkv")
    final_name = await _ask_for_name(client, chat_id, default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, "soft", chat_id, vid, sub, final_name, status))
    # allow fresh uploads asap (matches your old flow)
    db.erase(chat_id)

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
    final_name = await _ask_for_name(client, chat_id, default_final)

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
    final_name = await _ask_for_name(client, chat_id, default_final)

    job_id = uuid.uuid4().hex[:8]
    status = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, "nosub", chat_id, vid, None, final_name, status))
    db.erase(chat_id)

# ---------------------------
# Cancel handler
# ---------------------------

@Client.on_message(check_user & filters.command(["cancel"]) & filters.private)
async def cancel_current(client: Client, message: Message):
    """
    Cancels the *currently running* job for this chat (consistent with your old code).
    """
    chat_id = message.chat.id
    entry = running_jobs.get(chat_id)  # old helper_func.mux maps by chat_id
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

# ---------------------------
# Queue worker
# ---------------------------

async def queue_worker(client: Client):
    """
    Pulls Job objects from helper_func.queue.job_queue and runs them.
    Job = (job_id, mode, chat_id, vid, sub, final_name, status_msg)
    """
    while True:
        job: Job = await job_queue.get()
        try:
            await job.status_msg.edit(
                f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…",
                parse_mode=ParseMode.HTML
            )

            # Run encode/mux per mode; helper_func.mux returns the output BASENAME
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

            # Physically rename to user-specified final name
            try:
                if os.path.abspath(src) != os.path.abspath(dst):
                    os.replace(src, dst)
            except Exception:
                # If rename fails, just use the original file
                dst = src

            # Upload with progress wrapper compatible with your progress_bar
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

            # Best-effort cleanup: original inputs + final output
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
