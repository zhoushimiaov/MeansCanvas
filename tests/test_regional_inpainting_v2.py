"""Unit and regression tests for Regional Inpainting 2.0 (core/image_edit.py)."""
import asyncio
import base64
import io
import os
import pytest
from PIL import Image, ImageDraw

from core.image_edit import (
    synthesize_feathered_mask,
    decode_mask_data_url,
    composite_regional_edit,
    annotate_visual_region_guide,
    execute_canvas_edit,
)


def _make_png_bytes(w: int, h: int, color: tuple) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="PNG")
    return buf.getvalue()


def _make_mask_data_url(w: int, h: int, rect: tuple) -> str:
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rectangle(rect, fill=255)
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def test_decode_mask_data_url_resizes_and_feathers():
    data_url = _make_mask_data_url(100, 100, (20, 20, 60, 60))
    mask_bytes = decode_mask_data_url(data_url, target_w=200, target_h=200, feather_radius=4)
    with Image.open(io.BytesIO(mask_bytes)) as m:
        assert m.size == (200, 200)
        assert m.mode == "L"
        # Center of scaled rect (80, 80) should be white (255)
        assert m.getpixel((80, 80)) > 240
        # Far corner (5, 5) should be untouched black (0)
        assert m.getpixel((5, 5)) == 0


def test_composite_regional_edit_preserves_outside_pixels_bit_for_bit():
    orig = _make_png_bytes(120, 120, (10, 20, 30))
    # Simulate an AI model that regenerated the whole image and drifted background color to (250, 240, 230)
    edited = _make_png_bytes(240, 240, (250, 240, 230))
    mask_bytes = synthesize_feathered_mask(
        120, 120, {"x": 40, "y": 40, "width": 40, "height": 40}, feather_radius=2
    )
    merged_bytes = composite_regional_edit(orig, edited, mask_bytes)
    with Image.open(io.BytesIO(merged_bytes)).convert("RGB") as out_img:
        assert out_img.size == (120, 120)
        # Outside the region + feather halo, pixels must be 100% identical to orig (10, 20, 30)
        assert out_img.getpixel((5, 5)) == (10, 20, 30)
        assert out_img.getpixel((115, 115)) == (10, 20, 30)
        # Inside the core of the region (60, 60), pixels must come from edited (250, 240, 230)
        assert out_img.getpixel((60, 60)) == (250, 240, 230)


def test_annotate_visual_region_guide_marks_bbox_without_changing_dimensions():
    orig = _make_png_bytes(100, 100, (0, 0, 0))
    mask_bytes = synthesize_feathered_mask(
        100, 100, {"x": 20, "y": 20, "width": 40, "height": 40}, feather_radius=0
    )
    guided = annotate_visual_region_guide(orig, mask_bytes)
    with Image.open(io.BytesIO(guided)).convert("RGB") as img:
        assert img.size == (100, 100)
        # Interior center remains black
        assert img.getpixel((40, 40)) == (0, 0, 0)
        # Bounding contour at (20, 30) is highlighted
        assert img.getpixel((20, 30)) != (0, 0, 0)


def test_execute_canvas_edit_uses_cloud_dispatcher_and_composites_mask(tmp_path, monkeypatch):
    import core.providers as CP
    monkeypatch.setattr(CP, "OUTPUT_DIR", str(tmp_path))

    orig_bytes = _make_png_bytes(100, 100, (12, 34, 56))
    src_file = tmp_path / "src.png"
    src_file.write_bytes(orig_bytes)

    ref_bytes = _make_png_bytes(60, 60, (90, 80, 70))
    ref_data_url = "data:image/png;base64," + base64.b64encode(ref_bytes).decode("ascii")
    mask_data_url = _make_mask_data_url(100, 100, (30, 30, 70, 70))

    captured_dispatch = {}

    async def fake_cloud_dispatcher(spec):
        captured_dispatch.update(spec)
        # Return an all-white image of size 100x100
        return _make_png_bytes(100, 100, (255, 255, 255))

    monkeypatch.setattr(
        "core.media.output_file_from_url",
        lambda u: str(src_file) if u == "/output/src.png" else "",
    )
    monkeypatch.setattr(
        CP,
        "load_api_providers",
        lambda: [{"id": "gemini-cli", "name": "Gemini CLI", "protocol": "gemini-cli", "base_url": "", "enabled": True, "image_models": ["gemini-2.5-flash-image"]}],
    )

    res = asyncio.run(
        execute_canvas_edit(
            {
                "source_image_url": "/output/src.png",
                "prompt": "替换为参考图中的纹理",
                "provider_id": "gemini-cli",
                "model": "gemini-2.5-flash-image",
                "mask_data_url": mask_data_url,
                "reference_image_url": ref_data_url,
                "feather_radius": 2,
                "preserve_outside": True,
            },
            cloud_dispatcher=fake_cloud_dispatcher,
        )
    )

    assert res["output_url"].startswith("/output/reve_")
    assert captured_dispatch["provider"]["id"] == "gemini-cli"
    assert len(captured_dispatch["reference_images_bytes"]) == 1
    out_file = tmp_path / os.path.basename(res["output_url"])
    with Image.open(out_file).convert("RGB") as final_img:
        # Outside mask (5, 5) preserved as (12, 34, 56)
        assert final_img.getpixel((5, 5)) == (12, 34, 56)
        # Inside mask (50, 50) updated to (255, 255, 255)
        assert final_img.getpixel((50, 50)) == (255, 255, 255)
