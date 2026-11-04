import asyncio
import importlib
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
client = importlib.import_module('astrbot_plugin_dsqq_voice.local')
from dsqq_cli.mlx_worker import validate_job, atomic_json, generate_audio


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

    async def test_clone_uses_reference_and_rejects_unconfigured_speakers(self):
        config = {'mode': 'clone', 'voice': 'Taozijiang',
                  'reference_audio': '/private/reference.wav', 'reference_text': '参考文字'}
        model = Mock()
        job = {'text': '你好', 'voice': 'Taozijiang', 'created_at': time.time()}
        text, voice = validate_job(job, {'taozijiang'})
        generate_audio(model, config, text, voice)
        model.generate.assert_called_once_with(
            text='你好', lang_code='Chinese', ref_audio='/private/reference.wav',
            ref_text='参考文字', max_tokens=4096)
        model.generate_custom_voice.assert_not_called()
        with self.assertRaises(ValueError):
            validate_job({**job, 'voice': 'Vivian'}, {'taozijiang'})

    async def test_preset_keeps_custom_voice_generation(self):
        model = Mock()
        generate_audio(model, {}, '你好', 'Vivian')
        model.generate_custom_voice.assert_called_once_with(
            text='你好', speaker='Vivian', language='Chinese', max_tokens=4096)
        model.generate.assert_not_called()


class CloneConfigurationTest(unittest.TestCase):
    def test_private_reference_survives_source_removal_and_failed_restart_rolls_back(self):
        from dsqq_cli import local_voice
        from dsqq_cli.state import read_json, write_json, UserError
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            runtime = root / 'mlx-voice'
            (runtime / 'venv/bin').mkdir(parents=True)
            (runtime / 'venv/bin/python').touch()
            original = {'model_path': 'preset', 'jobs_dir': 'queue'}
            write_json(runtime / 'worker.json', original)
            audio, text = root / 'source.wav', root / 'source.txt'
            audio.write_bytes(b'wave fixture')
            text.write_text('参考文字')
            args = SimpleNamespace(name='Taozijiang', audio=audio, text_file=text)
            inst = SimpleNamespace(root=root)
            def fake_run(argv, **kwargs):
                if 'soundfile' in argv[2]:
                    Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
                return SimpleNamespace(returncode=0)
            with patch.object(local_voice, 'run', side_effect=fake_run), \
                 patch.object(local_voice, 'start_local_voice') as restart:
                local_voice.configure_local_clone(inst, args, ROOT)
                configured = read_json(runtime / 'worker.json')
                saved = Path(configured['reference_audio'])
                self.assertEqual(saved.read_bytes(), audio.read_bytes())
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)
                self.assertEqual(configured['reference_text'], '参考文字')
                self.assertEqual(configured['mode'], 'clone')
                restart.side_effect = [UserError('load failed'), None]
                with self.assertRaises(UserError):
                    local_voice.configure_local_clone(inst, args, ROOT)
                self.assertEqual(read_json(runtime / 'worker.json'), configured)
                self.assertEqual(list((runtime / 'references').glob('*.wav')), [saved])
            audio.unlink()
            self.assertTrue(saved.is_file())
