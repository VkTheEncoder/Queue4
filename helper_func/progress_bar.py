# helper_func/progress_bar.py

import time
import math

async def progress_bar(current, total, text, message, start, job_id=None, known_total=None):
    """
    Generic Telegram progress callback.
    - current, total: provided by Pyrogram (total can be 0 sometimes).
    - known_total: if provided and total==0, we'll use known_total.
    """
    now  = time.time()
    diff = now - start

    # prefer known_total when Telegram can't provide it
    if (not total or total == 0) and known_total:
        total = known_total

    # refresh ~every 2s or on completion
    if diff == 0:
        return
    if (now - getattr(progress_bar, "_last", 0)) < 2 and current != total:
        return
    progress_bar._last = now

    percentage = (current * 100) / total if total else 0.0

    speed = current / diff  # bytes/sec
    eta   = (total - current) / speed if speed > 0 and total else 0
    bar   = _bar(percentage)

    header = f"{text}" + (f"  [ID: {job_id}]" if job_id else "")
    body   = (
        f"{bar} {percentage:.2f}%\n"
        f"• {humanbytes(current)} of {humanbytes(total)}"
        f"\n• Speed: {humanrate(speed)}"
        f"\n• ETA: {timefmt(int(eta))}"
    )
    try:
        await message.edit_text(f"{header}\n{body}")
    except:
        pass


def _bar(pct: float) -> str:
    filled = int(pct // 5)  # 20 slots
    return "▰" * filled + "▱" * (20 - filled)

def humanbytes(size: int) -> str:
    if not size:
        return "0 B"
    power = 1024
    n = 0
    units = ['B', 'KiB', 'MiB', 'GiB', 'TiB']
    while size >= power and n < len(units)-1:
        size /= power
        n += 1
    return f"{round(size, 2)} {units[n]}"

def humanrate(bps: float) -> str:
    if bps <= 0:
        return "0 B/s"
    power = 1024
    n = 0
    units = ['B/s', 'KiB/s', 'MiB/s', 'GiB/s', 'TiB/s']
    while bps >= power and n < len(units)-1:
        bps /= power
        n += 1
    return f"{round(bps, 2)} {units[n]}"

def timefmt(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    if s: parts.append(f"{s}s")
    return " ".join(parts)
