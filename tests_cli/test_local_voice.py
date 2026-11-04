import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
client = importlib.import_module('astrbot_plugin_dsqq_voice.local')
from dsqq_cli.mlx_worker import validate_job, atomic_json


class LocalVoiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.queue = self.folder / 'mlx_jobs'
        self.queue.mkdir()
        atomic_json(self.queue / 'health.json', {'status': 'ready', 'updated_at': time.time()})

    async def test_native_queue_round_trip(self):
        job = asyncio.create_task(client.synthesize_local('你好', self.folder, 'Vivian', timeout=2))
        await asyncio.sleep(0.01)
        request = next(self.queue.glob('*.request.json'))
        data = json.loads(request.read_text())
        self.assertEqual(validate_job(data), ('你好', 'Vivian'))
        self.assertEqual(request.stat().st_mode & 0o777, 0o600)
        job_id = request.name.split('.')[0]
        wav = self.queue / (job_id + '.wav')
        wav.write_bytes(b'RIFF' + b'\0' * 50)
        atomic_json(self.queue / (job_id + '.result.json'), {'ok': True})
        self.assertEqual(await job, wav)
        self.assertFalse(request.exists())
        self.assertEqual(list(self.queue.glob('*.result.json')), [])

    async def test_stale_or_busy_worker_gets_no_request(self):
        for status, stamp in (('ready', time.time()-30), ('busy', time.time())):
            atomic_json(self.queue / 'health.json', {'status': status, 'updated_at': stamp})
            with self.assertRaises(RuntimeError):
                await client.synthesize_local('你好', self.folder, 'Vivian')
            self.assertEqual(list(self.queue.glob('*.request.json')), [])

    async def test_timeout_and_cancellation_do_not_resubmit(self):
        with self.assertRaises(TimeoutError):
            await client.synthesize_local('你好', self.folder, 'Vivian', timeout=0.01)
        self.assertEqual(list(self.queue.glob('*.request.json')), [])
        job = asyncio.create_task(client.synthesize_local('你好', self.folder, 'Vivian'))
        await asyncio.sleep(0.01)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertEqual(list(self.queue.glob('*.request.json')), [])

    async def test_worker_error_never_returns_audio(self):
        job = asyncio.create_task(client.synthesize_local('你好', self.folder, 'Vivian', timeout=2))
        await asyncio.sleep(0.01)
        request = next(self.queue.glob('*.request.json'))
        atomic_json(self.queue / (request.name.split('.')[0] + '.result.json'), {'ok': False})
        with self.assertRaises(RuntimeError):
            await job

    async def test_worker_rejects_old_invalid_voice_and_oversized_text(self):
        base = {'text': '你好', 'voice': 'Vivian', 'created_at': time.time()}
        for change in ({'voice': '../file'}, {'text': ''}, {'text': '字'*601}, {'created_at': 0}):
            with self.assertRaises(ValueError):
                validate_job({**base, **change})
