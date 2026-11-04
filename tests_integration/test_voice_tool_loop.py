"""Run inside the deployed AstrBot container; no model, synthesis or QQ traffic.

python -m unittest discover -s tests_integration -p test_voice_tool_loop.py -v
DSQQ_PLUGIN_DIR may point to a staged plugins directory for pre-deploy checks.
"""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get("DSQQ_PLUGIN_DIR", "/AstrBot/data/plugins"))

from astrbot.core.agent.hooks import BaseAgentRunHooks
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.runners.tool_loop_agent_runner import ToolLoopAgentRunner
from astrbot.core.agent.tool import FunctionTool, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.astr_agent_tool_exec import FunctionToolExecutor
from astrbot.core.provider.entites import LLMResponse, ProviderRequest
from astrbot_plugin_dsqq_voice import main


class VoiceToolLoopTest(unittest.IsolatedAsyncioTestCase):
    async def run_voice(self, synthesis_error=None):
        event = Mock(spec=main.AstrMessageEvent)
        event.is_admin.return_value = True
        event.get_result.return_value = None
        event.get_extra.return_value = None
        event.send = AsyncMock()
        with patch.object(main.StarTools, "get_data_dir", return_value=Path("/tmp")):
            plugin = main.DSQQVoice(Mock(), {})
        tool = FunctionTool(
            name="send_voice", description="Speak only on explicit request",
            parameters={"type": "object", "properties": {"text": {"type": "string"}},
                        "required": ["text"]}, handler=plugin.voice_tool,
        )
        provider = Mock()
        provider.provider_config = {"id": "voice-regression", "max_context_tokens": 0}
        # Replay the observed second-turn error at the model boundary.
        follow_up = LLMResponse(
            role="assistant" if synthesis_error else "err",
            completion_text="这次先用文字回复：你好" if synthesis_error else
            "All chat models failed: EmptyModelOutputError: OpenAI completion has no usable output.",
        )
        provider.text_chat = AsyncMock(side_effect=[
            LLMResponse(role="assistant", tools_call_name=["send_voice"],
                        tools_call_args=[{"text": "你好"}], tools_call_ids=["voice-test"]),
            follow_up,
        ])
        runner = ToolLoopAgentRunner()
        await runner.reset(
            provider=provider, request=ProviderRequest(func_tool=ToolSet([tool]),
                contexts=[{"role": "user", "content": "请用语音说你好"}]),
            run_context=ContextWrapper(AstrAgentContext(context=Mock(spec=main.Context), event=event)),
            tool_executor=FunctionToolExecutor(), agent_hooks=BaseAgentRunHooks(),
        )
        synth = AsyncMock(return_value=Path("/tmp/voice-regression.wav"),
                          side_effect=synthesis_error)
        with patch.object(main, "synthesize", synth):
            responses = [response async for response in runner.step_until_done(3)]
        return runner, provider, event, synth, responses

    async def test_delivered_voice_ends_loop_before_empty_model_follow_up(self):
        runner, provider, event, synth, responses = await self.run_voice()
        synth.assert_awaited_once()
        event.send.assert_awaited_once()
        self.assertFalse(any(response.type == "err" for response in responses),
                         "Already-delivered audio must not be followed by an LLM error")
        provider.text_chat.assert_awaited_once()
        self.assertTrue(runner.done())

    async def test_failed_synthesis_keeps_text_fallback_turn(self):
        runner, provider, event, synth, responses = await self.run_voice(TimeoutError())
        synth.assert_awaited_once()
        event.send.assert_not_called()
        self.assertEqual(provider.text_chat.await_count, 2)
        self.assertFalse(any(response.type == "err" for response in responses))
        self.assertEqual(runner.get_final_llm_resp().completion_text, "这次先用文字回复：你好")


if __name__ == "__main__":
    unittest.main()
