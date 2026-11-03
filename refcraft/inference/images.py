"""Original reference JPEG preprocessing (crop, quality ladder, byte budget)."""

import base64
import io
import os
import logging
from typing import Optional
from PIL import Image

logger = logging.getLogger(__name__)


def load_image_base64(
    path: str, quality: int = 80, max_bytes: int = 1500000, max_height: Optional[int] = None
) -> Optional[str]:
    if not os.path.exists(path):
        logger.warning(f"Image not found: {path}")
        return None
    try:
        img = Image.open(path).convert("RGB")
        if max_height and img.height > max_height:
            img = img.crop((0, 0, img.width, max_height))
        for q in [quality, 60, 40, 20]:
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=q)
            data = buf.getvalue()
            if len(data) <= max_bytes:
                return base64.b64encode(data).decode("utf-8")
        scale = (max_bytes / len(data)) ** 0.5
        new_w = max(100, int(img.width * scale))
        new_h = max(100, int(img.height * scale))
        img = img.resize((new_w, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=20)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as e:
        logger.error(f"Failed to load image {path}: {e}")
        return None
