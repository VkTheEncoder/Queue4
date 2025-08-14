# plugins/muxer.py

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from helper_func.queue import Job, job_queue
from helper_func.mux   import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper       import Database as Db
from config import Config

import uuid, time, os, asyncio

db = Db()

# allow only configured users
async def _check_user(filt, client, message):
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

# ------------------------------------------------------------------------------
# Wizard state: users can drop files first, then choose mode & name.
# ------------------------------------------------------------------------------
# { chat_id: { 'vid_id','vid_name','sub_id','sub_name','mode','awaiting_name':bool } }
PENDING = {}

def _mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎞 Hardmux", callback_data="mode:hard"),
        InlineKeyboardButton("🧩 Softmux",  callback_data="mode:soft"),
        InlineKeyboardButton("🚫 NoSub",    callback_data="mode:nosub"),
    ]])

def _is_subtitle_name(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(('.srt','.ass','.ssa','.vtt','.sub','.sbv','.smi'))

# Capture incoming video/subtitle *without* downloading yet
@Client.on_message((filters.video | filters.document) & check_user & filters.private)
async def capture_media_first(client, message):
    chat_id = message.from_user.id
    st = PENDING.setdefault(chat_id, {})

    # If it's clearly a subtitle document
    if message.document and _is_subtitle_name(message.document.file_name or ""):
        st['sub_id']   = message.document.file_id
        st['sub_name'] = message.document.file_name or f"{message.document.file_unique_id}.srt"
        return await message.reply(
            "✅ Subtitle saved.\nChoose a mode:",
            reply_markup=_mode_keyboard()
        )

    # Otherwise treat as video (video or generic document)
    if message.video:
        st['vid_id']   = message.video.file_id
        st['vid_name'] = message.video.file_name or f"{message.video.file_unique_id}.mp4"
    else:
        st['vid_id']   = message.document.file_id
        st['vid_name'] = message.document.file_name or f"{message.document.file_unique_id}.mp4"

    await message.reply("✅ Video saved.\nChoose a mode:", reply_markup=_mode_keyboard())

# Mode selection
@Client.on_callback_query(filters.regex(r"^mode:(hard|soft|nosub)$") & check_user)
async def choose_mode(client, cq):
    chat_id = cq.from_user.id
    st = PENDING.setdefault(chat_id, {})
    mode = cq.data.split(":")[1]
    st['mode'] = mode

    # If hard/soft and no subtitle yet, ask for it
    if mode in ('hard','soft') and not st.get('sub_id'):
        return await cq.message.edit_text(
            f"Mode set: <b>{'Hardmux' if mode=='hard' else 'Softmux'}</b>\n"
            "Now send a subtitle file (*.srt, *.ass, *.vtt).",
            parse_mode=ParseMode.HTML
        )

    # Otherwise ask for name
    st['awaiting_name'] = True
    await cq.message.edit_text(
        f"Mode set: <b>{'Hardmux' if mode=='hard' else 'Softmux' if mode=='soft' else 'NoSub'}</b>\n"
        "Send the <b>output file name with extension</b> or type <code>default</code>.",
        parse_mode=ParseMode.HTML
    )

# Receive the output name, then download & enqueue
@Client.on_message(filters.text & check_user & filters.private)
async def receive_output_name(client, message):
    chat_id = message.from_user.id
    st = PENDING.get(chat_id)
    if not st or not st.get('awaiting_name'):
        return  # ignore unrelated texts

    # Validate inputs present
    if not st.get('vid_id'):
        return await message.reply("Please send a video first.")
    if st.get('mode') in ('hard','soft') and not st.get('sub_id'):
        return await message.reply("Please send a subtitle file for the selected mode.")

    # Resolve final output name
    txt = message.text.strip()
    if txt.lower() == "default":
        base, _ = os.path.splitext(st['vid_name'])
        suffix = {'hard': '_hard.mp4', 'soft': '_soft.mkv', 'nosub': '_enc.mp4'}[st['mode']]
        final_name = base + suffix
    else:
        final_name = txt

    # Status message
    status = await message.reply("📥 Preparing downloads…")

    # Download video (AFTER we collected instructions)
    t0 = time.time()
    v_local = await client.download_media(
        st['vid_id'],
        file_name=os.path.join(Config.DOWNLOAD_DIR, st['vid_name']),
        progress=progress_bar,
        progress_args=("Downloading File", status, t0, "DLVID")
    )

    # Download subtitle if needed
    s_local = None
    if st['mode'] in ('hard','soft'):
        t1 = time.time()
        s_local = await client.download_media(
            st['sub_id'],
            file_name=os.path.join(Config.DOWNLOAD_DIR, st['sub_name']),
            progress=progress_bar,
            progress_args=("Downloading Subtitle", status, t1, "DLSUB")
        )

    # Enqueue job
    job_id = uuid.uuid4().hex[:8]
    await status.edit(
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )
    await job_queue.put(Job(
        job_id,
        st['mode'],
        chat_id,
        os.path.basename(v_local),
        os.path.basename(s_local) if s_local else None,
        final_name,
        status
    ))

    # Clear wizard state
    PENDING.pop(chat_id, None)

# (Optional) command to clear current wizard state
@Client.on_message(filters.command('reset') & check_user & filters.private)
async def reset_pending(client, message):
    PENDING.pop(message.from_user.id, None)
    await message.reply("🧹 Cleared pending setup. Send a video/subtitle again to start.")

# ------------------------------------------------------------------------------
# Legacy COMMAND workflows (kept for power users)
# ------------------------------------------------------------------------------

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

# Cancel a job (queued or running)
@Client.on_message(filters.command('cancel') & check_user & filters.private)
async def cancel_job(client, message):
    if len(message.command) != 2:
        return await message.reply_text("Usage: /cancel <job_id>", parse_mode=ParseMode.HTML)
    target = message.command[1]

    # Remove from pending queue first
    removed = False
    temp_q  = asyncio.Queue()
    while not job_queue.empty():
        job = await job_queue.get()
        if job.job_id == target:
            removed = True
            await job.status_msg.edit(
                f"❌ Job <code>{target}</code> cancelled before start.", parse_mode=ParseMode.HTML
            )
        else:
            await temp_q.put(job)
        job_queue.task_done()
    while not temp_q.empty():
        await job_queue.put(await temp_q.get())

    if removed:
        return

    # If already running, kill ffmpeg
    entry = running_jobs.get(target)
    if not entry:
        return await message.reply_text(
            f"No job `<code>{target}</code>` found.", parse_mode=ParseMode.HTML
        )

    entry['proc'].kill()
    for t in entry['tasks']:
        t.cancel()
    running_jobs.pop(target, None)
    await message.reply_text(
        f"🛑 Job `<code>{target}</code>` aborted.", parse_mode=ParseMode.HTML
    )

# ------------------------------------------------------------------------------
# Worker (unchanged)
# ------------------------------------------------------------------------------

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

            # upload as DOCUMENT with progress
            t0 = time.time()
            await client.send_document(
                job.chat_id,
                document=dst,
                caption=job.final_name,
                file_name=job.final_name,
                progress=progress_bar,
                progress_args=('Uploading…', job.status_msg, t0, job.job_id)
            )

            await job.status_msg.edit(
                f"✅ Job <code>{job.job_id}</code> done.", parse_mode=ParseMode.HTML
            )

            # cleanup
            for fn in (job.vid, job.sub, job.final_name):
                try:
                    if fn:
                        os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                except:
                    pass

        job_queue.task_done()
