# ================= 严格限定分辨率枚举 =================
ALLOWED_RESOLUTIONS = {
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
FALLBACK_LORA_PRESET = {
    "name": "default",
    "text": "<lora:anima-turbo-lora-v0.2:1.00> <lora:anima/style/far off in the morning haze024064645510a_30:1.00> <lora:anima/style/tsubonari_8-step00003000:0.40> <lora:anima/style/D4C:0.60> <lora:anima/细节增强/eyedetailer_anima_v1:0.60>",
    "description": "半写实二次元插画风格",
    "tags": "二次元, 通用",
    "scenarios": "通用",
}
