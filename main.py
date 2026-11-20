import asyncio
import base64
import random
import re
import time
from pathlib import Path
from typing import Any

from mcp.types import CallToolResult, ImageContent, TextContent

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image as CompImage
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .core.auditor import ContentAuditor
from .core.constants import ALLOWED_RESOLUTIONS
from .core.image import ImageProcessor
from .core.lora import LoraPresetManager
from .core.quota import QuotaManager
from .engines.comfy import ComfyEngine
from .engines.nai import NaiEngine


@register(
    "astrbot_plugin_anime_draw",
    "Author",
    "二次元插画绘图插件（模块化解耦版，支持界面化 LoRA 预设管理与主动风格选择）",
    "2.0.1",
)
class AnimeDrawPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}

        self.data_dir = Path("data/astrbot_plugin_anime_draw")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.img_dir = self.data_dir / "generated_images"
        self.img_dir.mkdir(parents=True, exist_ok=True)

        # 实例化业务核心与引擎组件
        self.quota_mgr = QuotaManager(self.data_dir / "draw_quota.json")
        self.lora_mgr = LoraPresetManager(self.config)
        self.comfy_engine = ComfyEngine(self.config)
        self.nai_engine = NaiEngine(self.config)

    def _is_whitelisted(self, user_id: str) -> bool:
        whitelist = [
            str(x).strip() for x in self.config.get("whitelist", []) if str(x).strip()
        ]
        return str(user_id).strip() in whitelist

    @staticmethod
    def _sanitize_error(error_obj: Any) -> str:
        """异常信息强力净化与截断器。

        物理抹除任何可能的 Base64 编码、二进制大对象与冗长堆栈，防止污染 Agent 上下文导致 Token 爆炸。
        """
        raw = str(error_obj or "").strip()
        if not raw:
            return "未知异常"

        # 1. 消除 base64:// 协议长串
        cleaned = re.sub(
            r"base64://[A-Za-z0-9+/=]{20,}", "base64://[DATA_TRUNCATED]", raw
        )
        # 2. 消除 Data URI 格式的图片长串
        cleaned = re.sub(
            r"data:image/[^;]+;base64,[A-Za-z0-9+/=]{20,}",
            "data:image/...;base64,[DATA_TRUNCATED]",
            cleaned,
        )
        # 3. 抹除连续超过 60 个字符的无空格 Base64/Hex 特征块
        cleaned = re.sub(r"[A-Za-z0-9+/=]{60,}", "[LONG_DATA_TRUNCATED]", cleaned)
        # 4. 去除多余换行，限制在 200 个字符以内
        cleaned = " ".join(cleaned.split())
        if len(cleaned) > 200:
            cleaned = cleaned[:200] + "...(已截断)"
        return cleaned

    # ================= 业务调度底层 =================
    async def _execute_draw_pipeline(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative: str = "",
        resolution: str = "1024x1024",
        opening_remark: str = "",
        lora_preset: str = "",
        use_private_paper: bool = False,
        from_companion: bool = False,
    ) -> tuple[bool, str, bytes | None, Path | None]:
        # 实时同步配置
        self.lora_mgr.update_config(self.config)
        self.comfy_engine.update_config(self.config)
        self.nai_engine.update_config(self.config)

        user_id = str(event.get_sender_id()) if event else ""
        is_whitelist = self._is_whitelisted(user_id) if user_id else False
        cd = self.config.get("quota", {}).get("cooldownSeconds", 45)
        daily_limit = self.config.get("quota", {}).get("dailyLimit", 3)

        # 1. 鉴权与额度检查
        if use_private_paper or from_companion:
            can_draw, reason = self.quota_mgr.can_draw_siwan_private(
                cooldown_seconds=15
            )
        else:
            can_draw, reason = self.quota_mgr.can_draw_user(
                user_id, is_whitelist, cd, daily_limit
            )

        if not can_draw:
            return False, reason, None, None

        # 2. 分辨率容错
        if resolution not in ALLOWED_RESOLUTIONS:
            resolution = "1024x1024"

        # 3. SFW 审查
        safe, audit_msg = ContentAuditor.audit(prompt, negative)
        if not safe:
            return False, audit_msg, None, None

        # 4. 开场白连续气泡推送（加防护，避免协议端报错打断整个绘画主流程）
        if opening_remark and not from_companion and event and hasattr(event, "send"):
            try:
                lines = [
                    line.strip() for line in opening_remark.split("\n") if line.strip()
                ]
                for line in lines:
                    await event.send(event.plain_result(line))
                    await asyncio.sleep(0.6)
            except Exception as e:
                logger.warning(f"[Draw] 开场白推送失败: {self._sanitize_error(e)}")

        final_pos = f"safe, sfw, {prompt or ''}".strip(" ,")
        final_neg = f"nsfw, explicit, nude, uncensored, lewd, {negative or ''}".strip(
            " ,"
        )

        # 5. 动态加载所选 LoRA 预设结构
        active_lora = self.lora_mgr.get_preset(lora_preset)

        # 6. 执行生图
        raw_buffer = None
        try:
            raw_buffer = await self.comfy_engine.run(
                final_pos, final_neg, resolution, active_lora
            )
        except Exception as e:
            logger.warning(f"[Draw] ComfyUI 出错: {self._sanitize_error(e)}")

        if not raw_buffer:
            logger.info("[Draw] 降级至 NAI 通道...")
            try:
                raw_buffer = await self.nai_engine.run(final_pos, final_neg, resolution)
            except Exception as e:
                logger.error(f"[Draw] NAI 通道出错: {self._sanitize_error(e)}")

        if not raw_buffer:
            return (
                False,
                "❌ 出图失败：本地算力离线且备用通道请求失败。",
                None,
                None,
            )

        # 7. 洗数据与长期存储落盘
        clean_bytes = ImageProcessor.clean_image_metadata(raw_buffer)
        filename = f"anime_{int(time.time())}_{random.randint(100, 999)}.png"
        img_path = self.img_dir / filename
        with open(img_path, "wb") as f:
            f.write(clean_bytes)

        # 8. 扣减额度
        if use_private_paper or from_companion:
            self.quota_mgr.deduct_siwan_private()
        else:
            self.quota_mgr.deduct_user(user_id, is_whitelist)

        # 9. 发送图片（针对 OneBot/NapCat 走原生 API 直传文件路径，绕过 AstrBot 强转 base64 逻辑）
        if not from_companion and event:
            try:
                abs_img_path = str(img_path.resolve())

                # 检测是否为 OneBot (aiocqhttp) 平台
                is_aiocqhttp = (
                    hasattr(event, "get_platform_name")
                    and event.get_platform_name() == "aiocqhttp"
                ) or hasattr(event, "bot")

                if is_aiocqhttp and hasattr(event, "bot"):
                    # 优先采用 NapCat/OneBot 原生消息段，直接传递真实绝对路径（不走 AstrBot 的 convert_to_base64）
                    group_id = (
                        event.get_group_id() if hasattr(event, "get_group_id") else None
                    )
                    sender_id = (
                        event.get_sender_id()
                        if hasattr(event, "get_sender_id")
                        else None
                    )

                    # 组装 OneBot 原生消息段：NapCat 支持原生本地绝对路径
                    onebot_message = [
                        {
                            "type": "text",
                            "data": {
                                "text": f"✨ 绘制完成！[风格: {active_lora['name']}]\n"
                            },
                        },
                        {
                            "type": "image",
                            "data": {
                                "file": abs_img_path
                            },  # NapCat 原生识别本地物理路径，不产生 base64
                        },
                    ]

                    if group_id and str(group_id) not in ("0", ""):
                        await event.bot.call_action(
                            "send_group_msg",
                            group_id=int(group_id),
                            message=onebot_message,
                        )
                    elif sender_id:
                        await event.bot.call_action(
                            "send_private_msg",
                            user_id=int(sender_id),
                            message=onebot_message,
                        )
                else:
                    # 非 OneBot 平台，回退到框架标准发送链路
                    if hasattr(event, "send"):
                        await event.send(
                            event.chain_result(
                                [
                                    Plain(
                                        f"✨ 绘制完成！[风格: {active_lora['name']}]\n"
                                    ),
                                    CompImage.fromFileSystem(abs_img_path),
                                ]
                            )
                        )
            except Exception as e:
                clean_err = self._sanitize_error(e)
                logger.error(f"[Draw] 协议端发图失败: {clean_err}")
                return (
                    False,
                    f"图片已生成，但发送至聊天失败: {clean_err}",
                    clean_bytes,
                    img_path,
                )

        return True, "绘制成功", clean_bytes, img_path

    # ================= 触发途径 1：LLM 绘图工具 =================
    @filter.llm_tool(name="anime-draw")
    async def tool_draw(
        self,
        event: AstrMessageEvent,
        prompt: str,
        opening_remark: str = "",
        use_private_paper: bool = True,
        negative_prompt: str = "",
        resolution: str = "1024x1024",
        lora_preset: str = "",
        from_companion: bool = False,
        **kwargs,
    ) -> CallToolResult:
        """【画图工具】绘制二次元动漫插画。

        Args:
            prompt(string): 正向提示词。英文 Danbooru 风格标签（逗号分隔）
            opening_remark(string): 动笔作画前，根据你的人设性格回复给用户的前置短句
            use_private_paper(boolean): 【四碗的私房画纸】默认True。是否动用四碗自己的私房画纸，只有在对方明确要求你画的时候才可以填否
            negative_prompt(string): 负向提示词
            resolution(string): 画布分辨率，默认 "1024x1024"
            lora_preset(string): 可选风格预设名称（留空则使用当前默认画风）
        """
        try:
            success, msg, clean_bytes, img_path = await self._execute_draw_pipeline(
                event,
                prompt=prompt,
                negative=negative_prompt,
                resolution=resolution,
                opening_remark=opening_remark,
                lora_preset=lora_preset,
                use_private_paper=use_private_paper,
                from_companion=from_companion,
            )

            if not success:
                clean_msg = self._sanitize_error(msg)
                return CallToolResult(
                    content=[
                        TextContent(type="text", text=f'{{"error": "{clean_msg}"}}')
                    ]
                )

            abs_path = str(img_path.resolve())
            compressed_bytes = ImageProcessor.compress_for_vision_model(clean_bytes)
            b64_img = base64.b64encode(compressed_bytes).decode("utf-8")

            paper_tip = (
                "【动用了四碗的私房画纸】"
                if (use_private_paper or from_companion)
                else "【消耗了群友的日常画纸】"
            )

            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=(
                            f"【绘制成功】{paper_tip}\n"
                            f"本地图片路径: {abs_path}\n"
                            "所附图片为本次绘制结果的缩略图，供参考评价。"
                        ),
                    ),
                    ImageContent(type="image", data=b64_img, mimeType="image/jpeg"),
                ]
            )
        except Exception as e:
            # 严格防止未捕获异常上抛给 tool_loop_agent_runner
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 工具执行异常: {clean_err}")
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f'{{"error": "生图发送失败: {clean_err}"}}',
                    )
                ]
            )

    # ================= 触发途径 2：风格查询工具 =================
    @filter.llm_tool(name="query-anime-styles")
    async def tool_query_styles(self, event: AstrMessageEvent) -> CallToolResult:
        """【查询画风风格】获取当前插件支持的所有 LoRA 风格名称、特点描述、标签及适用场景。方便你在绘图前按需挑选合适的 lora_preset。"""
        try:
            self.lora_mgr.update_config(self.config)
            meta_list = self.lora_mgr.list_presets_meta()
            current_active = self.config.get("current_lora_preset", "default")

            lines = [f"🎨 当前系统默认全局风格为: [{current_active}]\n可选风格列表:"]
            for p in meta_list:
                lines.append(
                    f"- 名称: {p['name']}\n"
                    f"  描述: {p['description']}\n"
                    f"  标签: {p['tags']}\n"
                    f"  适用场景: {p['scenarios']}"
                )

            return CallToolResult(
                content=[TextContent(type="text", text="\n".join(lines))]
            )
        except Exception as e:
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 查询风格异常: {clean_err}")
            return CallToolResult(
                content=[TextContent(type="text", text=f'{{"error": "{clean_err}"}}')]
            )

    # ================= 触发途径 3：LLM 额度查询工具 =================
    @filter.llm_tool(name="query-draw-paper")
    async def tool_query_paper(
        self,
        event: AstrMessageEvent,
        target_user_id: str = "",
    ) -> CallToolResult:
        """【画纸额度查询】查询群友剩余的画纸张数，或查询四碗自己剩余的私房画纸。"""
        try:
            sender_id = str(event.get_sender_id())
            query_id = target_user_id.strip() if target_user_id.strip() else sender_id
            daily_limit = self.config.get("quota", {}).get("dailyLimit", 3)

            siwan_private_left = self.quota_mgr.get_siwan_private_remaining(10)
            is_querying_self = query_id.lower() in (
                "bot",
                "self",
                "siwan",
                "四碗",
            ) or query_id == str(
                self.context.get_self_id()
                if hasattr(self.context, "get_self_id")
                else ""
            )

            if is_querying_self:
                report = (
                    f"【画纸资产盘点】\n"
                    f"🎨 四碗今日私房画纸剩余: {siwan_private_left} / 10 张\n"
                    f"（提醒：私房画纸每天 0 点刷新，省着点用哦！）"
                )
            else:
                is_wl = self._is_whitelisted(query_id)
                user_left = (
                    "无限（经纪人特权）"
                    if is_wl
                    else f"{self.quota_mgr.get_user_remaining(query_id, daily_limit)} / {daily_limit} 张"
                )
                report = (
                    f"【画纸查询结果】\n"
                    f"📄 用户 (ID: {query_id}) 今日剩余画纸: {user_left}\n"
                    f"🎨 四碗今日私房画纸储备: {siwan_private_left} / 10 张\n"
                    f"（请用四碗可爱的语气将查询结果告知对方，别机械报读）"
                )

            return CallToolResult(content=[TextContent(type="text", text=report)])
        except Exception as e:
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 额度查询异常: {clean_err}")
            return CallToolResult(
                content=[TextContent(type="text", text=f'{{"error": "{clean_err}"}}')]
            )

    # ================= 触发途径 4：指令直出 =================
    @filter.command("绘图", alias={"画图", "draw"}, priority=1000)
    async def cmd_draw(self, event: AstrMessageEvent):
        """手动绘图指令。用法：/绘图 1girl, white hair"""
        msg_str = event.message_str.strip()
        clean_prompt = re.sub(
            r"^[!/\\#]?(绘图|画图|draw)\s*", "", msg_str, flags=re.IGNORECASE
        ).strip()

        if not clean_prompt:
            event.stop_event()
            yield event.plain_result(
                "💡 请提供提示词，例如：/绘图 1girl, silver hair, blue eyes"
            )
            return

        event.stop_event()
        yield event.plain_result("🎨 正在绘制二次元插画，请稍候...")
        try:
            success, msg, _, _ = await self._execute_draw_pipeline(
                event,
                prompt=clean_prompt,
                negative="",
                resolution="1024x1024",
                use_private_paper=False,
            )
            if not success:
                yield event.plain_result(self._sanitize_error(msg))
        except Exception as e:
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 指令直出执行异常: {clean_err}")
            yield event.plain_result(f"❌ 绘图失败: {clean_err}")
