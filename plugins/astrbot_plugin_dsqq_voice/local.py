"""Async file-queue client for the Mac's native GPU worker."""
import asyncio
import json
import os
from pathlib import Path
import time
import uuid


async def synthesize_local(text, folder, voice, timeout=180):
    queue = Path(folder) / 'mlx_jobs'
    try:
        health = json.loads((queue / 'health.json').read_text())
        if health.get('status') != 'ready' or not 0 <= time.time() - health['updated_at'] < 10:
            raise RuntimeError('Local voice worker is not ready')
    except (OSError, ValueError, KeyError, TypeError):
        raise RuntimeError('Local voice worker is unavailable') from None
    job_id = uuid.uuid4().hex
    request = queue / (job_id + '.request.json')
    result = queue / (job_id + '.result.json')
    wav = queue / (job_id + '.wav')
    temp = queue / (job_id + '.tmp')
    try:
        with temp.open('w', encoding='utf-8') as out:
            os.chmod(temp, 0o600)
            json.dump({'text': text, 'voice': voice, 'created_at': time.time()}, out, ensure_ascii=False)
        os.replace(temp, request)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if result.exists():
                if not json.loads(result.read_text()).get('ok') or not wav.is_file() or wav.stat().st_size <= 44:
                    raise RuntimeError('Local voice synthesis failed')
                return wav
            await asyncio.sleep(0.2)
        raise TimeoutError('Local voice synthesis timed out')
    finally:
        # Never automatically resubmit; a claimed job may still be finishing.
        temp.unlink(missing_ok=True)
        request.unlink(missing_ok=True)
        result.unlink(missing_ok=True)
