# astrbot_plugin_anime_draw/core/lora.py
# LoRA 预设管理器（负责配置解析、节点组装以及与工作流的多对多兼容性校验）

import re
from typing import Any

from .constants import FALLBACK_LORA_PRESET

# 支持路径名、负数/浮点数权重，匹配格式形如：
# <lora:name:model> 或 <lora:name:clip:model>
LORA_TAG_REGEX = re.compile(
    r"<lora:\s*([^:>]+?)\s*(?::([-+]?[0-9]*\.?[0-9]+))?(?::([-+]?[0-9]*\.?[0-9]+))?\s*>"
)


class LoraPresetManager:
    """LoRA 预设管理器：负责读取 LoRA 预设、解析节点并校验其与工作流的绑定归属。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def update_config(self, config: dict[str, Any]) -> None:
        self.config = config

    def _get_presets_pool(self) -> list[dict[str, Any]]:
        raw_list = self.config.get("lora_presets", [])
        if not isinstance(raw_list, list):
            return [FALLBACK_LORA_PRESET]

        valid_list = [
            item
            for item in raw_list
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        return valid_list if valid_list else [FALLBACK_LORA_PRESET]

    @staticmethod
    def parse_workflows(raw_workflows: Any) -> list[str]:
        """解析 LoRA 绑定的适用工作流名称列表（支持逗号/分号/空白分隔字符串或列表）。"""
        if raw_workflows is None:
            return ["default"]
        if isinstance(raw_workflows, list):
            return [str(w).strip() for w in raw_workflows if str(w).strip()]
        text = str(raw_workflows).strip()
        if not text:
            return []
        return [w.strip() for w in re.split(r"[,;\s]+", text) if w.strip()]

    @staticmethod
    def parse_lora_text_to_nodes(text: str) -> list[dict[str, Any]]:
        """根据以下两种格式自动解析生成 ComfyUI Node 10 专用的 loras 列表：
        - 双参数：<lora:name:clip权重:模型权重>
        - 单参数：<lora:name:模型权重> (clip权重同步继承模型权重)
        """
        matches = LORA_TAG_REGEX.findall(text or "")
        loras: list[dict[str, Any]] = []
        for match in matches:
            lora_name = match[0].strip()
            arg1 = match[1]
            arg2 = match[2]

            if arg2:
                clip_strength = float(arg1)
                model_strength = float(arg2)
            elif arg1:
                model_strength = float(arg1)
                clip_strength = model_strength
            else:
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

    @classmethod
    def _build_preset_dict(cls, item: dict[str, Any]) -> dict[str, Any]:
        text = str(item.get("text", ""))
        workflows = cls.parse_workflows(item.get("workflows"))
        return {
            "name": str(item.get("name", "unknown")).strip(),
            "workflows": workflows,
            "text": text,
            "loras": cls.parse_lora_text_to_nodes(text),
            "description": str(item.get("description", "")),
            "tags": str(item.get("tags", "")),
            "scenarios": str(item.get("scenarios", "")),
        }

    def find_preset(self, preset_name: str) -> dict[str, Any] | None:
        """按名称精确查找 LoRA 预设，不存在则返回 None。"""
        target = preset_name.strip()
        if not target:
            return None

        for item in self._get_presets_pool():
            if str(item.get("name", "")).strip() == target:
                return self._build_preset_dict(item)
        return None

    @staticmethod
    def is_compatible_with_workflow(preset: dict[str, Any], workflow_name: str) -> bool:
        """校验目标 LoRA 预设是否允许在指定工作流中使用（强绑定白名单）。"""
        wf = workflow_name.strip()
        if not wf:
            return False
        allowed_workflows = preset.get("workflows", [])
        return wf in allowed_workflows

    def get_preset(self, preset_name: str | None = None) -> dict[str, Any]:
        """获取目标预设（优先级：指定名称 > 当前全局配置 > 列表第一项 > 内置兜底）。"""
        pool = self._get_presets_pool()
        target_name = (
            preset_name.strip()
            if preset_name and preset_name.strip()
            else str(self.config.get("current_lora_preset", "default")).strip()
        )

        for item in pool:
            if str(item.get("name", "")).strip() == target_name:
                return self._build_preset_dict(item)

        return self._build_preset_dict(pool[0])

    def list_presets_meta(self) -> list[dict[str, str]]:
        """获取所有预设的风格描述、绑定工作流、标签与适用场景元数据，供 LLM 或查询工具使用。"""
        pool = self._get_presets_pool()
        meta_list: list[dict[str, str]] = []
        for p in pool:
            workflows = self.parse_workflows(p.get("workflows"))
            meta_list.append(
                {
                    "name": str(p.get("name", "")).strip(),
                    "workflows": ", ".join(workflows) if workflows else "未绑定",
                    "description": str(p.get("description", "无描述")),
                    "tags": str(p.get("tags", "二次元")),
                    "scenarios": str(p.get("scenarios", "插画")),
                }
            )
        return meta_list
