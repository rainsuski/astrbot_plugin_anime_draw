import re
from typing import Any

from .constants import FALLBACK_LORA_PRESET

# 支持路径名、负数/浮点数权重，匹配格式形如：
# <lora:name:model> 或 <lora:name:clip:model>
LORA_TAG_REGEX = re.compile(
    r"<lora:\s*([^:>]+?)\s*(?::([-+]?[0-9]*\.?[0-9]+))?(?::([-+]?[0-9]*\.?[0-9]+))?\s*>"
)


class LoraPresetManager:
    """LoRA 预设管理器：负责从插件配置读取预设、根据 text 自动解析组装 loras 结构体"""

    def __init__(self, config: dict):
        self.config = config

    def update_config(self, config: dict):
        self.config = config

    @staticmethod
    def parse_lora_text_to_nodes(text: str) -> list[dict[str, Any]]:
        """
        根据以下两种格式自动解析生成 ComfyUI Node 10 专用的 loras 列表：
        - 双参数：<lora:name:clip权重:模型权重>
        - 单参数：<lora:name:模型权重> (clip权重同步继承模型权重)
        """
        matches = LORA_TAG_REGEX.findall(text or "")
        loras = []
        for match in matches:
            lora_name = match[0].strip()
            arg1 = match[1]
            arg2 = match[2]

            if arg2:
                # 提供了两个权重：<lora:name:clip权重:模型权重>
                clip_strength = float(arg1)
                model_strength = float(arg2)
            elif arg1:
                # 只提供了单权重：<lora:name:模型权重>，clip权重默认与模型权重相同
                model_strength = float(arg1)
                clip_strength = model_strength
            else:
                # 兜底：未显式指定权重
                model_strength = 1.0
                clip_strength = 1.0

            loras.append(
                {
                    "name": lora_name,
                    "strength": model_strength,
                    "active": True,
                    "expanded": False,
                    "clipStrength": clip_strength,
                    "selected": False,
                    "locked": False,
                }
            )
        return loras

    def get_preset(self, preset_name: str | None = None) -> dict[str, Any]:
        """获取目标预设（优先级：指定名称 > 当前全局配置 > 列表第一项 > 内置兜底）"""
        presets_list = self.config.get("lora_presets", [])
        if not presets_list:
            presets_list = [FALLBACK_LORA_PRESET]

        target_name = (
            preset_name.strip()
            if preset_name and preset_name.strip()
            else self.config.get("current_lora_preset", "default").strip()
        )

        matched_item = None
        for item in presets_list:
            if item.get("name", "").strip() == target_name:
                matched_item = item
                break

        if not matched_item:
            matched_item = presets_list[0] if presets_list else FALLBACK_LORA_PRESET

        text = matched_item.get("text", "")
        return {
            "name": matched_item.get("name", "unknown"),
            "text": text,
            "loras": self.parse_lora_text_to_nodes(text),
            "description": matched_item.get("description", ""),
            "tags": matched_item.get("tags", ""),
            "scenarios": matched_item.get("scenarios", ""),
        }

    def list_presets_meta(self) -> list[dict[str, str]]:
        """获取所有预设的风格描述、标签与适用场景元数据，供 LLM 或查询工具使用"""
        presets_list = self.config.get("lora_presets", [])
        if not presets_list:
            presets_list = [FALLBACK_LORA_PRESET]

        meta_list = []
        for p in presets_list:
            meta_list.append(
                {
                    "name": p.get("name", ""),
                    "description": p.get("description", "无描述"),
                    "tags": p.get("tags", "二次元"),
                    "scenarios": p.get("scenarios", "插画"),
                }
            )
        return meta_list
