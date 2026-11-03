import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from dsqq_cli.config import compose_config, initial_astrbot, initial_napcat, new_config, render_model, validate_model
from dsqq_cli.main import main, ensure_files, configure, import_pack, mcp
from dsqq_cli.runtime import AstrBot, Docker, NapCat, docker_context
from dsqq_cli.state import Installation, UserError, read_json, write_json


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "中文 data $1"
        self.inst = Installation(self.root)
        self.inst.config = new_config("123456", "https://gateway.example/v1", "claude-test", "sk-test-secret",
                                      "colima-test", "Asia/Singapore")
        self.root.mkdir(mode=0o700)
        self.inst.save()

    def tearDown(self):
        self.temp.cleanup()

    def test_private_atomic_persistence_and_redaction(self):
        self.assertEqual(self.inst.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.inst.load()["model"]["name"], "claude-test")
        self.assertEqual(self.inst.redact("error sk-test-secret"), "error ***")
        self.assertFalse(list(self.root.glob(".dsqq-*")))

    def test_astrbot_utf8_bom_config_is_readable(self):
        path = self.root / "cmd_config.json"
        path.write_text('\ufeff{"config_version":5}', encoding="utf-8")
        self.assertEqual(read_json(path)["config_version"], 5)

    def test_reject_unknown_schema(self):
        write_json(self.inst.path, {"schema": 99})
        with self.assertRaises(UserError):
            self.inst.load()

    def test_cross_process_lock(self):
        script = "from dsqq_cli.state import Installation; import sys; i=Installation(sys.argv[1]);\nwith i.lock(): pass"
        with self.inst.lock():
            result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("另一个 DSQQ", result.stderr)
        with self.inst.lock():
            pass

    def test_reject_init_over_unrelated_files(self):
        other = Path(self.temp.name) / "other"
        other.mkdir()
        (other / "important.txt").write_text("keep")
        with self.assertRaises(UserError), Installation(other).lock(creating=True):
            pass
        self.assertEqual((other / "important.txt").read_text(), "keep")

    def test_failed_init_can_retry(self):
        fresh = Installation(Path(self.temp.name) / "fresh")
        with fresh.lock(creating=True):
            pass
        with fresh.lock(creating=True):
            pass

    def test_container_network_and_media_paths(self):
        c = compose_config(self.inst.config)
        astro, nap = c["services"]["astrbot"], c["services"]["napcat"]
        self.assertTrue(all(p.startswith("127.0.0.1:") for p in astro["ports"] + nap["ports"]))
        self.assertEqual(len(astro["ports"]), 1)  # OneBot is not exposed on host.
        media = lambda service: next(v for v in service["volumes"] if v["target"] == "/AstrBot/data")
        self.assertEqual(media(astro), media(nap))
        self.assertNotIn("sk-test-secret", json.dumps(c))
        self.assertNotIn("privileged", nap)
        conf = initial_astrbot(self.inst.config)
        client = initial_napcat(self.inst.config)["network"]["websocketClients"][0]
        self.assertEqual(client["url"], "ws://astrbot:6199/ws")
        self.assertEqual(client["token"], conf["platform"][0]["ws_reverse_token"])
        self.assertEqual(conf["dashboard"]["host"], "0.0.0.0")
        self.assertNotIn(self.inst.config["secrets"]["dashboard_password"], json.dumps(conf))

    def test_recovery_does_not_overwrite_runtime_config(self):
        ensure_files(self.inst)
        path = self.root / "astrbot/data/cmd_config.json"
        conf = read_json(path)
        conf["custom"] = {"preserve": True}
        write_json(path, conf)
        self.inst.config["model"]["name"] = "different"
        ensure_files(self.inst)
        self.assertEqual(read_json(path), conf)
        self.assertTrue((self.root / "pack/tools/import_dafeiyu.py").exists())

    def test_model_change_keeps_other_configuration(self):
        conf = initial_astrbot(self.inst.config)
        conf["provider"].append({"id": "embedding", "model": "bge-m3"})
        conf["agent_runner"]["config"]["persona"] = {"persona_id": "大肥鱼"}
        conf["provider_settings"]["other"] = True
        self.inst.config["model"]["name"] = "claude-new"
        render_model(conf, self.inst.config)
        render_model(conf, self.inst.config)
        self.assertEqual(len(conf["provider"]), 2)
        self.assertEqual(conf["agent_runner"]["config"]["persona"]["persona_id"], "大肥鱼")
        self.assertTrue(conf["provider_settings"]["other"])

    def test_configure_stops_before_disk_mutation_and_restarts(self):
        ensure_files(self.inst)
        args = argparse.Namespace(base_url=None, model="new", api_key_file=None, api_key_env=None)
        with patch("dsqq_cli.main.Docker") as factory:
            docker = factory.return_value
            docker.running.return_value = ["astrbot", "napcat"]
            with contextlib.redirect_stdout(io.StringIO()):
                configure(self.inst, args)
            self.assertEqual([tuple(c.args) for c in docker.compose.call_args_list],
                             [("stop", "astrbot"), ("start", "astrbot")])
        self.assertEqual(read_json(self.root / "astrbot/data/cmd_config.json")["provider"][0]["model"], "new")

    def test_import_failure_removes_temporary_credentials_and_redacts_logs(self):
        ensure_files(self.inst)
        args = argparse.Namespace(memory="siliconflow", voice="none")
        def fake_run(argv, **kwargs):
            folder = next((self.root / "import").iterdir())
            answers = read_json(folder / "answers.json")
            self.assertNotIn("sk-memory-secret", json.dumps(answers))
            self.assertTrue(answers["password_file"].startswith("/dsqq/import/"))
            return subprocess.CompletedProcess(argv, 1, "failed sk-memory-secret sk-test-secret", "")
        with patch("dsqq_cli.main.Docker"), patch("dsqq_cli.main.AstrBot"), \
             patch.dict(os.environ, {"SILICONFLOW_API_KEY": "sk-memory-secret"}), \
             patch("dsqq_cli.main.run", side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(UserError):
                import_pack(self.inst, args)
        self.assertEqual(list((self.root / "import").iterdir()), [])
        self.assertNotIn("secret", (self.root / "import.log").read_text())

    def test_purge_needs_explicit_flag_and_never_removes_without_down(self):
        with patch("dsqq_cli.main.Docker") as factory, contextlib.redirect_stderr(io.StringIO()):
            code = main(["--root", str(self.root), "uninstall", "--purge"])
            factory.return_value.compose.assert_not_called()
        self.assertEqual(code, 2)
        self.assertTrue(self.inst.path.exists())

    def test_cli_uses_saved_context_and_project(self):
        docker = Docker(self.inst)
        with patch("dsqq_cli.runtime.run", return_value=subprocess.CompletedProcess([], 0, "[]", "")) as call:
            self.assertEqual(docker.processes(), [])
        argv = call.call_args.args[0]
        self.assertEqual(argv[:3], ["docker", "--context", "colima-test"])
        self.assertIn(self.inst.config["project"], argv)
        self.assertIn(str(self.inst.root / "compose.json"), argv)

    def test_ps_accepts_ndjson(self):
        with patch.object(Docker, "compose", return_value='{"Service":"astrbot","State":"running"}\n{"Service":"napcat","State":"exited"}'):
            self.assertEqual(Docker(self.inst).running(), ["astrbot"])

    def test_mcp_list_does_not_expose_remote_credentials(self):
        with patch("dsqq_cli.main.AstrBot") as factory, contextlib.redirect_stdout(io.StringIO()) as out:
            factory.return_value.call.return_value = [{"name": "demo", "headers": {"Authorization": "other-secret"},
                                                      "env": {"KEY": "private"}, "connected": True, "tools": ["search"]}]
            mcp(self.inst, argparse.Namespace(action="list"))
        self.assertNotIn("secret", out.getvalue())
        self.assertNotIn("private", out.getvalue())
        self.assertIn("search", out.getvalue())

    def test_models_reads_v4_response_envelope_and_hides_keys(self):
        with patch("dsqq_cli.main.AstrBot") as factory, contextlib.redirect_stdout(io.StringIO()) as out:
            factory.return_value.call.return_value = {"providers": [
                {"id": "dsqq/chat", "model": "claude-test", "key": ["never-print-this"]}], "model_metadata": {}}
            self.assertEqual(main(["--root", str(self.root), "models"]), 0)
        self.assertIn("claude-test", out.getvalue())
        self.assertNotIn("never-print-this", out.getvalue())

    def test_napcat_reauths_expired_token(self):
        responses = [{"code": 0, "data": {"Credential": "old"}},
                     {"code": -1, "message": "Unauthorized"},
                     {"code": 0, "data": {"Credential": "new"}},
                     {"code": 0, "data": {"isLogin": True}}]
        with patch("dsqq_cli.runtime.request_json", side_effect=responses):
            self.assertTrue(NapCat(self.inst).call("/QQLogin/CheckLoginStatus")["isLogin"])

    def test_napcat_session_reused_across_cli_instances(self):
        with patch("dsqq_cli.runtime.request_json", side_effect=[
            {"code": 0, "data": {"Credential": "session"}},
            {"code": 0, "data": {"isLogin": False}},
            {"code": 0, "data": {"isLogin": True}}]) as req:
            NapCat(self.inst).call("/QQLogin/CheckLoginStatus")
            self.assertTrue(NapCat(self.inst).call("/QQLogin/CheckLoginStatus")["isLogin"])
            self.assertEqual(len(req.call_args_list), 3)
        self.assertEqual((self.root / ".napcat-session.json").stat().st_mode & 0o777, 0o600)

    def test_mcp_add_uses_upstream_envelope(self):
        file = self.root / "mcp.json"
        config = {"command": "python", "args": ["/server.py"]}
        write_json(file, config)
        with patch("dsqq_cli.main.AstrBot") as factory, contextlib.redirect_stdout(io.StringIO()):
            mcp(self.inst, argparse.Namespace(action="add", file=str(file), name="demo"))
            factory.return_value.call.assert_called_once_with(
                "POST", "/mcp/servers", {"name": "demo", "mcpServers": {"demo": config}}, timeout=120)

    def test_astrbot_http_response_is_not_readiness_without_auth(self):
        with patch("dsqq_cli.runtime.request_json", return_value={"status": "error"}):
            with self.assertRaises(UserError):
                AstrBot(self.inst).login()

    def test_astrbot_reuses_session_but_probes_readiness(self):
        with patch("dsqq_cli.runtime.request_json", side_effect=[
            {"status": "ok", "data": {"token": "session"}},
            {"status": "ok", "data": {"providers": []}},
            UserError("offline")]) as req:
            AstrBot(self.inst).login()
            AstrBot(self.inst).login()
            self.assertTrue(req.call_args_list[1].args[0].endswith("/providers"))
            with self.assertRaises(UserError):
                AstrBot(self.inst).login()


class ValidationTest(unittest.TestCase):
    def test_container_localhost_is_rejected(self):
        for url in ("http://localhost:3000/v1", "http://127.0.0.1/v1", "http://[::1]/v1",
                    "file:///tmp/a", "https://user:pass@example.com/v1", "https://example.com/?key=secret"):
            with self.subTest(url=url), self.assertRaises(UserError):
                validate_model(url, "model")
        validate_model("http://host.docker.internal:3000/v1", "model")

    def test_remote_docker_rejected(self):
        with patch("dsqq_cli.runtime.shutil.which", return_value="/bin/docker"), \
             patch("dsqq_cli.runtime.run", return_value=subprocess.CompletedProcess([], 0, "ssh://host", "")):
            with self.assertRaises(UserError):
                docker_context("remote")


if __name__ == "__main__":
    unittest.main()
