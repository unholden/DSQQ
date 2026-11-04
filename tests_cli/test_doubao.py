import base64
import importlib
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins'))
doubao = importlib.import_module('astrbot_plugin_dsqq_voice.doubao')


def stream(*rows):
    return io.BytesIO(b''.join(json.dumps(row).encode() + b'\n' for row in rows))


class DoubaoTest(unittest.TestCase):
    def test_audio_chunks_are_joined_only_after_success_terminal(self):
        chunks = [{'code': 0, 'data': base64.b64encode(x).decode()} for x in (b'ID3first', b'second')]
        self.assertEqual(doubao.parse_audio(stream(*chunks, {'code': 20000000})), b'ID3firstsecond')
        for rows in (chunks, [{'code': 20000000}], chunks + [{'code': 45000030, 'message': 'private detail'}]):
            with self.assertRaises(doubao.DoubaoError) as caught:
                doubao.parse_audio(stream(*rows))
            self.assertNotIn('private detail', str(caught.exception))

    def test_invalid_and_oversized_response_rejected(self):
        for data in (b'[]\n', b'no json\n', b'{"code":0,"data":"!!!!"}\n'):
            with self.assertRaises(doubao.DoubaoError):
                doubao.parse_audio(io.BytesIO(data))
        with patch.object(doubao, 'MAX_LINE', 10):
            with self.assertRaises(doubao.DoubaoError):
                doubao.parse_audio(io.BytesIO(b'x' * 11))

    def test_new_and_legacy_credentials_use_documented_headers_without_retry(self):
        for credentials in ({'api_key': 'test-secret'}, {'app_id': '123', 'access_token': 'test-token'}):
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'credentials.json'
                path.write_text(json.dumps(credentials))
                opener = Mock()
                opener.open.return_value = stream({'code': 0, 'data': 'SUQz'}, {'code': 20000000})
                with patch.object(doubao.urllib.request, 'build_opener', return_value=opener):
                    self.assertEqual(doubao.request_audio('你好', doubao.DEFAULT_VOICE, path), b'ID3')
                opener.open.assert_called_once()
                req = opener.open.call_args.args[0]
                self.assertEqual(req.full_url, doubao.ENDPOINT)
                body = json.loads(req.data)
                self.assertEqual(body['req_params']['text'], '你好')
                self.assertEqual(body['req_params']['audio_params']['format'], 'mp3')
                if 'api_key' in credentials:
                    self.assertEqual(req.get_header('X-api-key'), 'test-secret')
                    self.assertIsNone(req.get_header('X-api-access-key'))
                else:
                    self.assertEqual(req.get_header('X-api-app-id'), '123')
                    self.assertEqual(req.get_header('X-api-access-key'), 'test-token')

    def test_redirect_and_raw_http_error_never_expose_key(self):
        with self.assertRaises(doubao.DoubaoError):
            doubao.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'key.json'
            path.write_text('{"api_key":"test-secret"}')
            opener = Mock()
            opener.open.side_effect = doubao.urllib.error.HTTPError(doubao.ENDPOINT, 401, 'test-secret', {}, io.BytesIO(b'test-secret'))
            with patch.object(doubao.urllib.request, 'build_opener', return_value=opener):
                with self.assertRaises(doubao.DoubaoError) as caught:
                    doubao.request_audio('你好', doubao.DEFAULT_VOICE, path)
            self.assertNotIn('test-secret', str(caught.exception))
            opener.open.assert_called_once()

    def test_missing_credentials_do_not_stop_running_service(self):
        from dsqq_cli import voice
        from dsqq_cli.state import UserError
        with tempfile.TemporaryDirectory() as folder, patch.object(voice, 'Docker') as docker:
            args = types.SimpleNamespace(backend='doubao', voice=None, credentials_file=None)
            with self.assertRaises(UserError):
                voice.voice_command(types.SimpleNamespace(root=Path(folder)), args, ROOT)
            docker.assert_not_called()

    def test_doubao_install_persists_private_credentials_and_preserves_backend_on_update(self):
        from dsqq_cli import voice
        from dsqq_cli.state import read_json, write_json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inst = types.SimpleNamespace(root=root)
            write_json(root / 'astrbot/data/cmd_config.json', {'provider_tts_settings': {'enable': False}})
            source = root / 'source.json'
            write_json(source, {'api_key': 'test-secret'})
            docker = Mock()
            docker.running.return_value = []
            args = types.SimpleNamespace(backend='doubao', voice=None, credentials_file=source)
            with patch.object(voice, 'Docker', return_value=docker), patch.object(voice, 'configure_send_timeout'):
                voice.voice_command(inst, args, ROOT)
                voice.voice_command(inst, types.SimpleNamespace(backend=None, voice=None), ROOT)
            config_path = root / 'astrbot/data/config/astrbot_plugin_dsqq_voice_config.json'
            config = read_json(config_path)
            self.assertEqual(config['backend'], 'doubao')
            self.assertEqual(config['voice'], doubao.DEFAULT_VOICE)
            self.assertNotIn('test-secret', config_path.read_text())
            cred = root / 'astrbot/data/plugin_data/astrbot_plugin_dsqq_voice/doubao_credentials.json'
            self.assertEqual(cred.stat().st_mode & 0o777, 0o600)
            self.assertEqual(read_json(cred)['api_key'], 'test-secret')
