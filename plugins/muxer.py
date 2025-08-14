from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from helper_func.queue import Job, job_queue
from helper_func.mux   import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper       import Database as Db
from config import Config
import uuid, time, os, asyncio

db = Db()

async def _check_user(filt, client, message):
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

async def _ask_for_name(client, chat_id, mode, vid, sub, default_name):
    status = await client.send_message(
        chat_id,
        "✍️ Send the output file name <b>with extension</b> (or type <code>default</code> to keep it):\n\n"
        f"<code>{default_name}</code>",
        parse_mode=ParseMode.HTML
    )
    _PENDING_RENAME[chat_id] = dict(
        mode=mode, vid=vid, sub=sub, default_name=default_name, status_msg=status
    )

# --------------------- COMMANDS ---------------------

@Client.on_message(filters.command('softmux') & check_user & filters.private)
async def enqueue_soft(client, message):
    chat_id = message.from_user.id
    vid     = db.get_vid_filename(chat_id)
    sub     = db.get_sub_filename(chat_id)
    if not vid or not sub:
        text = ''
        if not vid: text += 'First send a Video File\n'
        if not sub: text += 'Send a Subtitle File!'
        return await client.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    final_name = db.get_filename(chat_id)
    job_id     = uuid.uuid4().hex[:8]
    status     = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, 'soft', chat_id, vid, sub, final_name, status))
    db.erase(chat_id)

@Client.on_message(filters.command('hardmux') & check_user & filters.private)
async def enqueue_hard(client, message):
    chat_id = message.from_user.id
    vid     = db.get_vid_filename(chat_id)
    sub     = db.get_sub_filename(chat_id)
    if not vid or not sub:
        text = ''
        if not vid: text += 'First send a Video File\n'
        if not sub: text += 'Send a Subtitle File!'
        return await client.send_message(chat_id, text, parse_mode=ParseMode.HTML)

    final_name = db.get_filename(chat_id)
    job_id     = uuid.uuid4().hex[:8]
    status     = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, 'hard', chat_id, vid, sub, final_name, status))
    db.erase(chat_id)

@Client.on_message(filters.command('nosub') & check_user & filters.private)
async def enqueue_nosub(client, message):
    chat_id = message.from_user.id
    vid     = db.get_vid_filename(chat_id)
    if not vid:
        return await client.send_message(chat_id, 'First send a Video File', parse_mode=ParseMode.HTML)

    final_name = db.get_filename(chat_id)
    job_id     = uuid.uuid4().hex[:8]
    status     = await client.send_message(
        chat_id,
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )

    await job_queue.put(Job(job_id, 'nosub', chat_id, vid, None, final_name, status))
    db.erase(chat_id)

@Client.on_message(filters.command('cancel') & check_user & filters.private)
async def cancel_job(client, message):
    if len(message.command) != 2:
        return await message.reply_text("Usage: /cancel <job_id>", parse_mode=ParseMode.HTML)
    target = message.command[1]

    # Remove from pending queue if not started
    removed = False
    temp_q  = asyncio.Queue()
    while not job_queue.empty():
        job = await job_queue.get()
        if job.job_id == target:
            removed = True
            await job.status_msg.edit(f"❌ Job <code>{target}</code> cancelled before start.", parse_mode=ParseMode.HTML)
        else:
            await temp_q.put(job)
        job_queue.task_done()
    while not temp_q.empty():
        await job_queue.put(await temp_q.get())

    if removed:
        return

    # If running, kill ffmpeg
    entry = running_jobs.get(target)
    if not entry:
        return await message.reply_text(f"No job `<code>{target}</code>` found.", parse_mode=ParseMode.HTML)

    entry['proc'].kill()
    for t in entry['tasks']:
        t.cancel()
    running_jobs.pop(target, None)
    await message.reply_text(f"🛑 Job `<code>{target}</code>` aborted.", parse_mode=ParseMode.HTML)

# --------------------- WORKER ---------------------

async def queue_worker(client: Client):
    while True:
        job = await job_queue.get()

        await job.status_msg.edit(
            f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…  "
            f"Use <code>/cancel {job.job_id}</code> to abort.",
            parse_mode=ParseMode.HTML
        )

        if job.mode == 'soft':
            out_file = await softmux_vid(job.vid, job.sub, msg=job.status_msg)
        elif job.mode == 'hard':
            out_file = await hardmux_vid(job.vid, job.sub, msg=job.status_msg)
        else:  # nosub
            out_file = await nosub_encode(job.vid, msg=job.status_msg)

        if out_file:
            # rename to desired final name
            src = os.path.join(Config.DOWNLOAD_DIR, out_file)
            dst = os.path.join(Config.DOWNLOAD_DIR, job.final_name)
            try:
                os.rename(src, dst)
            except Exception:
                dst = src  # fallback

            # upload with progress UI
            t0 = time.time()
            await client.send_document(
                job.chat_id,
                document=dst,
                caption=job.final_name,
                file_name=job.final_name,   # keep nice filename
                progress=progress_bar,
                progress_args=('Uploading…', job.status_msg, t0, job.job_id)
            )

            await job.status_msg.edit(f"✅ Job <code>{job.job_id}</code> done.", parse_mode=ParseMode.HTML)

            # cleanup best-effort
            for fn in (job.vid, job.sub, job.final_name):
                try:
                    if fn:
                        os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                except:
                    pass

        job_queue.task_done()
