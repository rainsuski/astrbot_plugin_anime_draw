# astrbot_plugin_anime_draw/engines/comfy.py
# ComfyUI 算力执行引擎（支持工作流占位符动态渲染、按需登录与多输出节点结果提取）

import asyncio
import random
from typing import Any

import httpx

from astrbot.api import logger

from ..core.constants import ALLOWED_RESOLUTIONS
from ..core.workflow import WorkflowPresetManager


class ComfyEngine:
    """ComfyUI 异步任务调度与图片拉取引擎。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.cookie: str | None = None

    def update_config(self, config: dict[str, Any]) -> None:
        self.config = config

    async def login(self, client: httpx.AsyncClient) -> bool:
        conf = self.config.get("comfy", {})
        host = conf.get("host", "127.0.0.1")
        port = conf.get("port", 8188)
        username = str(conf.get("username", "")).strip()
        password = str(conf.get("password", "")).strip()

        if not username and not password:
            return False

        url = f"http://{host}:{port}/login"
        try:
            res = await client.post(
                url,
                data={
                    "username": username,
                    "password": password,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": f"http://{host}:{port}",
                    "Referer": f"http://{host}:{port}/login",
                },
                timeout=5.0,
            )
            if "AIOHTTP_SESSION" in res.cookies:
                self.cookie = f"AIOHTTP_SESSION={res.cookies['AIOHTTP_SESSION']}"
                return True
        except Exception as e:
            logger.error(f"[Draw] ComfyUI 登录异常: {type(e).__name__}: {e}")
        return False

    @staticmethod
    def _extract_first_image_info(outputs: dict[str, Any]) -> dict[str, Any] | None:
        """从 ComfyUI history outputs 中提取首个生成图片信息（兼容任意自定义输出节点 ID）。"""
        if not isinstance(outputs, dict):
            return None

        # 优先取常规最终输出（type == "output"），若无则退化为任意带 images 的节点（如 PreviewImage）
        fallback_info: dict[str, Any] | None = None
        for node_output in outputs.values():
            if not isinstance(node_output, dict):
                continue
            images = node_output.get("images")
            if not isinstance(images, list) or not images:
                continue
            for img in images:
                if not isinstance(img, dict) or not img.get("filename"):
                    continue
                if img.get("type") == "output":
                    return img
                if fallback_info is None:
                    fallback_info = img
        return fallback_info

    async def run(
        self,
        workflow_json: str,
        positive: str,
        negative: str,
        resolution: str,
        lora_cfg: dict[str, Any] | None = None,
    ) -> bytes | None:
        conf = self.config.get("comfy", {})
        host = conf.get("host", "127.0.0.1")
        port = conf.get("port", 8188)
        base_url = f"http://{host}:{port}"
        dim = ALLOWED_RESOLUTIONS.get(resolution, ALLOWED_RESOLUTIONS["1024x1024"])

        lora_text = str(lora_cfg.get("text", "")) if lora_cfg else ""
        loras_list = lora_cfg.get("loras", []) if lora_cfg else []
        seed = random.randint(10000000, 999999999999)

        workflow = WorkflowPresetManager.render_workflow(
            workflow_json=workflow_json,
            positive=positive,
            negative=negative,
            width=dim["width"],
            height=dim["height"],
            seed=seed,
            lora_text=lora_text,
            loras_list=loras_list,
        )

        has_credentials = bool(
            str(conf.get("username", "")).strip()
            or str(conf.get("password", "")).strip()
        )

        async with httpx.AsyncClient(timeout=120.0) as client:
            headers: dict[str, str] = {"Origin": base_url, "Referer": f"{base_url}/"}
            if has_credentials and not self.cookie:
                if not await self.login(client):
                    return None
            if self.cookie:
                headers["Cookie"] = self.cookie

            try:
                stats = await client.get(
                    f"{base_url}/system_stats", headers=headers, timeout=3.0
                )
                if stats.status_code in (401, 403):
                    if has_credentials and await self.login(client) and self.cookie:
                        headers["Cookie"] = self.cookie
                    else:
                        return None
            except Exception:
                return None

            res = await client.post(
                f"{base_url}/prompt", headers=headers, json={"prompt": workflow}
            )
            if res.status_code != 200:
                logger.warning(
                    f"[Draw] ComfyUI /prompt 提交失败，状态码: {res.status_code}"
                )
                return None

            prompt_id = res.json().get("prompt_id")
            if not prompt_id:
                return None

            for _ in range(90):
                await asyncio.sleep(2.0)
                h_res = await client.get(
                    f"{base_url}/history/{prompt_id}", headers=headers
                )
                if h_res.status_code == 200:
                    data = h_res.json()
                    outputs = data.get(prompt_id, {}).get("outputs", {})
                    img_info = self._extract_first_image_info(outputs)
                    if img_info:
                        filename = img_info.get("filename", "")
                        subfolder = img_info.get("subfolder", "")
                        img_type = img_info.get("type", "output")
                        view_url = (
                            f"{base_url}/view?filename={filename}"
                            f"&subfolder={subfolder}&type={img_type}"
                        )
                        img_res = await client.get(view_url, headers=headers)
                        if img_res.status_code == 200:
                            return img_res.content
        return None
