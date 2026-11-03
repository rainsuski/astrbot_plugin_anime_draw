import io

from PIL import Image

from astrbot.api import logger


class ImageProcessor:
    @staticmethod
    def clean_image_metadata(raw_bytes: bytes) -> bytes:
        """剥离图像元数据并重封装为干净的 PNG"""
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                out_io = io.BytesIO()
                img.convert("RGB").save(out_io, format="PNG")
                return out_io.getvalue()
        except Exception as e:
            logger.warning(f"[Draw] Pillow 洗元数据失败: {e}")
            return raw_bytes

    @staticmethod
    def compress_for_vision_model(raw_bytes: bytes) -> bytes:
        """为大模型视觉感知压缩生成缩略图"""
        try:
            with Image.open(io.BytesIO(raw_bytes)) as img:
                img = img.convert("RGB")
                img.thumbnail((768, 768), Image.Resampling.LANCZOS)
                out_io = io.BytesIO()
                img.save(out_io, format="JPEG", quality=65)
                return out_io.getvalue()
        except Exception as e:
            logger.error(f"[Draw] 压缩模型预览图异常: {e}")
            return raw_bytes
