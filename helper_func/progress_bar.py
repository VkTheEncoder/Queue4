# helper_func/progress_bar.py

import time
import math

async def progress_bar(current, total, text, message, start, job_id=None):
    """
    Edit `message` to show a progress bar.  
    If `job_id` is given, it will appear in the header.
    """
    now  = time.time()
    diff = now - start

    # only refresh every ~10s or on completion
    if round(diff % 10) == 0 or current == total:
        percentage = (current * 100) / total if total else 0
        speed      = current / diff if diff else 0
        elapsed_ms = round(diff) * 1000
        eta_ms     = (round((total - current) / speed) * 1000) if speed else 0
        total_eta  = elapsed_ms + eta_ms

        elapsed_str = TimeFormatter(elapsed_ms)
        eta_str     = TimeFormatter(total_eta)

        # header with optional job_id
        if job_id:
            header = f"🔄 <b>Job {job_id} Progress</b>\n\n"
        else:
            header = "🔄 Progress\n\n"

        # build the bar
        filled_length = math.floor(percentage / 5)
        bar = "[" + "◼️" * filled_length + "◻️" * (20 - filled_length) + "]\n\n"
        stats = (
            f"🔹 {round(percentage, 2)}%  "
            f"({humanbytes(current)}/{humanbytes(total)})\n\n"
            f"🔹 Speed: {humanbytes(speed)}/s\n"
            f"🔹 ETA: {eta_str}\n"
        )

        try:
            await message.edit(text=f"{text}\n\n{header}{bar}{stats}")
        except:
            pass


def humanbytes(size):
    """Convert bytes -> human-readable string."""
    if not size:
        return "0 B"
    power = 2**10
    n = 0
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    while size >= power and n < len(units)-1:
        size /= power
        n += 1
    return f"{round(size, 2)} {units[n]}"


def TimeFormatter(milliseconds: int) -> str:
    """Convert ms -> 'Xd, Xh, Xm, Xs, Xms'."""
    seconds, ms = divmod(int(milliseconds), 1000)
    minutes, sec = divmod(seconds, 60)
    hours, min_ = divmod(minutes, 60)
    days, hr   = divmod(hours, 24)

    parts = []
    if days:   parts.append(f"{days}d")
    if hr:     parts.append(f"{hr}h")
    if min_:   parts.append(f"{min_}m")
    if sec:    parts.append(f"{sec}s")
    if ms:     parts.append(f"{ms}ms")

    return ", ".join(parts) if parts else "0ms"
