from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import Message
from helper_func.queue import Job, job_queue
from helper_func.mux   import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper import Database as Db
from config import Config
import uuid, time, os, asyncio, re

db = Db()

async def _check_user(filt, client, message):
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

def _sanitize_name(name: str, default_ext: str) -> str:
    """
    Keep letters, numbers, space, dot, dash, underscore.
    Ensure it has an extension (uses default_ext if missing).
    Enforce sane length.
    """
    name = name.strip()
    # remove path separators and weird chars
    name = re.sub(r'[\\/]+', '', name)
    name = re.sub(r'[^A-Za-z0-9._ -]+', '', name).strip()
    # avoid empty
    if not name:
        name = f"output{default_ext}"
    # add default extension if missing
    if '.' not in os.path.basename(name):
        name = name + default_ext
    # overly long filenames can be rejected by FS or Telegram
    if len(name) > 180:
        root, ext = os.path.splitext(name)
        name = root[:160] + ext
    return name

async def _ask_for_name(client: Client, chat_id: int, mode: str, default_name: str) -> str:
    """
    Ask the user for the upload file name.
    Returns a safe final name (always includes extension).
    If timeout/no input, returns default_name.
    """
    prompt = await client.send_message(
        chat_id,
        (
            f"✍️ <b>Rename?</b>\n"
            f"Send the file name you want (without path).<br>"
            f"Default: <code>{default_name}</code><br><br>"
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

    text = reply.text.strip()
    if text in (".", "skip", "SKIP", "Skip"):
        return default_name

    default_ext = os.path.splitext(default_name)[1] or ".mp4"
    safe_name = _sanitize_name(text, default_ext)
    await prompt.edit(f"✅ Using file name: <code>{safe_name}</code>", parse_mode=ParseMode.HTML)
    return safe_name

@Client.on_message(check_user & filters.command(["softmux", "hardmux", "nosub"]))
async def enqueue_job(client: Client, message: Message):
    """
    Enqueue a job. We grab the latest video/sub from DB and push to queue.
    """
    user_id = message.from_user.id
    chat_id = message.chat.id

    has_video = db.check_video(chat_id)
    has_sub   = db.check_sub(chat_id)

    mode = message.command[0].lower()  # "softmux" | "hardmux" | "nosub"

    if mode in ("softmux", "hardmux") and (not has_video or not has_sub):
        return await message.reply("❌ Need both a video and a subtitle file first.")
    if mode == "nosub" and not has_video:
        return await message.reply("❌ Need a video file first.")

    vid = db.get_video(chat_id)
    sub = db.get_sub(chat_id) if mode in ("softmux", "hardmux") else None

    # pick a default output filename
    base_default = os.path.splitext(vid)[0] + (".mp4" if mode != "softmux" else ".mkv")
    status_msg = await message.reply(
        f"🧾 Queued <code>{mode}</code> job...\n"
        f"Video: <code>{vid}</code>\n"
        + (f"Subs: <code>{sub}</code>\n" if sub else "")
        + "You'll be asked for a final file name before upload.",
        parse_mode=ParseMode.HTML
    )

    job = Job(
        job_id=str(uuid.uuid4())[:8],
        mode=mode,
        chat_id=chat_id,
        vid=vid,
        sub=sub,
        final_name=base_default,
        status_msg=status_msg
    )
    await job_queue.put(job)

async def _do_upload(client: Client, chat_id: int, path: str, file_name: str, status_msg: Message, t0: float, job_id: str):
    size_total = os.path.getsize(path)

    async def _progress(current, total):
        await progress_bar(current, total, 'Uploading…', status_msg, t0, job_id)

    await client.send_document(
        chat_id=chat_id,
        document=path,
        file_name=file_name,
        caption=f"✅ <b>Done:</b> <code>{file_name}</code>",
        parse_mode=ParseMode.HTML,
        progress=_progress
    )

@Client.on_message(check_user & filters.command(["cancel"]))
async def cancel_current(client: Client, message: Message):
    chat_id = message.chat.id
    job = running_jobs.get(chat_id)
    if not job:
        return await message.reply("ℹ️ Nothing to cancel.")
    proc = job.get("proc")
    if proc and proc.returncode is None:
        try:
            proc.terminate()
        except Exception:
            pass
    running_jobs.pop(chat_id, None)
    await message.reply("🛑 Canceled current encode.")

async def queue_worker(app: Client):
    """
    Background worker: take jobs from the queue, run them, prompt for rename, upload, clean.
    """
    while True:
        job: Job = await job_queue.get()
        t0 = time.time()
        try:
            await job.status_msg.edit(
                f"🚀 Starting <code>{job.mode}</code> (<code>{job.job_id}</code>)…",
                parse_mode=ParseMode.HTML
            )

            # Run the encode/mux
            if job.mode == "softmux":
                out_path = await softmux_vid(app, job.chat_id, job.vid, job.sub, job.status_msg, job.job_id)
            elif job.mode == "hardmux":
                out_path = await hardmux_vid(app, job.chat_id, job.vid, job.sub, job.status_msg, job.job_id)
            else:
                out_path = await nosub_encode(app, job.chat_id, job.vid, job.status_msg, job.job_id)

            if not out_path or not os.path.exists(out_path):
                await job.status_msg.edit("❌ Encode failed.", parse_mode=ParseMode.HTML)
                job_queue.task_done()
                continue

            default_name = os.path.basename(out_path)
            # Ask user for final name
            final_name = await _ask_for_name(app, job.chat_id, job.mode, default_name)
            final_path = os.path.join(Config.DOWNLOAD_DIR, final_name)

            # Physically rename if changed
            if final_name != default_name:
                try:
                    os.replace(out_path, final_path)
                except Exception as e:
                    # If rename fails, fallback but tell the user
                    await job.status_msg.edit(
                        f"⚠️ Could not rename file ({e}). Using default: <code>{default_name}</code>.",
                        parse_mode=ParseMode.HTML
                    )
                    final_name = default_name
                    final_path = out_path
            else:
                final_path = out_path

            # Upload
            await _do_upload(app, job.chat_id, final_path, final_name, job.status_msg, t0, job.job_id)

            await job.status_msg.edit(
                f"✅ Job <code>{job.job_id}</code> done.",
                parse_mode=ParseMode.HTML
            )

            # Cleanup best effort
            for fn in (job.vid, job.sub):
                try:
                    if fn:
                        os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                except Exception:
                    pass
            try:
                os.remove(final_path)
            except Exception:
                pass

        except Exception as e:
            try:
                await job.status_msg.edit(f"❌ Error: <code>{e}</code>", parse_mode=ParseMode.HTML)
            except Exception:
                pass
        finally:
            job_queue.task_done()
