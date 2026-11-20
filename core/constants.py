# astrbot_plugin_anime_draw/core/constants.py
# 静态常量、分辨率字典与内置兜底预设定义

import json

# ================= 严格限定分辨率枚举 =================
ALLOWED_RESOLUTIONS: dict[str, dict[str, int]] = {
    "1024x1024": {"width": 1024, "height": 1024},
    "1536x640": {"width": 1536, "height": 640},
    "1344x768": {"width": 1344, "height": 768},
    "1216x832": {"width": 1216, "height": 832},
    "1152x896": {"width": 1152, "height": 896},
    "896x1152": {"width": 896, "height": 1152},
    "832x1216": {"width": 832, "height": 1216},
    "768x1344": {"width": 768, "height": 1344},
    "640x1536": {"width": 640, "height": 1536},
}

# ================= 兜底默认 LoRA 预设 =================
FALLBACK_LORA_PRESET: dict[str, str] = {
    "name": "default",
    "workflows": "default",
    "text": (
        "<lora:anima-turbo-lora-v0.2:1.00> "
        "<lora:anima/style/far off in the morning haze024064645510a_30:1.00> "
        "<lora:anima/style/tsubonari_8-step00003000:0.40> "
        "<lora:anima/style/D4C:0.60> "
        "<lora:anima/细节增强/eyedetailer_anima_v1:0.60>"
    ),
    "description": "半写实二次元插画风格",
    "tags": "二次元, 通用",
    "scenarios": "通用",
}

# ================= 兜底默认工作流模板（兼容 Anima 架构） =================
_FALLBACK_WORKFLOW_TEMPLATE: dict[str, object] = {
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
            "text": "%LORA_TEXT%",
            "loras": {"__value__": "%LORAS_JSON%"},
            "model": ["1", 0],
            "clip": ["3", 0],
        },
        "class_type": "Lora Loader (LoraManager)",
    },
    "11": {
        "inputs": {
            "seed": "%SEED%",
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
        "inputs": {"value": "%POSITIVE%"},
        "class_type": "PrimitiveStringMultiline",
    },
    "16": {
        "inputs": {"text": "%NEGATIVE%", "clip": ["10", 1]},
        "class_type": "CLIPTextEncode",
    },
    "17": {
        "inputs": {"images": ["6", 0]},
        "class_type": "PreviewImage",
    },
    "18": {
        "inputs": {
            "width": "%WIDTH%",
            "height": "%HEIGHT%",
            "batch_size": 1,
        },
        "class_type": "EmptyLatentImage",
    },
}

FALLBACK_WORKFLOW_PRESET: dict[str, str] = {
    "name": "default",
    "description": "内置默认二次元插画工作流（Anima 架构）",
    "default_lora": "default",
    "workflow_json": json.dumps(
        _FALLBACK_WORKFLOW_TEMPLATE, ensure_ascii=False, indent=2
    ),
}
