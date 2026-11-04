import asyncio
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))


class VoicePluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        modules = {name: types.ModuleType(name) for name in (
            "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star",
            "astrbot.api.message_components", "edge_tts")}
        modules["astrbot"].logger = Mock()
        modules["astrbot.api.event"].AstrMessageEvent = object
        modules["astrbot.api.event"].filter = types.SimpleNamespace(
            llm_tool=lambda **kw: lambda f: f, command=lambda *a: lambda f: f)
        class Star:
            def __init__(self, context):
                self.context = context
        class At:
            def __init__(self, qq):
                self.qq = qq
        class Record:
            @staticmethod
            def fromFileSystem(path, **kwargs):
                return {"type": "record", "path": path, **kwargs}
        star = modules["astrbot.api.star"]
        star.Star, star.Context = Star, object
        star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: "/unused")
        modules["astrbot.api.message_components"].At = At
        modules["astrbot.api.message_components"].Record = Record
        with patch.dict(sys.modules, modules):
            self.main = importlib.import_module("astrbot_plugin_dsqq_voice.main")
            self.client = importlib.import_module("astrbot_plugin_dsqq_voice.client")
        self.plugin = self.main.DSQQVoice(Mock(), {})
        self.event = Mock()
        self.event.is_admin.return_value = False
        self.event.get_group_id.return_value = "88"
        self.event.get_self_id.return_value = "42"
        self.event.get_messages.return_value = [self.main.At("42")]
        self.event.send = AsyncMock()

    async def test_group_member_explicit_tool_sends_one_record(self):
        with patch.object(self.main, "synthesize", new=AsyncMock(return_value=Path("/tmp/voice.wav"))) as synth:
            result = await self.plugin.voice_tool(self.event, "你好")
        self.assertIsNone(result)
        synth.assert_awaited_once()
        self.event.send.assert_awaited_once()
        record = self.event.chain_result.call_args.args[0][0]
        self.assertEqual(record["type"], "record")
        self.assertEqual(record["text"], "你好")
        self.assertFalse(self.plugin.busy)

    async def test_unaddressed_other_mentions_and_private_member_rejected(self):
        with patch.object(self.main, "synthesize", new=AsyncMock()) as synth:
            for group, mentions in (("88", []), ("88", [self.main.At("99")]),
                                    ("88", [self.main.At("all")]), ("", [self.main.At("42")])):
                self.event.get_group_id.return_value = group
                self.event.get_messages.return_value = mentions
                self.assertIn("@机器人", await self.plugin.speak(self.event, "你好"))
        synth.assert_not_called()
        self.event.send.assert_not_called()

    async def test_busy_empty_long_and_disabled_do_not_call_service(self):
        with patch.object(self.main, "synthesize", new=AsyncMock()) as synth:
            for text in ("", "字" * 601):
                self.assertIn("1–600", await self.plugin.speak(self.event, text))
            self.plugin.busy = True
            self.assertIn("稍等", await self.plugin.speak(self.event, "你好"))
            self.plugin.busy = False
            self.plugin.config["enabled"] = False
            self.assertIn("关闭", await self.plugin.speak(self.event, "你好"))
        synth.assert_not_called()

    async def test_synthesis_failure_falls_back_to_original_text(self):
        with patch.object(self.main, "synthesize", new=AsyncMock(side_effect=TimeoutError("private URL"))) as synth:
            result = await self.plugin.voice_tool(self.event, "你好")
        self.assertEqual(result, "这次先用文字回复：\n你好")
        self.assertFalse(self.plugin.busy)
        synth.assert_awaited_once()
        self.event.send.assert_not_called()

    async def test_send_timeout_does_not_retry_or_expose_uncertainty(self):
        self.event.send.side_effect = TimeoutError("private detail")
        with patch.object(self.main, "synthesize", new=AsyncMock(return_value=Path("/tmp/voice.wav"))) as synth:
            result = await self.plugin.speak(self.event, "你好")
        self.assertEqual(result, "这次先用文字回复：\n你好")
        synth.assert_awaited_once()
        self.event.send.assert_awaited_once()

    async def test_cancel_releases_job(self):
        with patch.object(self.main, "synthesize", new=AsyncMock(side_effect=asyncio.CancelledError())):
            with self.assertRaises(asyncio.CancelledError):
                await self.plugin.speak(self.event, "你好")
        self.assertFalse(self.plugin.busy)

    async def test_command_keeps_spaces_and_has_no_duplicate_success_text(self):
        self.event.message_str = "语音 hello my friend"
        with patch.object(self.plugin, "speak", new=AsyncMock(return_value=None)) as speak:
            result = [r async for r in self.plugin.voice_command(self.event)]
        speak.assert_awaited_once_with(self.event, "hello my friend")
        self.assertEqual(result, [])

    async def test_interrupted_synthesis_cleans_partial_files(self):
        with tempfile.TemporaryDirectory() as folder:
            communicate = Mock(save=AsyncMock(side_effect=asyncio.CancelledError()))
            with patch.object(self.client.edge_tts, "Communicate", return_value=communicate, create=True):
                with self.assertRaises(asyncio.CancelledError):
                    await self.client.synthesize("你好", folder)
            self.assertEqual(list(Path(folder).iterdir()), [])


class VoiceInstallerTest(unittest.TestCase):
    def test_installer_keeps_group_model_and_image_config_and_disables_auto_tts(self):
        from dsqq_cli import voice
        from dsqq_cli.state import read_json, write_json
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            inst = types.SimpleNamespace(root=root)
            config_path = root / "astrbot/data/cmd_config.json"
            original = {"provider_tts_settings": {"enable": True, "provider_id": "old"},
                        "platform_settings": {"id_whitelist": ["group"]}, "provider": [{"id": "chat"}]}
            write_json(config_path, original)
            image_path = root / "astrbot/data/config/astrbot_plugin_dsqq_image_config.json"
            write_json(image_path, {"allow_group_members": True})
            docker = Mock()
            docker.running.return_value = []
            with patch.object(voice, "Docker", return_value=docker), patch.object(voice, "configure_send_timeout"):
                voice.voice_command(inst, types.SimpleNamespace(voice="zh-CN-XiaoxiaoNeural"), ROOT)
            expected = {**original, "provider_tts_settings": {"enable": False, "provider_id": "old"}}
            self.assertEqual(read_json(config_path), expected)
            self.assertTrue(read_json(image_path)["allow_group_members"])
            self.assertTrue((root / "astrbot/data/plugins/astrbot_plugin_dsqq_voice/requirements.txt").is_file())
