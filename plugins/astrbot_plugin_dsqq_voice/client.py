"""Bounded Edge synthesis with files shared with NapCat."""
import asyncio
import os
from pathlib import Path
import time
import uuid

import edge_tts


async def synthesize(text, folder, voice="zh-CN-XiaoxiaoNeural", timeout=45):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for old in folder.glob("dsqq-voice-*.*"):
        if old.suffix in (".mp3", ".wav") and old.stat().st_mtime < time.time() - 86400:
            old.unlink(missing_ok=True)
    stem = folder / ("dsqq-voice-" + uuid.uuid4().hex)
    mp3, wav = stem.with_suffix(".mp3"), stem.with_suffix(".wav")
    process = None
    try:
        mp3.touch(mode=0o600)
        await asyncio.wait_for(edge_tts.Communicate(text, voice=voice).save(str(mp3)), timeout)
        if not mp3.stat().st_size:
            raise RuntimeError("Speech response was empty")
        wav.touch(mode=0o600)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-nostdin", "-y", "-v", "error", "-i", str(mp3),
            "-acodec", "pcm_s16le", "-ar", "24000", "-ac", "1", str(wav),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(process.wait(), 20)
        if process.returncode or wav.stat().st_size <= 44:
            raise RuntimeError("Speech conversion failed")
        os.chmod(wav, 0o600)
        return wav
    except BaseException:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        wav.unlink(missing_ok=True)
        raise
    finally:
        mp3.unlink(missing_ok=True)
