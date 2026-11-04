import asyncio

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Record
from astrbot.api.star import Context, Star, StarTools

from .client import synthesize

NAME = "astrbot_plugin_dsqq_voice"


class DSQQVoice(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.folder = StarTools.get_data_dir(NAME)
        self.busy = False

    def access_denied(self, event):
        if not self.config.get("enabled", True):
            return "语音功能已关闭。"
        if event.is_admin():
            return None
        if event.get_group_id() and any(
            isinstance(part, At) and str(part.qq) == str(event.get_self_id())
            for part in event.get_messages()
        ):
            return None
        return "请在群里 @机器人，再提出语音请求。"

    async def speak(self, event, text):
        denied = self.access_denied(event)
        if denied:
            return denied
        text = text.strip()
        if not text or len(text) > 600:
            return "请提供 1–600 字的语音内容，例如：@机器人 /语音 大家早上好。"
        if self.busy:
            return "上一条语音还在处理，请稍等。"
        self.busy = True
        try:
            path = await synthesize(
                text, self.folder, self.config.get("voice", "zh-CN-XiaoxiaoNeural"),
                backend=self.config.get("backend", "edge"),
                credentials_file=self.config.get("credentials_file", ""),
                resource_id=self.config.get("resource_id", "seed-tts-2.0"))
            try:
                await event.send(event.chain_result([Record.fromFileSystem(str(path), text=text)]))
            except Exception as exc:
                # Do not regenerate/retry: the audio may already be on QQ.
                logger.warning(f"[dsqq_voice] Audio send returned {type(exc).__name__}; using text fallback.")
                return "这次先用文字回复：\n" + text
            return None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No raw service response or user's speech text in logs.
            logger.warning(f"[dsqq_voice] Synthesis returned {type(exc).__name__}; using text fallback.")
            return "这次先用文字回复：\n" + text
        finally:
            self.busy = False

    @filter.llm_tool(name="send_voice")
    async def voice_tool(self, event: AstrMessageEvent, text: str):
        """仅在用户明确要求语音回复、朗读或说一句话时调用；普通聊天必须保持文字回复。
        生成语音并发送到当前聊天。群成员需要 @机器人。不是语音识别工具，不支持唱歌或克隆声音。
        成功后不要重复调用，也不要再复述同一段文字；失败时按返回内容改用文字，不要再次调用。

        Args:
            text(string): 要朗读的自然语言内容，1–600 字；不含 Markdown、链接或舞台动作。
        """
        # AstrBot treats None as a direct reply and ends the agent loop. A success
        # string would request another LLM turn, which can produce empty output
        # because the requested answer has already been delivered as audio.
        return await self.speak(event, text)

    @filter.command("语音")
    async def voice_command(self, event: AstrMessageEvent):
        parts = event.message_str.strip().split(maxsplit=1)
        result = await self.speak(event, parts[1] if len(parts) > 1 else "")
        if result:
            yield event.plain_result(result)
