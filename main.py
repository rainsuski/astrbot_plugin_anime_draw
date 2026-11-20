# astrbot_plugin_anime_draw/main.py
# 插件主入口（负责事件监听、CLI 参数解析、白名单鉴权、Pipeline 调度、降级回显与 LLM 工具暴露）

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
from .core.workflow import WorkflowPresetManager
from .engines.comfy import ComfyEngine
from .engines.nai import NaiEngine


@register(
    "astrbot_plugin_anime_draw",
    "Author",
    "二次元插画绘图插件（支持多工作流预设、LoRA 强绑定校验与多通道降级调度）",
    "2.1.0",
)
class AnimeDrawPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.config: dict[str, Any] = config or {}

        self.data_dir = Path("data/astrbot_plugin_anime_draw")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.img_dir = self.data_dir / "generated_images"
        self.img_dir.mkdir(parents=True, exist_ok=True)

        # 实例化业务核心与引擎组件
        self.quota_mgr = QuotaManager(self.data_dir / "draw_quota.json")
        self.workflow_mgr = WorkflowPresetManager(self.config)
        self.lora_mgr = LoraPresetManager(self.config)
        self.comfy_engine = ComfyEngine(self.config)
        self.nai_engine = NaiEngine(self.config)

    def _sync_configs(self) -> None:
        """实时向各子模块同步最新插件配置。"""
        self.workflow_mgr.update_config(self.config)
        self.lora_mgr.update_config(self.config)
        self.comfy_engine.update_config(self.config)
        self.nai_engine.update_config(self.config)

    def _build_whitelist_sets(self) -> tuple[set[str], set[str]]:
        """解析并标准化白名单配置，返回 (原始完整串集合, 纯 ID 提取集合)。"""
        raw_cfg = self.config.get("whitelist", [])
        if isinstance(raw_cfg, str):
            entries = [raw_cfg]
        elif isinstance(raw_cfg, list):
            entries = [str(item) for item in raw_cfg]
        else:
            entries = []

        raw_set: set[str] = set()
        pure_id_set: set[str] = set()

        for entry in entries:
            tokens = re.split(r"[,;\s\r\n]+", entry.strip())
            for token in tokens:
                clean_token = token.strip()
                if not clean_token:
                    continue
                raw_set.add(clean_token)
                pure_id = clean_token.split(":")[-1].strip()
                if pure_id:
                    pure_id_set.add(pure_id)

        return raw_set, pure_id_set

    def _is_whitelisted(self, user_id: str, unified_origin: str = "") -> bool:
        """双向归一化白名单校验，兼容纯数字 ID 与 AstrBot unified_msg_origin。"""
        raw_set, pure_id_set = self._build_whitelist_sets()
        if not raw_set and not pure_id_set:
            return False

        uid = str(user_id or "").strip()
        origin = str(unified_origin or "").strip()

        if uid:
            if uid in pure_id_set or uid in raw_set:
                return True
            uid_tail = uid.split(":")[-1].strip()
            if uid_tail and uid_tail in pure_id_set:
                return True

        if origin:
            if origin in raw_set:
                return True
            origin_tail = origin.split(":")[-1].strip()
            if origin_tail and origin_tail in pure_id_set:
                return True

        return False

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

    @staticmethod
    def _tokenize_cli_args(raw_args: str) -> list[str]:
        """容错型 CLI 分词器：支持完整引号包裹的参数，同时免疫正向提示词中的孤立单引号（如 girl's）。"""
        tokens: list[str] = []
        n = len(raw_args)
        i = 0

        while i < n:
            while i < n and raw_args[i].isspace():
                i += 1
            if i >= n:
                break

            quote_char = raw_args[i]
            if quote_char in ('"', "'"):
                close_idx = raw_args.find(quote_char, i + 1)
                if close_idx != -1:
                    tokens.append(raw_args[i + 1 : close_idx])
                    i = close_idx + 1
                    continue

            start = i
            while i < n and not raw_args[i].isspace():
                i += 1
            tokens.append(raw_args[start:i])

        return tokens

    @classmethod
    def _parse_draw_cli(cls, raw_args: str) -> dict[str, str]:
        """解析 /绘图 指令的 CLI 参数标志与剩余正向提示词。"""
        tokens = cls._tokenize_cli_args(raw_args)
        flag_map = {
            "-w": "workflow",
            "--workflow": "workflow",
            "-s": "style",
            "--style": "style",
            "-l": "style",
            "--lora": "style",
            "-r": "resolution",
            "--res": "resolution",
            "--resolution": "resolution",
            "-n": "negative",
            "--neg": "negative",
            "--negative": "negative",
        }

        parsed: dict[str, str] = {
            "workflow": "",
            "style": "",
            "resolution": "",
            "negative": "",
            "prompt": "",
        }
        prompt_tokens: list[str] = []

        idx = 0
        total = len(tokens)
        while idx < total:
            tok = tokens[idx]
            tok_lower = tok.lower()

            if tok_lower in flag_map:
                field = flag_map[tok_lower]
                if idx + 1 < total:
                    parsed[field] = tokens[idx + 1].strip()
                    idx += 2
                else:
                    idx += 1
                continue

            matched_eq = False
            if "=" in tok and tok.startswith("-"):
                flag_part, val_part = tok.split("=", 1)
                flag_lower = flag_part.lower()
                if flag_lower in flag_map:
                    parsed[flag_map[flag_lower]] = val_part.strip()
                    matched_eq = True

            if not matched_eq:
                prompt_tokens.append(tok)
            idx += 1

        parsed["prompt"] = " ".join(prompt_tokens).strip()
        return parsed

    # ================= 业务调度底层 =================
    async def _execute_draw_pipeline(
        self,
        event: AstrMessageEvent,
        prompt: str,
        negative: str = "",
        resolution: str = "1024x1024",
        opening_remark: str = "",
        workflow_preset: str = "",
        lora_preset: str = "",
        use_private_paper: bool = False,
        from_companion: bool = False,
    ) -> tuple[bool, str, bytes | None, Path | None]:
        # 实时同步配置
        self._sync_configs()

        # 0. 解析并校验工作流与 LoRA 强绑定关系（先于额度检查拦截无效请求）
        req_workflow = (workflow_preset or "").strip()
        if req_workflow:
            active_workflow = self.workflow_mgr.get_preset(req_workflow)
            if active_workflow is None:
                avail_names = ", ".join(
                    item["name"] for item in self.workflow_mgr.list_presets_meta()
                )
                return (
                    False,
                    f"❌ 未找到预设 [{req_workflow}]。当前可用预设: {avail_names}",
                    None,
                    None,
                )
        else:
            active_workflow = self.workflow_mgr.get_preset(None)

        assert active_workflow is not None
        workflow_name = active_workflow["name"]

        req_lora = (lora_preset or "").strip()
        active_lora: dict[str, Any] | None = None
        if req_lora:
            target_lora = self.lora_mgr.find_preset(req_lora)
            if target_lora is None:
                return (
                    False,
                    f"❌ 未找到风格预设 [{req_lora}]，请核对名称或留空使用预设默认风格。",
                    None,
                    None,
                )
            if not self.lora_mgr.is_compatible_with_workflow(
                target_lora, workflow_name
            ):
                return (
                    False,
                    f"❌ 风格 [{target_lora['name']}] 不适用于当前预设 [{workflow_name}]，请重新选择或留空使用默认风格。",
                    None,
                    None,
                )
            active_lora = target_lora
        else:
            default_lora_name = active_workflow.get("default_lora", "").strip()
            if default_lora_name:
                candidate = self.lora_mgr.find_preset(default_lora_name)
                if candidate and self.lora_mgr.is_compatible_with_workflow(
                    candidate, workflow_name
                ):
                    active_lora = candidate

        logger.info(
            f"[Draw] 开始调度生图: workflow={workflow_name}, "
            f"lora={active_lora['name'] if active_lora else 'None'}, "
            f"resolution={resolution}"
        )

        # 1. 鉴权与额度检查（支持 unified_msg_origin 与纯数字 ID 双向归一化匹配）
        sender_id = str(event.get_sender_id()).strip() if event else ""
        unified_origin = (
            str(getattr(event, "unified_msg_origin", "")).strip() if event else ""
        )
        quota_user_key = sender_id or (
            unified_origin.split(":")[-1].strip() if unified_origin else ""
        )
        is_whitelist = (
            self._is_whitelisted(sender_id, unified_origin)
            if (sender_id or unified_origin)
            else False
        )
        cd = self.config.get("quota", {}).get("cooldownSeconds", 45)
        daily_limit = self.config.get("quota", {}).get("dailyLimit", 3)

        if use_private_paper or from_companion:
            can_draw, reason = self.quota_mgr.can_draw_siwan_private(
                cooldown_seconds=15
            )
        else:
            can_draw, reason = self.quota_mgr.can_draw_user(
                quota_user_key, is_whitelist, cd, daily_limit
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

        # 5. 执行生图（记录实际成功通道）
        raw_buffer: bytes | None = None
        used_engine = ""
        try:
            raw_buffer = await self.comfy_engine.run(
                workflow_json=active_workflow["workflow_json"],
                positive=final_pos,
                negative=final_neg,
                resolution=resolution,
                lora_cfg=active_lora,
            )
            if raw_buffer:
                used_engine = "comfy"
        except Exception as e:
            logger.warning(f"[Draw] ComfyUI 出错: {self._sanitize_error(e)}")

        if not raw_buffer:
            logger.info("[Draw] 降级至 NAI 通道...")
            try:
                raw_buffer = await self.nai_engine.run(final_pos, final_neg, resolution)
                if raw_buffer:
                    used_engine = "nai"
            except Exception as e:
                logger.error(f"[Draw] NAI 通道出错: {self._sanitize_error(e)}")

        if not raw_buffer:
            return (
                False,
                "❌ 出图失败：本地算力离线且备用通道请求失败。",
                None,
                None,
            )

        # 6. 洗数据与长期存储落盘
        clean_bytes = ImageProcessor.clean_image_metadata(raw_buffer)
        filename = f"anime_{int(time.time())}_{random.randint(100, 999)}.png"
        img_path = self.img_dir / filename
        with open(img_path, "wb") as f:
            f.write(clean_bytes)

        # 7. 扣减额度
        if use_private_paper or from_companion:
            self.quota_mgr.deduct_siwan_private()
        else:
            self.quota_mgr.deduct_user(quota_user_key, is_whitelist)

        # 8. 构造极简回显文案（仅当 ComfyUI 通道实际挂载了有效 LoRA 时回显风格）
        if used_engine == "comfy" and active_lora and active_lora.get("name"):
            finish_text = f"✨ 绘制完成！[风格: {active_lora['name']}]\n"
        else:
            finish_text = "✨ 绘制完成！\n"

        # 9. 发送图片（统一交由 AstrBot 标准消息链发送）
        if not from_companion and event and hasattr(event, "send"):
            try:
                abs_img_path = str(img_path.resolve())
                await event.send(
                    event.chain_result(
                        [
                            Plain(finish_text),
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
        **kwargs: Any,
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
                workflow_preset="",
                lora_preset=lora_preset,
                use_private_paper=use_private_paper,
                from_companion=from_companion,
            )

            if not success or clean_bytes is None or img_path is None:
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
            self._sync_configs()
            meta_list = self.lora_mgr.list_presets_meta()
            current_workflow = self.workflow_mgr.get_default_preset_name()
            current_active = self.config.get("current_lora_preset", "default")

            lines = [
                f"🎨 当前全局默认预设为: [{current_workflow}]，兜底风格为: [{current_active}]\n可选风格列表:"
            ]
            for p in meta_list:
                lines.append(
                    f"- 名称: {p['name']}\n"
                    f"  适用预设: {p['workflows']}\n"
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
            sender_id = str(event.get_sender_id()).strip()
            unified_origin = str(getattr(event, "unified_msg_origin", "")).strip()
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
                is_wl = (
                    self._is_whitelisted(query_id, unified_origin)
                    if query_id == sender_id
                    else self._is_whitelisted(query_id)
                )
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

    # ================= 触发途径 4：辅助预设查询指令 =================
    @filter.command("绘图预设", priority=1000)
    async def cmd_workflow_presets(self, event: AstrMessageEvent):
        """查询当前可用的预设列表。用法：/绘图预设"""
        event.stop_event()
        try:
            self._sync_configs()
            meta_list = self.workflow_mgr.list_presets_meta()
            lines = ["📋 可用预设列表:"]
            for item in meta_list:
                prefix = "[默认] " if item["is_default"] else ""
                default_lora = item["default_lora"] or "无（纯底模出图）"
                lines.append(
                    f"- {prefix}{item['name']}\n"
                    f"  描述: {item['description']}\n"
                    f"  默认风格: {default_lora}"
                )
            lines.append("💡 提示: 绘图时可通过 -w <名称> 指定预设")
            await event.send(event.plain_result("\n".join(lines)))
        except Exception as e:
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 查询预设异常: {clean_err}")
            await event.send(event.plain_result(f"❌ 查询预设失败: {clean_err}"))

    # ================= 触发途径 5：指令直出 =================
    @filter.command("绘图", alias={"画图", "draw"}, priority=1000)
    async def cmd_draw(self, event: AstrMessageEvent):
        """手动绘图指令。用法：/绘图 [-w 预设] [-s 风格] [-r 分辨率] [-n 负向词] <提示词>"""
        msg_str = event.message_str.strip()
        raw_args = re.sub(
            r"^[!/\\#]?(绘图|画图|draw)\s*", "", msg_str, flags=re.IGNORECASE
        ).strip()

        parsed = self._parse_draw_cli(raw_args)
        clean_prompt = parsed["prompt"]

        event.stop_event()

        if not clean_prompt:
            await event.send(
                event.plain_result(
                    "💡 请提供提示词，例如：/绘图 1girl, silver hair, blue eyes\n"
                    '可选参数：-w <预设> -s <风格> -r <分辨率> -n "<负向词>"'
                )
            )
            return

        try:
            await event.send(event.plain_result("🎨 正在绘制，请稍候..."))
        except Exception as e:
            logger.warning(f"[Draw] 发送绘图提示失败: {self._sanitize_error(e)}")

        try:
            success, msg, _, _ = await self._execute_draw_pipeline(
                event,
                prompt=clean_prompt,
                negative=parsed["negative"],
                resolution=parsed["resolution"] or "1024x1024",
                workflow_preset=parsed["workflow"],
                lora_preset=parsed["style"],
                use_private_paper=False,
            )
            if not success:
                await event.send(event.plain_result(self._sanitize_error(msg)))
        except Exception as e:
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 指令直出执行异常: {clean_err}")
            await event.send(event.plain_result(f"❌ 绘图失败: {clean_err}"))

    # ================= 触发途径 5：指令直出 =================
    @filter.command("绘图", alias={"画图", "draw"}, priority=1000)
    async def cmd_draw(self, event: AstrMessageEvent):
        """手动绘图指令。用法：/绘图 [-w 预设] [-s 风格] [-r 分辨率] [-n 负向词] <提示词>"""
        msg_str = event.message_str.strip()
        raw_args = re.sub(
            r"^[!/\\#]?(绘图|画图|draw)\s*", "", msg_str, flags=re.IGNORECASE
        ).strip()

        parsed = self._parse_draw_cli(raw_args)
        clean_prompt = parsed["prompt"]

        if not clean_prompt:
            event.stop_event()
            yield event.plain_result(
                "💡 请提供提示词，例如：/绘图 1girl, silver hair, blue eyes\n"
                '可选参数：-w <预设> -s <风格> -r <分辨率> -n "<负向词>"'
            )
            return

        event.stop_event()
        yield event.plain_result("🎨 正在绘制二次元插画，请稍候...")
        try:
            success, msg, _, _ = await self._execute_draw_pipeline(
                event,
                prompt=clean_prompt,
                negative=parsed["negative"],
                resolution=parsed["resolution"] or "1024x1024",
                workflow_preset=parsed["workflow"],
                lora_preset=parsed["style"],
                use_private_paper=False,
            )
            if not success:
                yield event.plain_result(self._sanitize_error(msg))
        except Exception as e:
            clean_err = self._sanitize_error(e)
            logger.error(f"[Draw] 指令直出执行异常: {clean_err}")
            yield event.plain_result(f"❌ 绘图失败: {clean_err}")
