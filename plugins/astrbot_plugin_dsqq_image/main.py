import asyncio

from astrbot import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

from .client import ImageError, generate_image

NAME = "astrbot_plugin_dsqq_image"


class DSQQImage(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.job = None
        self.folder = StarTools.get_data_dir(NAME)

    def credentials(self):
        source_id = self.config.get("provider_source_id", "dsqq_source")
        sources = self.context.get_config().get("provider_sources", [])
        source = next((s for s in sources if s.get("id") == source_id), {})
        keys = source.get("key", [])
        key = keys[0] if isinstance(keys, list) and keys else keys if isinstance(keys, str) else ""
        if not source.get("api_base") or not key:
            raise ImageError("生图网关尚未配置，请先配置 DSQQ 模型网关。")
        return source["api_base"], key

    def start(self, event, prompt):
        if not event.is_admin():
            return "生图工具目前仅供管理员使用。"
        if not self.config.get("enabled", True):
            return "生图工具已关闭。"
        prompt = prompt.strip()
        if not prompt or len(prompt) > 4000:
            return "请提供 1–4000 字的图片描述，例如：/生图 一只戴宇航员头盔的蓝色小鱼。"
        if self.job and not self.job.done():
            return "上一张图片还在生成，请等它完成后再试。"
        try:
            base, key = self.credentials()
        except ImageError as exc:
            return str(exc)
        self.job = asyncio.create_task(self.generate_and_send(event, prompt, base, key))
        return "已开始生成一张图片，完成后会自动发到当前聊天。请勿重复调用本工具。"

    @filter.llm_tool(name="generate_image")
    async def image_tool(self, event: AstrMessageEvent, prompt: str):
        """当用户明确要求画图、生成图片或设计图像时调用，生成一张新图并发送到当前聊天。
        仅管理员可用。不要用于搜索现有图片或发送表情包；不要自行生图。
        本工具在后台生成，返回开始生成后不要重复调用，也不要声称已经完成。

        Args:
            prompt(string): 完整图片描述，包含主体、风格和用户要求的文字，最多 4000 字。
        """
        return self.start(event, prompt)

    @filter.command("生图")
    async def image_command(self, event: AstrMessageEvent):
        # The entire remainder is one prompt, including spaces and punctuation.
        text = event.message_str.strip().split(maxsplit=1)
        yield event.plain_result(self.start(event, text[1] if len(text) > 1 else ""))

    async def generate_and_send(self, event, prompt, base, key):
        try:
            path = await asyncio.to_thread(generate_image, base, key,
                                          self.config.get("model", "gpt-image-2"), prompt,
                                          self.folder, int(self.config.get("timeout_seconds", 300)))
            await event.send(event.image_result(str(path)))
            logger.info("[dsqq_image] Generated image delivered successfully.")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Never log raw upstream responses, prompts, signed URLs or credentials.
            logger.warning(f"[dsqq_image] Image job failed ({type(exc).__name__}).")
            message = str(exc) if isinstance(exc, ImageError) else "图片生成或发送失败，请稍后重试。"
            try:
                await event.send(event.plain_result(message))
            except Exception:
                logger.warning("[dsqq_image] Could not deliver failure notification.")

    async def terminate(self):
        if self.job and not self.job.done():
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
