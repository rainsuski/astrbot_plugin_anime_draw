# astrbot_plugin_anime_draw/core/workflow.py
# 工作流预设管理器（负责工作流配置解析、默认预设维护与动态占位符注入渲染）

import json
import re
from typing import Any

from .constants import FALLBACK_WORKFLOW_PRESET


class WorkflowPresetManager:
    """工作流预设管理器：管理 ComfyUI 多底模工作流模板及动态占位符渲染。"""

    def __init__(self, config: dict[str, Any]):
        self.config = config

    def update_config(self, config: dict[str, Any]) -> None:
        self.config = config

    def _get_presets_pool(self) -> list[dict[str, Any]]:
        """获取有效工作流预设池（配置为空时自动使用内置兜底预设）。"""
        raw_list = self.config.get("workflow_presets", [])
        if not isinstance(raw_list, list):
            return [FALLBACK_WORKFLOW_PRESET]

        valid_list = [
            item
            for item in raw_list
            if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
        return valid_list if valid_list else [FALLBACK_WORKFLOW_PRESET]

    def get_default_preset_name(self) -> str:
        """获取当前全局默认工作流名称。"""
        name = str(self.config.get("current_workflow_preset", "default")).strip()
        return name or "default"

    @staticmethod
    def _normalize_preset(item: dict[str, Any]) -> dict[str, str]:
        workflow_json = str(item.get("workflow_json", "")).strip()
        if not workflow_json:
            workflow_json = FALLBACK_WORKFLOW_PRESET["workflow_json"]

        return {
            "name": str(item.get("name", "default")).strip(),
            "description": str(item.get("description", "")).strip(),
            "default_lora": str(item.get("default_lora", "")).strip(),
            "workflow_json": workflow_json,
        }

    def find_preset(self, preset_name: str) -> dict[str, str] | None:
        """按名称精确查找工作流预设，不存在时返回 None。"""
        target = preset_name.strip()
        if not target:
            return None

        for item in self._get_presets_pool():
            if str(item.get("name", "")).strip() == target:
                return self._normalize_preset(item)
        return None

    def get_preset(self, preset_name: str | None = None) -> dict[str, str] | None:
        """获取工作流预设：
        - 若显式传入 preset_name：精确查找，找不到返回 None；
        - 若 preset_name 为空：使用全局默认 current_workflow_preset，若找不到则回退池中第一项或内置兜底。
        """
        if preset_name is not None and preset_name.strip():
            return self.find_preset(preset_name)

        pool = self._get_presets_pool()
        default_name = self.get_default_preset_name()
        for item in pool:
            if str(item.get("name", "")).strip() == default_name:
                return self._normalize_preset(item)

        return self._normalize_preset(pool[0])

    def list_presets_meta(self) -> list[dict[str, Any]]:
        """列出所有可用工作流预设的元数据。"""
        pool = self._get_presets_pool()
        active_default = self.get_default_preset_name()

        # 若全局默认名称未命中任何预设，则将首项标记为当前实际生效的默认项
        has_exact_default = any(
            str(item.get("name", "")).strip() == active_default for item in pool
        )

        meta_list: list[dict[str, Any]] = []
        for idx, item in enumerate(pool):
            norm = self._normalize_preset(item)
            is_default = (
                norm["name"] == active_default if has_exact_default else idx == 0
            )
            meta_list.append(
                {
                    "name": norm["name"],
                    "description": norm["description"] or "无描述",
                    "default_lora": norm["default_lora"],
                    "is_default": is_default,
                }
            )
        return meta_list

    @staticmethod
    def _escape_json_string_content(value: str) -> str:
        """将字符串安全转义为可嵌入 JSON 双引号内部的合法片段（支持引号、换行及反斜杠）。"""
        return json.dumps(value or "", ensure_ascii=False)[1:-1]

    @classmethod
    def render_workflow(
        cls,
        workflow_json: str,
        positive: str,
        negative: str,
        width: int,
        height: int,
        seed: int,
        lora_text: str = "",
        loras_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """执行工作流动态占位符全局替换并反序列化为 ComfyUI API Dict。

        支持占位符：
        - %POSITIVE%, %NEGATIVE%, %LORA_TEXT%（字符串转义注入）
        - %WIDTH%, %HEIGHT%, %SEED%（整数类型注入，兼容带引号与裸宏写法）
        - %LORAS_JSON%（JSON Array 注入，兼容带引号与裸宏写法）
        """
        raw_template = (workflow_json or "").strip()
        if not raw_template:
            raw_template = FALLBACK_WORKFLOW_PRESET["workflow_json"]

        escaped_pos = cls._escape_json_string_content(positive)
        escaped_neg = cls._escape_json_string_content(negative)
        escaped_lora_text = cls._escape_json_string_content(lora_text)
        loras_json_str = json.dumps(loras_list or [], ensure_ascii=False)

        rendered = raw_template

        # 1. 字符串型占位符替换
        rendered = rendered.replace("%POSITIVE%", escaped_pos)
        rendered = rendered.replace("%NEGATIVE%", escaped_neg)
        rendered = rendered.replace("%LORA_TEXT%", escaped_lora_text)

        # 2. 数值与 JSON 数组型占位符替换（优先剥离作为独立 JSON 字符串写法的包裹双引号）
        rendered = re.sub(r'"%WIDTH%"', str(int(width)), rendered)
        rendered = rendered.replace("%WIDTH%", str(int(width)))

        rendered = re.sub(r'"%HEIGHT%"', str(int(height)), rendered)
        rendered = rendered.replace("%HEIGHT%", str(int(height)))

        rendered = re.sub(r'"%SEED%"', str(int(seed)), rendered)
        rendered = rendered.replace("%SEED%", str(int(seed)))

        rendered = re.sub(r'"%LORAS_JSON%"', lambda _m: loras_json_str, rendered)
        rendered = rendered.replace("%LORAS_JSON%", loras_json_str)

        try:
            parsed = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise ValueError(
                f"工作流 JSON 渲染后格式非法: {e.msg} (行 {e.lineno}, 列 {e.colno})"
            ) from e

        if not isinstance(parsed, dict):
            raise ValueError("工作流 JSON 根节点必须为字典对象")

        return parsed
