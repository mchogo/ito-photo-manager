"""画像処理ユーティリティ

アップロード画像のリサイズ・JPEG圧縮を行う。
Excelに貼り付ける際のファイルサイズ抑制が目的。
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ExifTags

logger = logging.getLogger(__name__)

# Excel挿入用のデフォルト幅（ピクセル）
DEFAULT_MAX_WIDTH = 800
# JPEG圧縮品質
JPEG_QUALITY = 85


def resize_image(
    image_bytes: bytes,
    max_width: int = DEFAULT_MAX_WIDTH,
    quality: int = JPEG_QUALITY,
) -> bytes:
    """画像をリサイズし、JPEG形式で圧縮して返す

    - EXIF情報の回転を適用
    - アスペクト比を維持しつつmax_width以下にリサイズ
    - JPEG品質を指定して圧縮
    """
    img = Image.open(io.BytesIO(image_bytes))

    # EXIF回転情報を適用（スマホ撮影時の回転補正）
    img = _apply_exif_rotation(img)

    # リサイズ（幅がmax_widthを超える場合のみ）
    if img.width > max_width:
        ratio = max_width / img.width
        new_height = int(img.height * ratio)
        img = img.resize((max_width, new_height), Image.Resampling.LANCZOS)
        logger.info("Resized image: %dx%d -> %dx%d", img.width, img.height, max_width, new_height)

    # RGBA → RGB 変換（JPEG保存のため）
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")

    # JPEG圧縮
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def _apply_exif_rotation(img: Image.Image) -> Image.Image:
    """EXIF情報に基づいて画像を正しい向きに回転する"""
    try:
        exif = img.getexif()
        if not exif:
            return img

        orientation_key = None
        for key, val in ExifTags.TAGS.items():
            if val == "Orientation":
                orientation_key = key
                break

        if orientation_key is None or orientation_key not in exif:
            return img

        orientation = exif[orientation_key]
        rotations = {
            3: 180,
            6: 270,
            8: 90,
        }
        if orientation in rotations:
            img = img.rotate(rotations[orientation], expand=True)
    except Exception:
        logger.warning("Failed to read EXIF orientation, skipping rotation")
    return img
