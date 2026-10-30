"""Core smart mask synthesis and edit utilities for Reve-style image editing."""

import io
from typing import Optional, Dict, Any, List
from PIL import Image, ImageDraw, ImageFilter

# data: 内嵌图片的字符上限。base64 字符数 ~= 解码字节数 * 4/3，
# 32MB 字符 ~= 24MB 原始像素，与 HTTP 分支 50MB 下载上限同一量级。
MAX_DATA_URL_CHARS = 32 * 1024 * 1024


def synthesize_feathered_mask(
    orig_w: int,
    orig_h: int,
    region: Optional[Dict[str, Any]],
    feather_radius: int = 6,
) -> bytes:
    """Synthesizes a grayscale PNG mask for the specified bounding box region.

    If region is None or dimensions are non-positive, returns b"".
    Otherwise, paints white (255) in the bounded box with a smooth Gaussian feather
    around edges, and black (0) outside the region.
    """
    if not region:
        return b""

    try:
        rw = int(region.get("width", 0))
        rh = int(region.get("height", 0))
    except (TypeError, ValueError):
        return b""

    if rw <= 0 or rh <= 0:
        return b""

    orig_w = max(1, int(orig_w))
    orig_h = max(1, int(orig_h))

    try:
        rx = int(region.get("x", 0))
        ry = int(region.get("y", 0))
    except (TypeError, ValueError):
        rx, ry = 0, 0

    # Clamp region coordinates to image boundaries
    x0 = max(0, min(orig_w, rx))
    y0 = max(0, min(orig_h, ry))
    x1 = max(0, min(orig_w, rx + rw))
    y1 = max(0, min(orig_h, ry + rh))

    mask = Image.new("L", (orig_w, orig_h), 0)
    if x1 > x0 and y1 > y0:
        draw = ImageDraw.Draw(mask)
        draw.rectangle([x0, y0, x1, y1], fill=255)

        if feather_radius and feather_radius > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(radius=int(feather_radius)))

    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def _detect_image_format(data: bytes) -> tuple:
    """探测图片真实格式，返回 (扩展名, MIME)。此前固定按 PNG 上传，
    JPEG/WebP 源会被严格网关按 magic byte 拒绝。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    return "png", "image/png"


def to_alpha_mask_png(grayscale_mask_png: bytes) -> bytes:
    """把 8 位灰度遮罩转换为 OpenAI /images/edits 要求的 RGBA 遮罩。

    该端点规定 mask 必须含 alpha 通道，且「透明区域为要重绘的部分」。
    我们合成的是「选区内为白（要修改）」的灰度图，因此取 alpha = 255 - 灰度，
    使选区内变透明（重绘）、选区外不透明（保留）。
    """
    with Image.open(io.BytesIO(grayscale_mask_png)) as src:
        gray = src.convert("L")
        rgba = Image.new("RGBA", gray.size, (255, 255, 255, 0))
        rgba.putalpha(Image.eval(gray, lambda v: 255 - v))
        buf = io.BytesIO()
        rgba.save(buf, format="PNG")
        return buf.getvalue()


def _full_canvas_mask(source_image_bytes: bytes) -> bytes:
    """合成一张覆盖整图的遮罩，用于「未框选 = 全图重绘」语义。"""
    with Image.open(io.BytesIO(source_image_bytes)) as src:
        w, h = src.size
    mask = Image.new("L", (max(1, w), max(1, h)), 255)
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def decode_mask_data_url(
    mask_data_url: str,
    target_w: int,
    target_h: int,
    feather_radius: int = 12,
) -> bytes:
    """Decodes a base64 data:image/* mask URL into a (target_w, target_h) grayscale PNG mask."""
    import base64

    text = str(mask_data_url or "").strip()
    if not text or not text.startswith("data:image/") or "," not in text:
        return b""
    if len(text) > MAX_DATA_URL_CHARS:
        raise ValueError("蒙版数据过大")
    _, b64 = text.split(",", 1)
    raw = base64.b64decode(b64, validate=True)
    target_w = max(1, int(target_w))
    target_h = max(1, int(target_h))
    with Image.open(io.BytesIO(raw)) as img:
        # Support both RGBA (drawn on transparent canvas) and L/RGB masks
        if "A" in img.getbands():
            alpha = img.getchannel("A")
            gray = img.convert("L")
            # Take max of luminance and alpha so any painted stroke registers as white
            mask = Image.fromarray(
                bytes(max(g, a) for g, a in zip(gray.tobytes(), alpha.tobytes())),
                mode="L",
            ) if gray.size == alpha.size else alpha
        else:
            mask = img.convert("L")
        if mask.size != (target_w, target_h):
            mask = mask.resize((target_w, target_h), Image.Resampling.BILINEAR)
        # Binarize stroke core before feathering so semi-transparent brush strokes become solid 255 inside
        mask = mask.point(lambda p: 255 if p >= 32 else 0)
        if feather_radius and int(feather_radius) > 0:
            mask = mask.filter(ImageFilter.GaussianBlur(radius=min(64, int(feather_radius))))
    buf = io.BytesIO()
    mask.save(buf, format="PNG")
    return buf.getvalue()


def mask_bounding_box(mask_png_bytes: bytes) -> Optional[Dict[str, int]]:
    """Returns {'x', 'y', 'width', 'height'} of the non-zero region in a grayscale mask PNG."""
    if not mask_png_bytes:
        return None
    with Image.open(io.BytesIO(mask_png_bytes)) as m:
        gray = m.convert("L")
        binary = gray.point(lambda p: 255 if p >= 16 else 0)
        bbox = binary.getbbox()
        if not bbox:
            return None
        x0, y0, x1, y1 = bbox
        return {"x": int(x0), "y": int(y0), "width": max(1, int(x1 - x0)), "height": max(1, int(y1 - y0))}


def annotate_visual_region_guide(source_image_bytes: bytes, mask_png_bytes: bytes) -> bytes:
    """Draws a 2px high-contrast coral rectangle around the mask bbox to guide instruction models."""
    bbox = mask_bounding_box(mask_png_bytes)
    if not bbox:
        return source_image_bytes
    with Image.open(io.BytesIO(source_image_bytes)) as src:
        canvas = src.convert("RGBA")
        draw = ImageDraw.Draw(canvas)
        x0, y0 = bbox["x"], bbox["y"]
        x1, y1 = min(canvas.width - 1, x0 + bbox["width"]), min(canvas.height - 1, y0 + bbox["height"])
        for offset in range(2):
            draw.rectangle(
                [max(0, x0 - offset), max(0, y0 - offset), min(canvas.width - 1, x1 + offset), min(canvas.height - 1, y1 + offset)],
                outline=(255, 90, 54, 255),
            )
        buf = io.BytesIO()
        canvas.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()


def composite_regional_edit(orig_bytes: bytes, edited_bytes: bytes, mask_png_bytes: bytes) -> bytes:
    """Alpha-composites edited_bytes inside mask_png_bytes onto orig_bytes, locking outside pixels."""
    if not mask_png_bytes:
        return edited_bytes
    with Image.open(io.BytesIO(orig_bytes)) as orig_im, Image.open(io.BytesIO(edited_bytes)) as edit_im, Image.open(io.BytesIO(mask_png_bytes)) as mask_im:
        orig_rgba = orig_im.convert("RGBA")
        edit_rgba = edit_im.convert("RGBA")
        if edit_rgba.size != orig_rgba.size:
            edit_rgba = edit_rgba.resize(orig_rgba.size, Image.Resampling.LANCZOS)
        mask_l = mask_im.convert("L")
        if mask_l.size != orig_rgba.size:
            mask_l = mask_l.resize(orig_rgba.size, Image.Resampling.BILINEAR)
        merged = Image.composite(edit_rgba, orig_rgba, mask_l)
        buf = io.BytesIO()
        merged.convert("RGB").save(buf, format="PNG")
        return buf.getvalue()


def build_edit_prompt_with_region(
    prompt: str,
    region: Optional[Dict[str, Any]],
    orig_w: int = 1000,
    orig_h: int = 1000,
) -> str:
    """Augments prompt with spatial layout instructions if region is specified."""
    prompt = str(prompt or "").strip()
    if not region:
        return prompt

    try:
        rx = int(region.get("x", 0))
        ry = int(region.get("y", 0))
        rw = int(region.get("width", 0))
        rh = int(region.get("height", 0))
    except (TypeError, ValueError):
        return prompt

    if rw <= 0 or rh <= 0:
        return prompt

    # Calculate rough relative positional descriptors
    cx = rx + rw / 2
    cy = ry + rh / 2
    h_pos = "左侧" if cx < orig_w * 0.35 else "右侧" if cx > orig_w * 0.65 else "中央"
    v_pos = "上方" if cy < orig_h * 0.35 else "下方" if cy > orig_h * 0.65 else "中间"
    pos_desc = f"{v_pos}{h_pos}" if h_pos != "中央" or v_pos != "中间" else "中心区域"

    spatial_hint = f"[针对画面{pos_desc}指定局部区域 (x:{rx}, y:{ry}, w:{rw}, h:{rh}) 进行修改] {prompt}"
    return spatial_hint


def detect_edit_dispatch_mode(provider: Optional[Dict[str, Any]], model: str) -> str:
    """Detects the dispatch mode: 'bfl-fill', 'openai-edits', or 'instruction'."""
    m = str(model or "").lower()
    pid = str((provider or {}).get("id") or "").lower()
    mode_attr = str((provider or {}).get("image_edit_mode") or "").lower()

    if "fill" in m or (pid == "bfl" and "fill" in m):
        return "bfl-fill"
    if mode_attr == "edits" or m in {"dall-e-2", "dalle-2"}:
        return "openai-edits"
    return "instruction"


def prepare_reve_edit_payload(
    provider: Optional[Dict[str, Any]],
    model: str,
    prompt: str,
    source_image_bytes: bytes,
    mask_bytes: Optional[bytes] = None,
    region: Optional[Dict[str, Any]] = None,
    orig_w: Optional[int] = None,
    orig_h: Optional[int] = None,
) -> Dict[str, Any]:
    """Builds the request payload for the appropriate cloud provider dispatch mode."""
    import base64

    provider = provider or {}
    base_url = (provider.get("base_url") or "https://api.openai.com/v1").rstrip("/")
    mode = detect_edit_dispatch_mode(provider, model)
    # 必须传入真实图片尺寸：此前固定按 1000x1000 估算方位，
    # 导致所有小于 1000px 的图把右侧选区误判为「中央」。
    augmented_prompt = build_edit_prompt_with_region(
        prompt, region, orig_w=int(orig_w or 1000), orig_h=int(orig_h or 1000)
    )

    if mode == "bfl-fill":
        endpoint_url = f"{base_url}/{model}" if not base_url.endswith(f"/{model}") else base_url
        b64_image = base64.b64encode(source_image_bytes).decode("ascii")
        # flux-pro-1.0-fill 是 inpainting 端点，空 mask 会被拒绝（422）。
        # 用户未框选时合成一张全白 mask，等价于「整图重绘」。
        if not mask_bytes:
            mask_bytes = _full_canvas_mask(source_image_bytes)
        b64_mask = base64.b64encode(mask_bytes).decode("ascii") if mask_bytes else ""
        return {
            "mode": "bfl-fill",
            "endpoint_url": endpoint_url,
            "json_body": {
                "image": b64_image,
                "mask": b64_mask,
                "prompt": prompt,
            },
            "files": {},
            "form_data": {},
            "is_async_task": True,
        }

    if mode == "openai-edits":
        from core.providers import provider_endpoint_url
        endpoint_url = provider_endpoint_url(provider, "image_edit_endpoint", "/v1/images/edits")
        # 图片的真实格式此前被硬编码为 image.png，JPEG 源会被严格网关按
        # magic byte 拒绝；这里按实际探测到的格式给出 MIME 与扩展名。
        ext, mime = _detect_image_format(source_image_bytes)
        files = {
            "image": (f"image.{ext}", source_image_bytes, mime),
        }
        if mask_bytes:
            # /v1/images/edits 要求 mask 带 alpha 通道（透明处为重绘区），
            # 此前传入的是 8 位灰度 PNG，语义相反且格式不合规。
            files["mask"] = ("mask.png", to_alpha_mask_png(mask_bytes), "image/png")
        return {
            "mode": "openai-edits",
            "endpoint_url": endpoint_url,
            "json_body": {},
            "files": files,
            "form_data": {
                "model": model,
                "prompt": augmented_prompt,
            },
            "is_async_task": False,
        }

    # Instruction mode
    from core.providers import provider_endpoint_url
    endpoint_url = provider_endpoint_url(provider, "image_generation_endpoint", "/v1/images/generations")
    data_url = f"data:image/png;base64,{base64.b64encode(source_image_bytes).decode('ascii')}"
    return {
        "mode": "instruction",
        "endpoint_url": endpoint_url,
        "json_body": {
            "model": model,
            "prompt": augmented_prompt,
            "image": [data_url],
        },
        "files": {},
        "form_data": {},
        "is_async_task": False,
    }


def make_provider_headers(provider: Optional[Dict[str, Any]], api_key: str = "") -> Dict[str, str]:
    """Constructs HTTP request headers for the specified AI provider."""
    from core.providers import provider_env_key_value

    headers = {"Accept": "application/json"}
    pid = str((provider or {}).get("id") or "").lower()
    base_url = str((provider or {}).get("base_url") or "").lower()
    if not api_key:
        api_key = provider_env_key_value(pid) or (provider or {}).get("api_key", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        if "bfl" in pid or "bfl.ml" in base_url:
            headers["x-key"] = api_key
    return headers


def save_output_image_bytes(data: bytes, filename_prefix: str = "reve") -> str:
    """Saves generated image bytes to the system OUTPUT_DIR and returns its URL."""
    import os
    import time
    import uuid
    from core.providers import OUTPUT_DIR

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    filename = f"{filename_prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
    filepath = os.path.join(OUTPUT_DIR, filename)
    with open(filepath, "wb") as f:
        f.write(data)
    return f"/output/{filename}"


async def execute_canvas_edit(
    payload: Dict[str, Any],
    history_saver=None,
    cloud_dispatcher=None,
) -> Dict[str, Any]:
    """Orchestrates Reve-style canvas image editing across cloud providers."""
    import os
    import io
    import time
    import uuid
    import asyncio
    import httpx
    from fastapi import HTTPException
    from PIL import Image

    from core.security import validate_safe_url, safe_json_post
    from core.providers import get_api_provider_exact, load_api_providers, provider_env_key_value

    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="提示词不能为空")
    source_url = str(payload.get("source_image_url") or "").strip()
    if not source_url:
        raise HTTPException(status_code=400, detail="缺少源图片地址")

    feather_radius = max(0, min(64, int(payload.get("feather_radius", 12) or 12)))
    preserve_outside = bool(payload.get("preserve_outside", True))

    async def _fetch_img_bytes(target_str: str) -> bytes:
        target_str = str(target_str or "").strip()
        if not target_str:
            return b""
        if target_str.startswith(("http://", "https://")):
            try:
                validate_safe_url(target_str, allow_private=False)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            from core.security import safe_download_bytes
            b, _ = await safe_download_bytes(target_str)
            return b
        elif target_str.startswith("data:image/"):
            import base64
            # 该分支不受 safe_download_bytes 的 50MB 上限保护，需自行设限，
            # 否则超大 base64 会在内存中物化两份（解码 + 重新编码发往上游）。
            if len(target_str) > MAX_DATA_URL_CHARS:
                raise HTTPException(status_code=413, detail="内嵌图片过大")
            if "," not in target_str:
                raise HTTPException(status_code=400, detail="内嵌图片格式非法：缺少 base64 数据段")
            _, b64 = target_str.split(",", 1)
            try:
                return base64.b64decode(b64, validate=True)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=f"内嵌图片 base64 解码失败: {exc}")
        else:
            from core.media import output_file_from_url
            local_p = output_file_from_url(target_str)
            if not local_p or not os.path.isfile(local_p):
                raise HTTPException(status_code=400, detail=f"找不到本地图片: {target_str}")
            with open(local_p, "rb") as f:
                return f.read()

    img_bytes = await _fetch_img_bytes(source_url)

    try:
        with Image.open(io.BytesIO(img_bytes)) as pil_img:
            orig_w, orig_h = pil_img.size
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"无法解析图片数据: {e}")

    region = payload.get("region")
    mask_data_url = payload.get("mask_data_url")
    mask_bytes = None
    if mask_data_url:
        try:
            mask_bytes = await asyncio.to_thread(
                decode_mask_data_url, mask_data_url, orig_w, orig_h, feather_radius
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"蒙版解析失败: {e}")
        if not region:
            region = mask_bounding_box(mask_bytes)
    elif region and isinstance(region, dict):
        mask_bytes = await asyncio.to_thread(
            synthesize_feathered_mask, orig_w, orig_h, region, feather_radius
        )
        if not region:
            region = mask_bounding_box(mask_bytes)

    ref_bytes_list: list[bytes] = []
    ref_url = str(payload.get("reference_image_url") or "").strip()
    if ref_url:
        try:
            rb = await _fetch_img_bytes(ref_url)
            if rb:
                ref_bytes_list.append(rb)
        except Exception:
            pass

    effective_prompt = prompt
    if ref_bytes_list:
        effective_prompt = f"{prompt} [同时参考附加的局部参考图主体特征与材质进行融合替换]"

    pid = str(payload.get("provider_id") or "").strip()
    req_model = str(payload.get("model") or "").strip()
    provider = None
    if pid:
        try:
            provider = get_api_provider_exact(pid)
        except HTTPException as exc:
            if "已禁用" in str(exc.detail) or not req_model:
                raise
            providers = load_api_providers()
            provider = next(
                (p for p in providers if p.get("enabled", True) and p.get("protocol") == "openai" and req_model in (p.get("image_models") or [])),
                None,
            ) or next(
                (p for p in providers if p.get("enabled", True) and req_model in (p.get("image_models") or [])),
                None,
            )
            if not provider:
                raise

    if not provider:
        providers = load_api_providers()
        req_model = str(payload.get("model") or "").strip()
        if req_model:
            for p in providers:
                if p.get("enabled", True) and p.get("protocol") == "openai" and req_model in (p.get("image_models") or []):
                    provider = p
                    break
            if not provider:
                for p in providers:
                    if p.get("enabled", True) and req_model in (p.get("image_models") or []):
                        provider = p
                        break
        if not provider:
            for p in providers:
                if p.get("enabled", True) and p.get("image_models"):
                    provider = p
                    break
        if not provider and providers:
            provider = providers[0]
        if not provider:
            provider = {"id": "default", "base_url": "https://api.openai.com/v1"}

    model = str(payload.get("model") or "").strip()
    if not model:
        models = provider.get("image_models") or []
        model = models[0] if models else "qwen-image-edit"

    edit_mode = detect_edit_dispatch_mode(provider, model)
    proto = str((provider or {}).get("protocol") or (provider or {}).get("id") or "").lower()
    pid = str((provider or {}).get("id") or "").lower()
    multi_proto = {"codex", "gemini-cli", "jimeng", "volcengine", "runninghub", "apimart", "pollinations", "gemini"}

    task_id = f"reve_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    should_dispatch_cloud = (
        callable(cloud_dispatcher)
        and edit_mode not in ("bfl-fill", "openai-edits")
        and (proto in multi_proto or pid in multi_proto)
        and proto != "openai"
    )

    if should_dispatch_cloud:
        augmented_prompt = build_edit_prompt_with_region(effective_prompt, region, orig_w, orig_h)
        guide_src = annotate_visual_region_guide(img_bytes, mask_bytes) if mask_bytes else img_bytes
        out_bytes = await cloud_dispatcher({
            "provider": provider,
            "model": model,
            "prompt": augmented_prompt,
            "source_image_bytes": guide_src,
            "raw_source_image_bytes": img_bytes,
            "mask_bytes": mask_bytes,
            "reference_images_bytes": ref_bytes_list,
            "orig_w": orig_w,
            "orig_h": orig_h,
        })
        if preserve_outside and mask_bytes:
            out_bytes = await asyncio.to_thread(composite_regional_edit, img_bytes, out_bytes, mask_bytes)
        output_url = save_output_image_bytes(out_bytes)
        if callable(history_saver):
            try:
                history_saver({
                    "type": "image",
                    "source": f"reve-edit:{provider.get('id', '')}/{model}",
                    "prompt": prompt,
                    "images": [output_url],
                    "image_urls": [output_url],
                    "model": model,
                    "provider": provider.get("id", ""),
                })
            except Exception:
                pass
        return {
            "output_url": output_url,
            "task_id": task_id,
            "model": model,
            "provider_id": provider.get("id", "default"),
        }

    edit_spec = prepare_reve_edit_payload(
        provider=provider,
        model=model,
        prompt=effective_prompt,
        source_image_bytes=img_bytes,
        mask_bytes=mask_bytes,
        region=region,
        orig_w=orig_w,
        orig_h=orig_h,
    )

    api_key = provider_env_key_value(provider.get("id", "")) or provider.get("api_key", "")
    headers = make_provider_headers(provider, api_key)
    endpoint_url = edit_spec["endpoint_url"]
    try:
        validate_safe_url(endpoint_url, allow_private=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"非法接口地址: {exc}")

    # 纵深防御：写入路径已校验绑定，但任何绕过写入的途径（直接改 API/.env、
    # 其他 Provider 编辑入口）都可能让密钥指向别处，因此每次使用前再校验一次。
    if api_key and provider.get("base_url"):
        import urllib.parse as _up
        _bound = _up.urlparse(str(provider["base_url"])).netloc.lower()
        _target = _up.urlparse(endpoint_url).netloc.lower()
        if _bound and _target and _bound != _target:
            raise HTTPException(
                status_code=400,
                detail=f"安全拦截：该 Provider 的 API Key 仅限向绑定域名 ({_bound}) 发送，禁止发送至目标地址 ({_target})",
            )

    task_id = f"reve_{int(time.time())}_{uuid.uuid4().hex[:8]}"

    if edit_spec["mode"] == "openai-edits":
        headers.pop("Content-Type", None)

    # 该请求携带 API Key，必须走 safe_json_post：它以 pin_dns=True 预解析并钉扎，
    # 连接建立后复检真实对端 IP，消除「预解析通过、实际连到内网」的 DNS 重绑定窗口。
    try:
        resp = await safe_json_post(
            endpoint_url,
            headers=headers,
            json_body=None if edit_spec["mode"] == "openai-edits" else edit_spec["json_body"],
            form_data=edit_spec["form_data"] if edit_spec["mode"] == "openai-edits" else None,
            files=edit_spec["files"] if edit_spec["mode"] == "openai-edits" else None,
            timeout=120.0,
            allow_private=False,
        )
    except httpx.HTTPStatusError as e:
        err_msg = ""
        try:
            err_data = e.response.json()
            if isinstance(err_data, dict):
                err_msg = err_data.get("error", {}).get("message") or err_data.get("detail") or err_data.get("message") or ""
        except Exception:
            err_msg = e.response.text[:200]
        if not err_msg:
            err_msg = e.response.text[:200]
        raise HTTPException(status_code=502, detail=f"上游服务请求失败 ({e.response.status_code}): {err_msg}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=504, detail=f"连接上游服务失败或超时: {e}")
    except ValueError as e:
        # safe_json_post 的 SSRF 复检在此抛出
        raise HTTPException(status_code=400, detail=str(e))

    try:
        resp.raise_for_status()
        raw = resp.json()
    except httpx.HTTPStatusError as e:
        err_msg = ""
        try:
            err_data = e.response.json()
            if isinstance(err_data, dict):
                err_msg = err_data.get("error", {}).get("message") or err_data.get("detail") or err_data.get("message") or ""
        except Exception:
            err_msg = e.response.text[:200]
        if not err_msg:
            err_msg = e.response.text[:200]
        raise HTTPException(status_code=502, detail=f"上游服务请求失败 ({e.response.status_code}): {err_msg}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=504, detail=f"连接上游服务失败或超时: {e}")

    # 异步任务轮询（BFL Fill）。该请求同样携带 API Key，因此走 safe_json_post。
    if edit_spec.get("is_async_task") and isinstance(raw, dict) and raw.get("id"):
        poll_id = raw["id"]
        base_api = (provider.get("base_url") or "https://api.bfl.ml/v1").rstrip("/")
        poll_url = f"{base_api}/get_result?id={poll_id}"
        poll_headers = make_provider_headers(provider, api_key)

        deadline = time.monotonic() + 60.0
        ready = False
        while time.monotonic() < deadline:
            await asyncio.sleep(1.2)
            try:
                poll_resp = await safe_json_post(
                    poll_url, method="GET", headers=poll_headers,
                    timeout=30.0, allow_private=False,
                )
                poll_resp.raise_for_status()
                poll_data = poll_resp.json()
            except Exception:
                continue

            if not isinstance(poll_data, dict):
                continue
            status = str(poll_data.get("status") or "").capitalize()
            if status == "Ready":
                raw = poll_data
                ready = True
                break
            elif status in ("Error", "Failed"):
                err_detail = poll_data.get("error") or poll_data.get("message") or "BFL 处理失败"
                raise HTTPException(status_code=502, detail=f"BFL 任务失败: {err_detail}")
        if not ready:
            raise HTTPException(status_code=504, detail="BFL 局部修图任务超时")

    out_bytes: Optional[bytes] = None
    if isinstance(raw, dict):
        data_list = raw.get("data")
        if isinstance(data_list, list) and len(data_list) > 0:
            first = data_list[0]
            if isinstance(first, dict):
                if first.get("b64_json"):
                    import base64
                    out_bytes = base64.b64decode(first["b64_json"])
                elif first.get("url"):
                    img_url = first["url"]
                    try:
                        validate_safe_url(img_url, allow_private=False)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc))
                    from core.security import safe_download_bytes
                    out_bytes, _ = await safe_download_bytes(img_url)
        elif raw.get("images") and isinstance(raw["images"], list) and len(raw["images"]) > 0:
            first_img = raw["images"][0]
            if isinstance(first_img, str) and first_img.startswith(("http://", "https://")):
                try:
                    validate_safe_url(first_img, allow_private=False)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                from core.security import safe_download_bytes
                out_bytes, _ = await safe_download_bytes(first_img)
            elif isinstance(first_img, dict):
                if first_img.get("url"):
                    img_url = first_img["url"]
                    try:
                        validate_safe_url(img_url, allow_private=False)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc))
                    from core.security import safe_download_bytes
                    out_bytes, _ = await safe_download_bytes(img_url)
                elif first_img.get("b64_json"):
                    import base64
                    out_bytes = base64.b64decode(first_img["b64_json"])
        elif raw.get("output"):
            out_val = raw["output"]
            if isinstance(out_val, list) and len(out_val) > 0:
                out_val = out_val[0]
            if isinstance(out_val, str) and out_val.startswith(("http://", "https://")):
                try:
                    validate_safe_url(out_val, allow_private=False)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc))
                from core.security import safe_download_bytes
                out_bytes, _ = await safe_download_bytes(out_val)
        elif isinstance(raw.get("result"), dict) and raw["result"].get("sample"):
            sample_url = raw["result"]["sample"]
            try:
                validate_safe_url(sample_url, allow_private=False)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            from core.security import safe_download_bytes
            out_bytes, _ = await safe_download_bytes(sample_url)

    if not out_bytes and isinstance(raw, dict) and isinstance(raw.get("url"), str) and raw["url"].startswith(("http://", "https://")):
        try:
            validate_safe_url(raw["url"], allow_private=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        from core.security import safe_download_bytes
        out_bytes, _ = await safe_download_bytes(raw["url"])

    if not out_bytes:
        # 上游返回了无法识别的结构。此处过去会回退到一个从未写盘的
        # /output/{task_id}.png 并当作成功返回，前端会把永久破图标记为"完成"。
        raise HTTPException(status_code=502, detail="上游未返回可用图片")

    if preserve_outside and mask_bytes:
        out_bytes = await asyncio.to_thread(composite_regional_edit, img_bytes, out_bytes, mask_bytes)

    output_url = save_output_image_bytes(out_bytes)

    if not output_url:
        # 上游返回了无法识别的结构。此处过去会回退到一个从未写盘的
        # /output/{task_id}.png 并当作成功返回，前端会把永久破图标记为"完成"。
        raise HTTPException(status_code=502, detail="上游未返回可用图片")

    # spec §2.3.4：编辑结果需进入历史栈（Ctrl+Z 可回退）。
    # 此前只在 online_image 侧记录，execute_canvas_edit 完全没有落历史。
    # history_saver 由调用方（routers 层）注入：core 不得反向 import routers，
    # 该分层不变式由 tests/test_round15_fixes.py 守护。
    if callable(history_saver):
        try:
            history_saver({
                "type": "image",
                "source": f"reve-edit:{provider.get('id', '')}/{model}",
                "prompt": prompt,
                "images": [output_url],
                "image_urls": [output_url],
                "model": model,
                "provider": provider.get("id", ""),
            })
        except Exception:
            # 历史记录失败不得影响编辑结果本身
            pass

    return {
        "output_url": output_url,
        "task_id": task_id,
        "model": model,
        "provider_id": provider.get("id", "default"),
    }


