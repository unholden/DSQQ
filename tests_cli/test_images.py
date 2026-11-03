import asyncio
import base64
import importlib
import io
import copy
import time
import json
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins"))
client = importlib.import_module("astrbot_plugin_dsqq_image.client")
delivery = importlib.import_module("astrbot_plugin_dsqq_image.delivery")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class ImageClientTest(unittest.TestCase):
    def test_real_http_contract_and_base64_response(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                requests.append((self.path, self.headers.get("Authorization"),
                                 json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"data": [{"b64_json": base64.b64encode(PNG).decode()}]}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                p = client.generate_image(f"http://127.0.0.1:{server.server_port}/v1", "secret", "gpt-image-2", "蓝色小鱼", temp)
                self.assertEqual(p.read_bytes(), PNG)
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            self.assertEqual(requests, [("/v1/images/generations", "Bearer secret", {
                "model": "gpt-image-2", "prompt": "蓝色小鱼", "n": 1, "size": "1024x1024"})])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_download_has_no_gateway_credentials(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(PNG)
        with tempfile.TemporaryDirectory() as temp, patch.object(client, "public_image_url", side_effect=lambda x: x), \
                patch.object(client.urllib.request, "build_opener", return_value=opener):
            p = client.save_response({"data": [{"url": "https://cdn.example/image.png"}]}, temp)
            self.assertEqual(p.read_bytes(), PNG)
        req = opener.open.call_args.args[0]
        self.assertIsNone(req.get_header("Authorization"))
        self.assertIn("DSQQImage", req.get_header("User-agent"))
        self.assertIn("image/png", req.get_header("Accept"))

    def test_cdn_failure_does_not_suggest_regenerating(self):
        opener = Mock()
        opener.open.side_effect = client.urllib.error.HTTPError("https://cdn.example/x", 403, "denied", {}, io.BytesIO(b"denied"))
        with patch.object(client, "public_image_url", side_effect=lambda x: x), \
                patch.object(client.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(client.ImageError) as caught:
                client.save_response({"data": [{"url": "https://cdn.example/x"}]}, "/unused")
        self.assertIn("图片已生成", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    def test_reject_private_urls_including_redirect(self):
        private = [(2, 1, 6, "", ("127.0.0.1", 443))]
        with patch.object(client.socket, "getaddrinfo", return_value=private):
            with self.assertRaises(client.ImageError):
                client.public_image_url("https://cdn.example/x")
            with self.assertRaises(client.ImageError):
                client.PublicRedirect().redirect_request(None, None, 302, "", {}, "https://cdn.example/x")
        for url in ("http://example.com/x", "file:///etc/passwd", "https://user:secret@example.com/x"):
            with self.assertRaises(client.ImageError):
                client.public_image_url(url)

    def test_gateway_redirect_does_not_forward_key(self):
        with self.assertRaises(client.ImageError):
            client.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")

    def test_bad_or_oversized_results_are_not_saved(self):
        with self.assertRaises(client.ImageError):
            client.read_bounded(io.BytesIO(b"01234"), 4)
        with tempfile.TemporaryDirectory() as temp:
            for payload in ({}, {"data": []}, {"data": [{"b64_json": "!!!"}]},
                            {"data": [{"b64_json": base64.b64encode(b"<html>error</html>").decode()}]}):
                with self.assertRaises(client.ImageError):
                    client.save_response(payload, temp)
            self.assertEqual(list(Path(temp).iterdir()), [])

    def test_upstream_error_is_not_leaked_or_retried(self):
        opener = Mock()
        opener.open.side_effect = client.urllib.error.HTTPError("https://gateway", 401, "Bearer secret", {}, io.BytesIO(b"denied"))
        with patch.object(client.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(client.ImageError) as caught:
                client.generate_image("https://gateway/v1", "secret", "gpt-image-2", "test", "/unused")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)


class ImagePluginTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Exercise the plugin's authorization/job lifecycle without starting AstrBot.
        mods = {name: types.ModuleType(name) for name in (
            "astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")}
        mods["astrbot"].logger = Mock()
        event_mod = mods["astrbot.api.event"]
        event_mod.AstrMessageEvent = object
        event_mod.filter = types.SimpleNamespace(llm_tool=lambda **kw: lambda f: f, command=lambda *a: lambda f: f)

        class Star:
            def __init__(self, context):
                self.context = context

        star = mods["astrbot.api.star"]
        star.Star, star.Context = Star, object
        star.StarTools = types.SimpleNamespace(get_data_dir=lambda name: "/unused")
        with patch.dict(sys.modules, mods):
            self.module = importlib.import_module("astrbot_plugin_dsqq_image.main")
        context = Mock()
        context.get_config.return_value = {"provider_sources": [{"id": "dsqq_source", "key": ["secret"], "api_base": "https://gateway/v1"}]}
        self.plugin = self.module.DSQQImage(context, {})
        self.event = Mock()
        self.event.is_admin.return_value = True
        self.event.send = AsyncMock()
        self.event.get_self_id.return_value = "42"
        self.event.get_sender_id.return_value = "7"
        self.event.get_group_id.return_value = ""
        self.event.unified_msg_origin = "qq:private:7"
        self.event.bot.call_action = AsyncMock(return_value={"messages": []})
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.plugin.folder = Path(self.temp.name)
        self.path = self.plugin.folder / ("dsqq-image-" + "a" * 32 + ".png")
        self.path.write_bytes(PNG)
        self.delays = patch.object(delivery, "CHECK_DELAYS", (0, 0))
        self.delays.start()
        self.addCleanup(self.delays.stop)

    def sent_row(self):
        return {"message_id": 123, "real_seq": "14", "time": time.time(),
                "sender": {"user_id": 42}, "message_type": "private",
                "message_sent_type": "self", "message": [{"type": "image", "data": {
                    "file": delivery.md5_file(self.path).upper() + ".png", "file_size": str(len(PNG))}}]}


    async def asyncTearDown(self):
        await self.plugin.terminate()

    async def test_member_cannot_generate_or_read_credentials(self):
        self.event.is_admin.return_value = False
        self.assertIn("管理员", self.plugin.start(self.event, "draw"))
        self.assertIsNone(self.plugin.job)
        self.plugin.context.get_config.assert_not_called()

    async def test_one_job_delivers_image_and_rejects_duplicate(self):
        with patch.object(self.module, "generate_image", return_value=self.path) as generate:
            self.assertIn("已开始", self.plugin.start(self.event, "blue fish"))
            self.assertIn("上一张", self.plugin.start(self.event, "blue fish"))
            await self.plugin.job
            self.assertEqual(generate.call_count, 1)
            self.event.image_result.assert_called_once_with(str(self.path))
            self.event.send.assert_awaited_once_with(self.event.image_result.return_value)

    async def test_failure_notifies_without_exposing_exception(self):
        with patch.object(self.module, "generate_image", side_effect=RuntimeError("secret")):
            self.plugin.start(self.event, "test")
            await self.plugin.job
        message = self.event.plain_result.call_args.args[0]
        self.assertNotIn("secret", message)
        self.event.send.assert_awaited_once()

    async def test_send_timeout_is_not_reported_as_generation_failure(self):
        # Match aiocqhttp.ActionFailed's result shape and repr-only error contract.
        class ActionFailed(Exception):
            result = {"retcode": 1200, "wording": (
                "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
                "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet: {}")}

            def __str__(self):
                return repr(self.result)

        self.event.send.side_effect = ActionFailed()
        self.event.bot.call_action.side_effect = [
            {"messages": []}, {"messages": []}, {"messages": [self.sent_row()]}]
        with patch.object(self.module, "generate_image", return_value=self.path) as generate:
            self.plugin.start(self.event, "blue fish")
            await self.plugin.job
        self.event.plain_result.assert_not_called()
        self.event.send.assert_awaited_once()
        self.assertEqual(generate.call_count, 1)
        receipt = json.loads(delivery.receipt_path(self.plugin.folder, self.event).read_text())
        self.assertEqual((receipt["status"], receipt["message_id"]), ("sent", 123))

    async def test_ack_timeout_does_not_expose_transport_uncertainty(self):
        self.event.send.side_effect = [RuntimeError(
            "Timeout: NTEvent serviceAndMethod:NodeIKernelMsgService/sendMsg "
            "ListenerName:NodeIKernelMsgListener/onMsgInfoListUpdate EventRet: {}"), None]
        with patch.object(self.module, "generate_image", return_value=self.path):
            self.plugin.start(self.event, "blue fish")
            await self.plugin.job
        for call in self.event.plain_result.call_args_list:
            self.assertNotIn("确认超时", call.args[0])
            self.assertNotIn("可能已送达", call.args[0])

    async def test_explicit_send_failure_keeps_generation_success(self):
        self.event.send.side_effect = [RuntimeError("invalid target secret"), None]
        with patch.object(self.module, "generate_image", return_value=self.path) as generate:
            self.plugin.start(self.event, "blue fish")
            await self.plugin.job
        message = self.event.plain_result.call_args.args[0]
        self.assertIn("图片已做好并保存", message)
        self.assertIn("/取图", message)
        self.assertNotIn("secret", message)
        self.assertEqual(generate.call_count, 1)

    async def test_generation_timeout_does_not_claim_image_exists(self):
        with patch.object(self.module, "generate_image", side_effect=TimeoutError("private detail")):
            self.plugin.start(self.event, "blue fish")
            await self.plugin.job
        message = self.event.plain_result.call_args.args[0]
        self.assertNotIn("图片已生成", message)
        self.assertNotIn("private detail", message)
        self.event.image_result.assert_not_called()

    async def test_command_keeps_full_prompt(self):
        self.event.message_str = "/生图 a blue fish, white background"
        with patch.object(self.plugin, "start", return_value="started") as start:
            replies = [r async for r in self.plugin.image_command(self.event)]
        start.assert_called_once_with(self.event, "a blue fish, white background")
        self.assertEqual(len(replies), 1)

    async def test_history_outage_does_not_resend_or_regenerate(self):
        self.event.bot.call_action.side_effect = ConnectionError("private detail")
        self.event.send.side_effect = [TimeoutError(), None]
        with patch.object(self.module, "generate_image", return_value=self.path) as generate:
            await self.plugin.generate_and_send(self.event, "fish", "base", "key")
        self.event.image_result.assert_called_once()
        self.assertEqual(generate.call_count, 1)
        self.assertIn("/取图", self.event.plain_result.call_args.args[0])
        self.assertEqual(delivery.latest_image(self.plugin.folder, self.event), self.path)

    async def test_explicit_retrieve_uses_existing_file(self):
        delivery.save_receipt(self.plugin.folder, self.event, self.path, "saved")
        with patch.object(self.module, "generate_image") as generate:
            replies = [r async for r in self.plugin.retrieve_command(self.event)]
            await self.plugin.job
        generate.assert_not_called()
        self.assertEqual(len(replies), 1)
        self.event.image_result.assert_called_once_with(str(self.path))

    async def test_retrieve_is_scoped_and_admin_only(self):
        delivery.save_receipt(self.plugin.folder, self.event, self.path, "saved")
        self.event.is_admin.return_value = False
        _ = [r async for r in self.plugin.retrieve_command(self.event)]
        self.assertIsNone(self.plugin.job)
        self.event.is_admin.return_value = True
        self.event.unified_msg_origin = "qq:group:99"
        _ = [r async for r in self.plugin.retrieve_command(self.event)]
        self.assertIsNone(self.plugin.job)
        self.event.send.assert_not_called()

    async def test_receipt_cannot_escape_image_directory(self):
        receipt = delivery.receipt_path(self.plugin.folder, self.event)
        for name in ("../secret", "/etc/passwd", "anything.png"):
            receipt.write_text(json.dumps({"image": name}))
            self.assertIsNone(delivery.latest_image(self.plugin.folder, self.event))

    async def test_cancellation_stops_reconciliation(self):
        self.event.bot.call_action.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await delivery.reconcile_delivery(self.event, delivery.target_for(self.event))

    async def test_only_new_server_sequenced_matching_image_confirms(self):
        ticket = await delivery.prepare_delivery(self.event, self.path)
        row = self.sent_row()
        self.assertIs(delivery.matching_message({"messages": [row]}, ticket), row)
        variants = [{"real_seq": "0"}, {"time": ticket["started_at"] - 10},
                    {"sender": {"user_id": "7"}}, {"target_id": "8"},
                    {"message_type": "group"}, {"message_sent_type": "pending"},
                    {"message": [{"type": "image", "data": {"file": "0" * 32 + ".png"}}]},
                    {"message": [{"type": "image", "data": {
                        "file": delivery.md5_file(self.path), "file_size": "999999"}}]}]
        for change in variants:
            with self.subTest(change=change):
                self.assertIsNone(delivery.matching_message({"messages": [{**row, **change}]}, ticket))
        ticket["before_ids"] = {"123"}
        self.assertIsNone(delivery.matching_message({"messages": [row]}, ticket))
        ticket.update(kind="group", target_id="88", before_ids=set())
        row.update(message_type="group", group_id="88")
        self.assertIsNotNone(delivery.matching_message({"data": {"messages": [row]}}, ticket))
        row["group_id"] = "99"
        self.assertIsNone(delivery.matching_message({"messages": [row]}, ticket))


class TimeoutPolicyTest(unittest.TestCase):
    def test_preserves_other_config_and_longer_user_timeouts(self):
        from dsqq_cli.images import tune_send_timeout
        original = {"network": {"token": "secret"}, "timeout": {
            "baseTimeout": 10000, "uploadSpeedKBps": 256, "maxTimeout": 1800000}}
        tuned = tune_send_timeout(copy.deepcopy(original))
        expected = copy.deepcopy(original)
        expected["timeout"]["baseTimeout"] = 30000
        self.assertEqual(tuned, expected)
        tuned["timeout"]["baseTimeout"] = 90000
        self.assertEqual(tune_send_timeout(tuned)["timeout"]["baseTimeout"], 90000)

    def test_recorded_images_fit_new_wait_budget(self):
        from dsqq_cli.images import tune_send_timeout
        base = tune_send_timeout({})["timeout"]["baseTimeout"] / 1000
        for size, elapsed in ((1686984, 27), (2806109, 32)):
            upload = size / 1024 / 256
            self.assertLess(10 + upload, elapsed)
            self.assertGreater(base + upload, elapsed)
