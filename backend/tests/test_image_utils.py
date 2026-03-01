"""画像処理ユーティリティのテスト"""

import io

from PIL import Image

from image_utils import resize_image


def _create_test_image(width: int, height: int, fmt: str = "JPEG") -> bytes:
    """テスト用画像を生成"""
    img = Image.new("RGB", (width, height), color="red")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_resize_large_image():
    """大きい画像が800px幅にリサイズされる"""
    original = _create_test_image(1600, 1200)
    result = resize_image(original, max_width=800)
    img = Image.open(io.BytesIO(result))
    assert img.width == 800
    assert img.height == 600  # アスペクト比維持


def test_small_image_not_resized():
    """小さい画像はリサイズされない"""
    original = _create_test_image(400, 300)
    result = resize_image(original, max_width=800)
    img = Image.open(io.BytesIO(result))
    assert img.width == 400
    assert img.height == 300


def test_output_is_jpeg():
    """出力が常にJPEGである"""
    png = _create_test_image(100, 100, fmt="PNG")
    result = resize_image(png)
    # JPEGマジックバイト
    assert result[:2] == b"\xff\xd8"


def test_rgba_to_rgb():
    """RGBA画像がRGBに変換される"""
    img = Image.new("RGBA", (100, 100), color=(255, 0, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    result = resize_image(buf.getvalue())
    out = Image.open(io.BytesIO(result))
    assert out.mode == "RGB"


def test_compression_reduces_size():
    """高品質画像が圧縮でファイルサイズが小さくなる"""
    large = _create_test_image(1000, 1000)
    compressed = resize_image(large, max_width=1000, quality=50)
    assert len(compressed) < len(large)
