import base64
import re

import httpx

from ..core.constants import ALLOWED_RESOLUTIONS


class NaiEngine:
    def __init__(self, config: dict):
        self.config = config

    def update_config(self, config: dict):
        self.config = config

    async def run(self, positive: str, negative: str, resolution: str) -> bytes | None:
        conf = self.config.get("nai", {})
        dim = ALLOWED_RESOLUTIONS.get(resolution, ALLOWED_RESOLUTIONS["1024x1024"])
        clean_pos = (positive or "1girl").strip(" ,()")
        preset = conf.get("promptPreset", "")
        if preset:
            clean_pos = f"{preset}, {clean_pos}"
        clean_neg = (negative or "").strip(" ,()")

        payload = {
            "prompt": clean_pos,
            "input": clean_pos,
            "model": "nai-diffusion-3",
            "action": "generate",
            "parameters": {
                "width": dim["width"],
                "height": dim["height"],
                "scale": 5,
                "sampler": "k_euler",
                "steps": 28,
                "n_samples": 1,
                "ucPreset": 0,
                "qualityToggle": False,
                "dynamic_thresholding": False,
                "uc": clean_neg,
                "negative_prompt": clean_neg,
            },
        }
        proxy = conf.get("proxy", "").strip() or None
        headers = {
            "Authorization": f"Bearer {conf.get('token', '')}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        }
        async with httpx.AsyncClient(proxy=proxy, timeout=90.0) as client:
            res = await client.post(conf.get("endpoint"), json=payload, headers=headers)
            if res.status_code == 200:
                data = res.json()
                b64 = (
                    data.get("data", [{}])[0].get("b64_json")
                    or data.get("image")
                    or data.get("output")
                )
                if b64:
                    b64 = re.sub(r"^data:image/\w+;base64,", "", b64)
                    return base64.b64decode(b64)
                if data.get("data", [{}])[0].get("url"):
                    img_res = await client.get(data["data"][0]["url"])
                    return img_res.content
        return None
