import asyncio
import random
from typing import Any

import httpx

from astrbot.api import logger

from ..core.constants import ALLOWED_RESOLUTIONS


class ComfyEngine:
    def __init__(self, config: dict):
        self.config = config
        self.cookie: str | None = None

    def update_config(self, config: dict):
        self.config = config

    async def login(self, client: httpx.AsyncClient) -> bool:
        conf = self.config.get("comfy", {})
        host, port = conf.get("host", "127.0.0.1"), conf.get("port", 24603)
        url = f"http://{host}:{port}/login"
        try:
            res = await client.post(
                url,
                data={
                    "username": conf.get("username", "suski"),
                    "password": conf.get("password", "2001o326"),
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
            logger.error(f"[Draw] ComfyUI 登录异常: {e}")
        return False

    async def run(
        self,
        positive: str,
        negative: str,
        resolution: str,
        lora_cfg: dict[str, Any],
    ) -> bytes | None:
        conf = self.config.get("comfy", {})
        host, port = conf.get("host", "127.0.0.1"), conf.get("port", 24603)
        base_url = f"http://{host}:{port}"
        dim = ALLOWED_RESOLUTIONS.get(resolution, ALLOWED_RESOLUTIONS["1024x1024"])

        workflow = {
            "1": {
                "inputs": {
                    "unet_name": "silvermoonmixAnimaEvolved_v2329B_3223975.safetensors",
                    "weight_dtype": "default",
                },
                "class_type": "UNETLoader",
            },
            "3": {
                "inputs": {
                    "clip_name": "qwen_3_06b_base.safetensors",
                    "type": "stable_diffusion",
                    "device": "default",
                },
                "class_type": "CLIPLoader",
            },
            "5": {
                "inputs": {"vae_name": "merged_anima&hdr_vae_001.safetensors"},
                "class_type": "VAELoader",
            },
            "6": {
                "inputs": {
                    "tile_size": 512,
                    "overlap": 64,
                    "temporal_size": 64,
                    "temporal_overlap": 8,
                    "samples": ["11", 0],
                    "vae": ["5", 0],
                },
                "class_type": "VAEDecodeTiled",
            },
            "10": {
                "inputs": {
                    "__lm_autocomplete_meta_text": {
                        "version": 1,
                        "textWidgetName": "text",
                    },
                    "text": lora_cfg.get("text", ""),
                    "loras": {"__value__": lora_cfg.get("loras", [])},
                    "model": ["1", 0],
                    "clip": ["3", 0],
                },
                "class_type": "Lora Loader (LoraManager)",
            },
            "11": {
                "inputs": {
                    "seed": random.randint(10000000, 999999999999),
                    "steps": 8,
                    "cfg": 1,
                    "sampler_name": "euler",
                    "scheduler": "sgm_uniform",
                    "denoise": 1,
                    "model": ["10", 0],
                    "positive": ["12", 0],
                    "negative": ["16", 0],
                    "latent_image": ["18", 0],
                },
                "class_type": "KSampler",
            },
            "12": {
                "inputs": {"text": ["13", 0], "clip": ["10", 1]},
                "class_type": "CLIPTextEncode",
            },
            "13": {
                "inputs": {
                    "string_a": ["10", 2],
                    "string_b": ["15", 0],
                    "delimiter": "",
                },
                "class_type": "StringConcatenate",
            },
            "15": {
                "inputs": {"value": positive},
                "class_type": "PrimitiveStringMultiline",
            },
            "16": {
                "inputs": {"text": negative, "clip": ["10", 1]},
                "class_type": "CLIPTextEncode",
            },
            "17": {"inputs": {"images": ["6", 0]}, "class_type": "PreviewImage"},
            "18": {
                "inputs": {
                    "width": dim["width"],
                    "height": dim["height"],
                    "batch_size": 1,
                },
                "class_type": "EmptyLatentImage",
            },
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            headers = {"Origin": base_url, "Referer": f"{base_url}/"}
            if not self.cookie:
                if not await self.login(client):
                    return None
            headers["Cookie"] = self.cookie

            try:
                stats = await client.get(
                    f"{base_url}/system_stats", headers=headers, timeout=3.0
                )
                if stats.status_code in (401, 403):
                    if await self.login(client):
                        headers["Cookie"] = self.cookie
                    else:
                        return None
            except Exception:
                return None

            res = await client.post(
                f"{base_url}/prompt", headers=headers, json={"prompt": workflow}
            )
            if res.status_code != 200:
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
                    imgs = (
                        data.get(prompt_id, {})
                        .get("outputs", {})
                        .get("17", {})
                        .get("images", [])
                    )
                    if imgs:
                        img_info = imgs[0]
                        view_url = f"{base_url}/view?filename={img_info['filename']}&subfolder={img_info['subfolder']}&type={img_info['type']}"
                        img_res = await client.get(view_url, headers=headers)
                        if img_res.status_code == 200:
                            return img_res.content
        return None
