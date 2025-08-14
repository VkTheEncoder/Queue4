# plugins/muxer.py

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from helper_func.queue import Job, job_queue
from helper_func.mux   import softmux_vid, hardmux_vid, nosub_encode, running_jobs
from helper_func.progress_bar import progress_bar
from helper_func.dbhelper       import Database as Db
from helper_func.settings_manager import SettingsManager
from config import Config

import uuid, time, os, asyncio

db = Db()

# allow only configured users
async def _check_user(filt, client, message):
    return str(message.from_user.id) in Config.ALLOWED_USERS
check_user = filters.create(_check_user)

# ------------------------------------------------------------------------------
# Wizard state (supports groups): key = (chat_id, user_id)
# ------------------------------------------------------------------------------
PENDING = {}   # {(chat_id, user_id): {'vid_id','vid_name','vid_size','sub_id','sub_name','sub_size','mode','awaiting_name':bool}}

def _mode_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎞 Hardmux", callback_data="mode:hard"),
        InlineKeyboardButton("🧩 Softmux",  callback_data="mode:soft"),
        InlineKeyboardButton("🚫 NoSub",    callback_data="mode:nosub"),
    ]])

def _is_subtitle_name(name: str) -> bool:
    n = (name or "").lower()
    return n.endswith(('.srt','.ass','.ssa','.vtt','.sub','.sbv','.smi'))

def _tg_token(file_id: str, file_name: str, file_size: int | None) -> str:
    safe_name = os.path.basename(file_name) if file_name else "file.bin"
    size_str  = str(file_size or 0)
    return f"tg:{file_id}::{safe_name}::{size_str}"

def _parse_tg_token(token: str):
    if not token or not token.startswith("tg:"):
        return None, None, 0
    try:
        _, rest = token.split("tg:", 1)
        file_id, file_name, size_str = rest.split("::", 2)
        return file_id, os.path.basename(file_name), int(size_str or 0)
    except Exception:
        return None, None, 0

# ------------------------------------------------------------------------------
# SETTINGS UI
# ------------------------------------------------------------------------------
@Client.on_message(filters.command('settings') & check_user & (filters.private | filters.group))
async def show_settings(client, message):
    chat_id = message.chat.id
    cfg = SettingsManager.get(chat_id) or {}
    text, kb = _render_settings(cfg)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=kb)

@Client.on_callback_query(filters.regex(r"^cycle:(res|fps|codec|crf|preset)$") & check_user)
async def cycle_setting(client, cq):
    chat_id = cq.message.chat.id
    cfg = SettingsManager.get(chat_id) or {}

    field = cq.data.split(":", 1)[1]
    lists = {
        'res':   ['original','1280:720','1920:1080','2560:1440','3840:2160'],
        'fps':   ['original','24','30','60'],
        'codec': ['libx264','libx265'],
        'crf':   ['18','20','23','27','30'],
        'preset':['veryslow','slower','slow','medium','fast','faster']
    }
    keymap = {'res':'resolution','fps':'fps','codec':'codec','crf':'crf','preset':'preset'}
    key = keymap[field]

    cur = str(cfg.get(key, lists[field][0]))
    vals = lists[field]
    try:
        nxt = vals[(vals.index(cur) + 1) % len(vals)]
    except ValueError:
        nxt = vals[0]

    SettingsManager.set(chat_id, key, nxt)
    cfg[key] = nxt

    text, kb = _render_settings(cfg)
    await cq.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

def _render_settings(cfg: dict):
    res    = cfg.get('resolution','original')
    fps    = cfg.get('fps','original')
    codec  = cfg.get('codec','libx264')
    crf    = cfg.get('crf','27')
    preset = cfg.get('preset','faster')

    text = (
        "⚙️ <b>Encoding Settings</b>\n"
        f"• Resolution: <code>{res}</code>\n"
        f"• FPS       : <code>{fps}</code>\n"
        f"• Codec     : <code>{codec}</code>\n"
        f"• CRF       : <code>{crf}</code>\n"
        f"• Preset    : <code>{preset}</code>\n\n"
        "Tap a button to change:"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"Resolution ({res})", callback_data="cycle:res")],
        [InlineKeyboardButton(f"FPS ({fps})",        callback_data="cycle:fps")],
        [InlineKeyboardButton(f"Codec ({codec})",    callback_data="cycle:codec")],
        [InlineKeyboardButton(f"CRF ({crf})",        callback_data="cycle:crf")],
        [InlineKeyboardButton(f"Preset ({preset})",  callback_data="cycle:preset")],
    ])
    return text, kb

# ------------------------------------------------------------------------------
# WIZARD: capture media first (no download yet)
# ------------------------------------------------------------------------------
@Client.on_message((filters.video | filters.document) & check_user & (filters.private | filters.group))
async def capture_media_first(client, message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    st = PENDING.setdefault((chat_id, user_id), {})

    # Subtitle?
    if message.document and _is_subtitle_name(message.document.file_name or ""):
        st['sub_id']   = message.document.file_id
        st['sub_name'] = message.document.file_name or f"{message.document.file_unique_id}.srt"
        st['sub_size'] = getattr(message.document, "file_size", 0) or 0
        return await message.reply(
            "✅ Subtitle saved.\nChoose a mode:", reply_markup=_mode_keyboard()
        )

    # Video (video or generic doc)
    if message.video:
        st['vid_id']   = message.video.file_id
        st['vid_name'] = message.video.file_name or f"{message.video.file_unique_id}.mp4"
        st['vid_size'] = getattr(message.video, "file_size", 0) or 0
    else:
        st['vid_id']   = message.document.file_id
        st['vid_name'] = message.document.file_name or f"{message.document.file_unique_id}.mp4"
        st['vid_size'] = getattr(message.document, "file_size", 0) or 0

    await message.reply("✅ Video saved.\nChoose a mode:", reply_markup=_mode_keyboard())

@Client.on_callback_query(filters.regex(r"^mode:(hard|soft|nosub)$") & check_user)
async def choose_mode(client, cq):
    chat_id = cq.message.chat.id
    user_id = cq.from_user.id
    st = PENDING.setdefault((chat_id, user_id), {})
    mode = cq.data.split(":")[1]
    st['mode'] = mode

    if mode in ('hard','soft') and not st.get('sub_id'):
        return await cq.message.edit_text(
            f"Mode set: <b>{'Hardmux' if mode=='hard' else 'Softmux'}</b>\n"
            "Now send a subtitle file (*.srt, *.ass, *.vtt).",
            parse_mode=ParseMode.HTML
        )

    st['awaiting_name'] = True
    await cq.message.edit_text(
        f"Mode set: <b>{'Hardmux' if mode=='hard' else 'Softmux' if mode=='soft' else 'NoSub'}</b>\n"
        "Send the <b>output file name with extension</b> or type <code>default</code>.",
        parse_mode=ParseMode.HTML
    )

@Client.on_message(filters.text & check_user & (filters.private | filters.group))
async def receive_output_name(client, message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    st = PENDING.get((chat_id, user_id))
    if not st or not st.get('awaiting_name'):
        return

    if not st.get('vid_id'):
        return await message.reply("Please send a video first.")
    if st.get('mode') in ('hard','soft') and not st.get('sub_id'):
        return await message.reply("Please send a subtitle file for the selected mode.")

    txt = message.text.strip()
    if txt.lower() == "default":
        base, _ = os.path.splitext(st['vid_name'])
        suffix = {'hard': '_hard.mp4', 'soft': '_soft.mkv', 'nosub': '_enc.mp4'}[st['mode']]
        final_name = base + suffix
    else:
        final_name = os.path.basename(txt)

    status = await message.reply("⏳ Queuing…")

    # include file sizes to fix 0B in progress
    vid_token = _tg_token(st['vid_id'], st['vid_name'], st.get('vid_size', 0))
    sub_token = _tg_token(st['sub_id'], st['sub_name'], st.get('sub_size', 0)) if st['mode'] in ('hard','soft') else None

    job_id = uuid.uuid4().hex[:8]
    await status.edit(
        f"🧾 Job <code>{job_id}</code> enqueued at position {job_queue.qsize() + 1}",
        parse_mode=ParseMode.HTML
    )
    await job_queue.put(Job(
        job_id,
        st['mode'],
        chat_id,
        vid_token,
        sub_token,
        final_name,
        status
    ))

    PENDING.pop((chat_id, user_id), None)

@Client.on_message(filters.command('reset') & check_user & (filters.private | filters.group))
async def reset_pending(client, message):
    key = (message.chat.id, message.from_user.id)
    PENDING.pop(key, None)
    await message.reply("🧹 Cleared pending setup. Send a video/subtitle again to start.")

# ------------------------------------------------------------------------------
# Legacy COMMAND workflows (kept for power users)
# ------------------------------------------------------------------------------

@Client.on_message(filters.command('softmux') & check_user & (filters.private | filters.group))
async def enqueue_soft(client, message):
    chat_id = message.chat.id
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

@Client.on_message(filters.command('hardmux') & check_user & (filters.private | filters.group))
async def enqueue_hard(client, message):
    chat_id = message.chat.id
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

@Client.on_message(filters.command('nosub') & check_user & (filters.private | filters.group))
async def enqueue_nosub(client, message):
    chat_id = message.chat.id
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

# ------------------------------------------------------------------------------
# CANCEL (works during both download and encode)
# ------------------------------------------------------------------------------
@Client.on_message(filters.command('cancel') & check_user & (filters.private | filters.group))
async def cancel_job(client, message):
    if len(message.command) != 2:
        return await message.reply_text("Usage: /cancel <job_id>", parse_mode=ParseMode.HTML)
    target = message.command[1].strip()

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

    # If running (download or encode), stop it
    entry = running_jobs.get(target)
    if not entry:
        return await message.reply_text(
            f"No running job `<code>{target}</code>` found.", parse_mode=ParseMode.HTML
        )

    # Cancel download task if present
    dl_task = entry.get('dl_task')
    if dl_task:
        try:
            dl_task.cancel()
        except:
            pass
        # clean any partial file
        path = entry.get('dl_path')
        if path:
            for candidate in (path, path + ".temp"):
                try:
                    if os.path.exists(candidate):
                        os.remove(candidate)
                except:
                    pass

    # Kill ffmpeg if present
    proc = entry.get('proc')
    if proc:
        try:
            proc.kill()
        except:
            pass
        for t in entry.get('tasks', []):
            try:
                t.cancel()
            except:
                pass

    await message.reply_text(
        f"🛑 Job `<code>{target}</code>` aborted.", parse_mode=ParseMode.HTML
    )

# ------------------------------------------------------------------------------
# Worker — downloads INSIDE the queue, one job at a time
# ------------------------------------------------------------------------------

async def queue_worker(client: Client):
    os.makedirs(Config.DOWNLOAD_DIR, exist_ok=True)

    while True:
        job = await job_queue.get()

        await job.status_msg.edit(
            f"▶️ Starting <code>{job.job_id}</code> ({job.mode})…  "
            f"Use <code>/cancel {job.job_id}</code> to abort.",
            parse_mode=ParseMode.HTML
        )

        def _parse(token):
            if token and token.startswith("tg:"):
                return _parse_tg_token(token)
            return None, None, 0

        local_vid = job.vid
        local_sub = job.sub

        # -------------------- Video download (cancel-aware) --------------------
        vid_id, vid_name, vid_size = _parse(job.vid)
        if vid_id:
            target_path = os.path.join(Config.DOWNLOAD_DIR, os.path.basename(vid_name or "video.mp4"))
            # Register this job as "downloading"
            dl_task = asyncio.create_task(
                client.download_media(
                    vid_id,
                    file_name=target_path,
                    progress=progress_bar,
                    progress_args=("Downloading File", job.status_msg, time.time(), job.job_id, vid_size)
                )
            )
            running_jobs[job.job_id] = {'dl_task': dl_task, 'dl_path': target_path}
            try:
                local_vid = await dl_task
                local_vid = os.path.basename(local_vid)
            except asyncio.CancelledError:
                # Cancelled by /cancel
                running_jobs.pop(job.job_id, None)
                await job.status_msg.edit(
                    f"🛑 Job <code>{job.job_id}</code> cancelled during download.", parse_mode=ParseMode.HTML
                )
                job_queue.task_done()
                continue

        # -------------------- Subtitle download (cancel-aware) --------------------
        sub_id, sub_name, sub_size = _parse(job.sub)
        if sub_id:
            target_path = os.path.join(Config.DOWNLOAD_DIR, os.path.basename(sub_name or "subs.srt"))
            dl_task = asyncio.create_task(
                client.download_media(
                    sub_id,
                    file_name=target_path,
                    progress=progress_bar,
                    progress_args=("Downloading Subtitle", job.status_msg, time.time(), job.job_id, sub_size)
                )
            )
            running_jobs[job.job_id] = {'dl_task': dl_task, 'dl_path': target_path}
            try:
                local_sub = await dl_task
                local_sub = os.path.basename(local_sub)
            except asyncio.CancelledError:
                running_jobs.pop(job.job_id, None)
                await job.status_msg.edit(
                    f"🛑 Job <code>{job.job_id}</code> cancelled during subtitle download.", parse_mode=ParseMode.HTML
                )
                job_queue.task_done()
                continue

        # -------------------- Encode --------------------
        # Switch entry to ffmpeg process (so /cancel kills it)
        running_jobs.pop(job.job_id, None)

        if job.mode == 'soft':
            out_file = await softmux_vid(local_vid, local_sub, msg=job.status_msg, job_id=job.job_id)
        elif job.mode == 'hard':
            out_file = await hardmux_vid(local_vid, local_sub, msg=job.status_msg, job_id=job.job_id)
        else:  # nosub
            out_file = await nosub_encode(local_vid, msg=job.status_msg, job_id=job.job_id)

        if out_file:
            # rename to desired final name
            src = os.path.join(Config.DOWNLOAD_DIR, out_file)
            dst = os.path.join(Config.DOWNLOAD_DIR, job.final_name)
            try:
                os.rename(src, dst)
            except Exception:
                dst = src  # fallback

            # upload as DOCUMENT with progress
            t2 = time.time()
            await client.send_document(
                job.chat_id,
                document=dst,
                caption=job.final_name,
                file_name=job.final_name,
                progress=progress_bar,
                progress_args=('Uploading…', job.status_msg, t2, job.job_id, None)
            )

            await job.status_msg.edit(
                f"✅ Job <code>{job.job_id}</code> done.",
                parse_mode=ParseMode.HTML
            )

            # cleanup
            for fn in (local_vid, local_sub, job.final_name):
                try:
                    if fn:
                        os.remove(os.path.join(Config.DOWNLOAD_DIR, fn))
                except:
                    pass

        job_queue.task_done()
