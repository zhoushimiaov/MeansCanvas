"""Cloud AI Online Image Generation, Midjourney Bridge & Uploads Router for Infinite-Canvas.

Decoupled router providing endpoints and generation engines for:
- Cloud Image Generation Dispatch (OpenAI DALL-E/Flux, Pollinations, Meigen, Tudou, Apimart, ModelScope, Gemini, Volcengine, RunningHub, Codex, Jimeng)
- Midjourney Bridge (Submit, Actions, Modal, Tasks)
- ModelScope Native Generation & Angle Control
- Canvas Image Task Management & Polling
- Generic & AI Image Asset Uploads
"""
import os
import re
import sys
import json
import time
import math
import uuid
import base64
import hashlib
import asyncio
import logging
import urllib
import urllib.parse
import shutil
import random
import mimetypes
from io import BytesIO
from typing import Dict, Any, List, Optional, Tuple, Set, Union
from urllib.parse import urlparse, quote, unquote

import httpx
import requests
from PIL import Image, ImageOps
from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from core.providers import (
    BASE_DIR,
    DATA_DIR,
    OUTPUT_DIR,
    OUTPUT_INPUT_DIR,
    OUTPUT_OUTPUT_DIR,
    ASSETS_DIR,
    ASSET_LIBRARY_DIR,
    LOCAL_UPLOAD_DIR,
    CONVERSATION_DIR,
    PUBLIC_BASE_URL,
    PUBLIC_MEDIA_BASE_URL,
    AI_API_KEY,
    VIDEO_POLL_TIMEOUT,
    AI_REQUEST_TIMEOUT,
    COMFYUI_INSTANCES,
    IMAGE_MODEL,
    IMAGE_MODELS,
    IMAGE_POLL_INTERVAL,
    IMAGE_TASK_TIMEOUT,
    POLLINATIONS_DEFAULT_IMAGE_MODELS,
    MEIGEN_DEFAULT_IMAGE_MODELS,
    MODELSCOPE_DEFAULT_IMAGE_MODEL,
    MODELSCOPE_DEFAULT_IMAGE_MODELS,
    MODELSCOPE_DEFAULT_LORAS,
    ONLINE_IMAGE_PROMPT_MAX_LENGTH,
    ONLINE_IMAGE_REFERENCE_MAX,
    APIMART_IMAGE_TASK_TIMEOUT,
    APIMART_IMAGE_INITIAL_POLL_DELAY,
    APIMART_IMAGE_POLL_INTERVAL,
    TUDOU_ASYNC_IMAGE_TASK_TIMEOUT,
    TUDOU_ASYNC_IMAGE_INITIAL_POLL_DELAY,
    TUDOU_ASYNC_IMAGE_POLL_INTERVAL,
    now_ms,
    get_api_provider,
    get_api_provider_exact,
    bearer_auth_value,
    provider_endpoint_url,
    provider_env_key_value,
    provider_protocol,
    read_api_env_value,
    load_api_providers,
    save_api_providers,
    update_env_values,
    dump_pydantic,
    model_list_from_values,
    modelscope_api_key,
    modelscope_api_root,
    modelscope_image_api_root,
    modelscope_provider_config,
    detect_image_request_mode,
    effective_image_request_mode,
    normalize_image_request_mode,
    locked_recommended_provider_rule,
    selected_model,
    effective_protocol,
    AI_BASE_URL,
    log_upstream_call,
    _upstream_request_hook,
    _upstream_response_hook,
)
from core.media import (
    output_file_from_url,
    content_type_for_path,
    is_image_reference_value,
    is_video_reference_value,
    convert_output_to_jpg,
    reference_to_data_url,
    is_image_reference,
    image_references,
    media_reference_to_url,
    compress_data_url_image,
    looks_like_image_media_url,
)
from core.security import (
    safe_download_bytes,
    validate_safe_url,
)
from routers.models import (
    TokenRequest,
    CloudGenRequest,
    CloudPollRequest,
    AIReference,
    OnlineImageRequest,
    MidjourneySubmitRequest,
    MidjourneyActionRequest,
    MidjourneyModalRequest,
    ImageTaskQueryRequest,
    TempShUploadRequest,
    LocalImageImportRequest,
    Base64UploadRequest,
    MsGenerateRequest,
)
from routers.assets import (
    output_url_for,
    output_path_for,
    ensure_same_origin_request,
    sanitize_asset_name,
    _local_upload_kind_ext,
)
from routers.common import (
    get_global_loop,
    manager,
    save_to_history,
    log_net_error,
    post_openai_responses_stream,
    CHAT_RATIO_SIZE_OPTIONS,
    is_tudou_grok_image_model,
    WsProxy,
    api_headers,
    is_volcengine_provider,
    is_runninghub_provider,
    is_jimeng_provider,
    is_codex_provider,
    is_gemini_cli_provider,
    is_gemini_provider,
    is_tudou_provider,
    is_apimart_provider,
    is_meigen_provider,
    is_pollinations_provider,
    meigen_env_key,
    _meigen_rpc,
    looks_like_generated_image_url,
    looks_like_image_base64,
    image_payload_from_string,
    extract_image_flexible,
    extract_images,
    extract_image,
    extract_task_id,
    extract_task_id_from_text,
    images_api_unsupported,
    public_base_url,
    public_media_url_suffix,
    local_asset_public_url,
    save_ai_image_to_output,
    image_output_meta,
    save_remote_video_to_output,
    parse_size_pair,
    snap_size_to_multiple,
    friendly_image_error_detail,
    parse_error_payload_text,
    friendly_chat_error_detail,
)
from routers.integrations import (
    generate_runninghub_entry_image,
    generate_runninghub_provider_image,
    generate_codex_provider_image,
    generate_gemini_cli_provider_image,
    generate_jimeng_provider_image,
    generate_jimeng_upscale_image,
    generate_volcengine_provider_image,
    runninghub_api_key,
    runninghub_endpoint_url,
    runninghub_query_status,
    runninghub_extract_task_id,
    runninghub_extract_outputs,
    runninghub_fail_reason,
    runninghub_error_detail,
    wait_for_runninghub_image_task,
    rewrite_runninghub_file_url,
    JimengPendingError,
    jimeng_pending_payload,
    runninghub_aspect_from_size,
    runninghub_app_headers,
    runninghub_store_remote_output,
)
from routers.tasks import record_task_event
from routers.video import (
    sanitize_video_prompt,
    upload_local_video_to_cloud,
    openai_video_proxy_local_image_path,
    openai_video_proxy_public_reference_url,
)

logger = logging.getLogger("infinite_canvas.online_image")
_server_logger = logging.getLogger("dx.server")

router = APIRouter(tags=["online_image"])

# Global in-memory image tasks cache
CANVAS_TASKS: Dict[str, Dict[str, Any]] = {}
import threading
CANVAS_TASK_LOCK = threading.Lock()
MAX_CANVAS_TASKS = 1000
_CANVAS_TASK_SEMAPHORE = asyncio.Semaphore(10)
CANVAS_IMAGE_TASK_TIMEOUT = int(os.getenv("CANVAS_IMAGE_TASK_TIMEOUT", "1800"))

def _prune_canvas_tasks_locked():
    """Prune oldest completed or failed tasks when CANVAS_TASKS exceeds MAX_CANVAS_TASKS."""
    if len(CANVAS_TASKS) <= MAX_CANVAS_TASKS:
        return
    prunable = [
        (tid, task.get("updated_at", 0))
        for tid, task in CANVAS_TASKS.items()
        if task.get("status") in ("succeeded", "failed", "cancelled")
    ]
    prunable.sort(key=lambda x: x[1])
    excess = len(CANVAS_TASKS) - MAX_CANVAS_TASKS
    for tid, _ in prunable[:excess]:
        CANVAS_TASKS.pop(tid, None)

_MEIGEN_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
APIMART_MIDJOURNEY_API_ROOT = "https://api.apimart.ai"
MIDJOURNEY_SPEEDS = {"relax", "fast", "turbo"}
MIDJOURNEY_ACTION_PATHS = {
    "upsample": "/v1/midjourney/action/upsample",
    "variation": "/v1/midjourney/action/variation",
    "zoom": "/v1/midjourney/action/zoom",
    "pan": "/v1/midjourney/action/pan",
    "describe": "/v1/midjourney/describe",
    "shorten": "/v1/midjourney/shorten",
}
LOCAL_IMAGE_IMPORT_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
LOCAL_IMAGE_IMPORT_MAX_BYTES = 50 * 1024 * 1024
IMAGE_TASK_SUCCESS_STATUSES = {"SUCCEED", "SUCCESS", "SUCCEEDED", "COMPLETED", "DONE", "READY"}
IMAGE_TASK_FAILED_STATUSES = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}
TUDOU_GROK_IMAGE_SIZES = {"1024x1024", "1280x720", "720x1280", "1792x1024", "1024x1792"}
TUDOU_GROK_IMAGE_ASPECT_SIZES = {
    "1:1": "1024x1024",
    "16:9": "1280x720",
    "9:16": "720x1280",
    "7:4": "1792x1024",
    "4:7": "1024x1792",
}

async def yuli_fetch_reference_bytes(client, ref_url):
    """把参考图（input_reference 垫图）取回为 (filename, bytes, mime)"""
    ref_url = str(ref_url or "").strip()
    if not ref_url:
        return None
    if ref_url.startswith("data:"):
        header, _, b64 = ref_url.partition(",")
        mime = (header[5:].split(";")[0] or "image/png").strip()
        try:
            raw = base64.b64decode(b64)
        except Exception:
            return None
        ext = (mime.split("/")[-1] or "png").split("+")[0]
        return (f"input_reference.{ext}", raw, mime)
    path = output_file_from_url(ref_url)
    if path:
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except Exception:
            return None
        mime = content_type_for_path(path)
        return (os.path.basename(path) or "input_reference", raw, mime)
    if ref_url.startswith("http://") or ref_url.startswith("https://"):
        try:
            raw, ctype = await safe_download_bytes(ref_url, max_bytes=50 * 1024 * 1024, allow_private=False)
        except Exception:
            return None
        mime = (ctype or "image/png").split(";")[0].strip()
        ext = (mime.split("/")[-1] or "png").split("+")[0]
        return (f"input_reference.{ext}", raw, mime)
    return None


# --- Sliced from online.py lines 564-575 ---
def modelscope_size(value, fallback="1024x1024"):
    size = str(value or fallback).strip().lower().replace("*", "x")
    if re.fullmatch(r"\d{2,5}x\d{2,5}", size):
        return size
    raise HTTPException(status_code=400, detail=f"ModelScope size 格式不正确：{value or fallback}，应为 WxH，例如 1024x1024")

def unwrap_apimart_response(raw):
    """APIMart 将标准 OpenAI 响应包在 {"code":200,"data":{...}} 里；如果检测到就解包。"""
    if isinstance(raw, dict) and "data" in raw and isinstance(raw.get("data"), dict) and "choices" not in raw:
        return raw["data"]
    return raw



# --- Sliced from online.py lines 914-1117 ---
def responses_image_size_instruction(size: str) -> str:
    """RS 中转多为网页版逆向：结构化 size 参数（tool.size / 顶层 size / --size 尾注）全被无视，
    只有内部模型能“听懂”的自然语言比例要求有效（实测中文明确说横版+比例+禁止正方形可让
    1:1 变成 3:2 横版）。这里生成中英双语的强化指令。"""
    match = re.match(r"^\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*$", str(size or ""))
    if not match:
        return ""
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return ""
    if width == height:
        return "请生成正方形图片（宽高比 1:1）。Generate a SQUARE image (aspect ratio 1:1)."
    from fractions import Fraction
    ratio = Fraction(width, height).limit_denominator(32)
    rw, rh = ratio.numerator, ratio.denominator
    if width > height:
        zh_shape, en_shape = "横版（宽幅）", "LANDSCAPE (wide)"
    else:
        zh_shape, en_shape = "竖版（长幅）", "PORTRAIT (tall)"
    return (
        f"请生成{zh_shape}图片：宽高比 {rw}:{rh}，目标尺寸为宽 {width} × 高 {height} 像素，绝对不要输出正方形（1:1）。"
        f" Generate a {en_shape} image with aspect ratio {rw}:{rh}, target size {width}x{height} pixels (width x height)."
        f" Never output a square 1:1 image. Do not swap width and height."
    )

def responses_proxy_tool_size(size: str) -> str:
    """部分 RS 中转把 image_generation.size 当成 height x width；这里只对 RS 模式做兼容翻转。"""
    match = re.match(r"^\s*(\d{2,5})\s*[xX*]\s*(\d{2,5})\s*$", str(size or ""))
    if not match:
        return str(size or "").strip()
    width, height = match.group(1), match.group(2)
    return f"{height}x{width}" if width != height else f"{width}x{height}"

async def responses_input_image_url(ref, require_public_url=False) -> str:
    """RS / Responses 的 input_image。
    本机/内网 URL 不能透传（上游拉不到会挂到 Cloudflare 120s 超时/524）。
    本地文件优先上传图床（同视频卡片的 Litterbox/temp.sh 通道）换公网短链——
    几 MB 的 base64 请求体会让部分中转源站处理超时，公网 URL 让请求体和文生图一样小；
    图床不可用时回退内联 base64（Responses 协议两种都支持）。"""
    raw = ref.get("url", "") if isinstance(ref, dict) else ref
    text = str(raw or "").strip()
    if not text:
        return ""
    local_path = text
    if re.match(r"^https?://", text, re.I):
        parsed = urllib.parse.urlsplit(text)
        host = (parsed.hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"} or re.match(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)", host):
            local_path = urllib.parse.unquote(parsed.path or "")
        else:
            return text
    local_file = output_file_from_url(local_path)
    if not local_file:
        if require_public_url:
            raise HTTPException(status_code=400, detail=f"RS 参考图不是公网 URL，无法传给上游：{text[:160]}")
        return ""
    if require_public_url:
        return await openai_video_proxy_public_reference_url(local_path)
    try:
        uploaded = await upload_local_video_to_cloud(local_path)
        url = str((uploaded or {}).get("url") or "")
        if url.startswith(("http://", "https://")):
            return url
    except HTTPException as exc:
        logger.warning(f"RS 参考图上传图床失败，回退内联 base64：{exc.detail}")
    except Exception as exc:
        logger.warning(f"RS 参考图上传图床异常，回退内联 base64：{exc}")
    data_url = reference_to_data_url({"url": local_path}, max_size=1536)
    return data_url if data_url.startswith("data:") else ""

def responses_no_image_detail(data) -> str:
    if not isinstance(data, dict):
        return ""
    details = []
    error = data.get("error")
    if isinstance(error, dict):
        msg = error.get("message") or error.get("detail") or error.get("code")
        if msg:
            details.append(str(msg))
    output_text = data.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        details.append(output_text.strip()[:300])
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "image_generation_call":
                continue
            status = item.get("status")
            if status:
                details.append(f"image_generation_call.status={status}")
            item_error = item.get("error")
            if isinstance(item_error, dict):
                msg = item_error.get("message") or item_error.get("detail") or item_error.get("code")
                if msg:
                    details.append(str(msg))
            elif isinstance(item_error, str) and item_error.strip():
                details.append(item_error.strip())
    joined = "；".join(dict.fromkeys(details))
    return f"RS / Responses 没有返回图片数据{f'：{joined}' if joined else ''}"

def responses_output_text_image(raw):
    """兜底解析：部分 RS 中转不返回标准 image_generation_call，而是把生图结果
    以 output_text 里的 markdown 图片链接（![...](url)）或裸图片 URL 返回。"""
    texts = []
    def collect(value, depth=0):
        if depth > 6 or len(texts) > 40:
            return
        if isinstance(value, str):
            if value.strip():
                texts.append(value)
            return
        if isinstance(value, list):
            for item in value:
                collect(item, depth + 1)
            return
        if isinstance(value, dict):
            for key in ("output", "content", "text", "output_text", "message", "response"):
                if key in value:
                    collect(value[key], depth + 1)
    collect(raw)
    for text in texts:
        match = re.search(r"!\[[^\]]*\]\((https?://[^)\s]+)\)", text)
        if match:
            return {"type": "url", "value": match.group(1)}
        match = re.search(r"https?://[^\s)\"'<>]+\.(?:png|jpe?g|webp|gif)(?:\?[^\s)\"'<>]*)?", text, re.I)
        if match:
            return {"type": "url", "value": match.group(0)}
    return None

def _responses_wrap(url, status_code, payload):
    return httpx.Response(
        status_code,
        headers={"content-type": "application/json"},
        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        request=httpx.Request("POST", url),
    )

RESPONSES_REJECT_STATUSES = {400, 404, 405, 415, 422}
RESPONSES_POLL_INTERVAL = 5.0
RESPONSES_POLL_MAX_SECONDS = 1500.0

async def post_openai_responses(client, upstream_url, headers, body):
    """RS / Responses 请求。图片编辑经常超过 120 秒，非流式请求会被中转前面的
    Cloudflare 读超时掐断（Error 524）。策略按可靠性排序：
    1) background:true 后台任务 + 轮询 GET /v1/responses/{id}（每个请求都秒回，彻底绕开超时）；
    2) 后台模式被拒（4xx 参数类错误）→ SSE 流式；
    3) 流式也被拒 → 非流式直接请求。
    5xx/超时一律不自动重试，避免上游已开始生成后重复扣费。"""
    bg_body = dict(body)
    bg_body["background"] = True
    try:
        resp = await client.post(upstream_url, headers=headers, json=bg_body)
    except httpx.HTTPError as e:
        logger.info(f"RS background 请求传输失败，改走流式：{e}")
        return await post_openai_responses_stream(client, upstream_url, headers, body)
    if resp.status_code in RESPONSES_REJECT_STATUSES:
        logger.info(f"RS background 模式被拒（{resp.status_code}），改走流式：{resp.text[:200]}")
        return await post_openai_responses_stream(client, upstream_url, headers, body)
    if resp.status_code >= 400:
        if resp.status_code == 524:
            return _responses_wrap(upstream_url, 502, {"error": {"message": (
                "中转在 background 模式下仍然 524 超时：该渠道对 /v1/responses 的 background/stream 都不透传，"
                "无法完成超过 120 秒的图片编辑。请换支持 Responses 透传的渠道。上游原文："
                f"{resp.text[:300]}"
            )}})
        return resp
    try:
        data = resp.json()
    except ValueError:
        return resp
    status = str((data or {}).get("status") or "").lower()
    rid = str((data or {}).get("id") or "").strip()
    if status not in {"queued", "in_progress", "processing", "pending", "running"} or not rid:
        return resp  # 中转忽略 background 直接同步返回了结果（或未知结构），交给下游解析
    # 轮询后台任务
    retrieve_url = f"{upstream_url.rstrip('/')}/{urllib.parse.quote(rid)}"
    deadline = time.monotonic() + RESPONSES_POLL_MAX_SECONDS
    transient_failures = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(RESPONSES_POLL_INTERVAL)
        try:
            poll = await client.get(retrieve_url, headers=headers)
        except httpx.HTTPError as e:
            transient_failures += 1
            if transient_failures > 5:
                return _responses_wrap(upstream_url, 502, {"error": {"message": f"RS 后台任务轮询连续失败：{e}（任务 id={rid}）"}})
            continue
        if poll.status_code >= 400:
            transient_failures += 1
            if transient_failures > 5:
                return _responses_wrap(upstream_url, 502, {"error": {"message": f"RS 后台任务轮询失败（{poll.status_code}）：{poll.text[:200]}（任务 id={rid}）"}})
            continue
        transient_failures = 0
        try:
            data = poll.json()
        except ValueError:
            continue
        status = str((data or {}).get("status") or "").lower()
        if status == "completed":
            return _responses_wrap(upstream_url, 200, data)
        if status in {"failed", "cancelled", "incomplete"}:
            return _responses_wrap(upstream_url, 502, data)
    return _responses_wrap(upstream_url, 502, {"error": {"message": f"RS 后台任务超过 {int(RESPONSES_POLL_MAX_SECONDS)}s 仍未完成（任务 id={rid}）"}})



# --- Sliced from online.py lines 1299-1408 ---
def is_tudou_async_image_mode(provider, model=""):
    return (
        normalize_image_request_mode((provider or {}).get("image_request_mode")) == "tudou-async"
        and (not model or is_tudou_async_image_model(model))
    )

def is_tudou_async_image_model(model):
    return str(model or "").strip().lower().startswith("gpt-image-2")

def tudou_image_model_for_request(model):
    """Keep older canvases working after Tudou retired the unsuffixed GPT Image 2 model."""
    value = str(model or "").strip()
    return "gpt-image-2-1k" if value.lower() == "gpt-image-2" else value

def tudou_async_resolution(model, resolution, size=""):
    requested = str(resolution or "").strip().lower()
    if requested in {"1k", "2k", "4k"}:
        return requested
    model_name = str(model or "").strip().lower()
    for value in ("4k", "2k", "1k"):
        if model_name.endswith(f"-{value}"):
            return value
    width, height = parse_size_pair(size)
    edge = max(width or 0, height or 0)
    pixels = (width or 0) * (height or 0)
    if edge >= 2800 or pixels >= 7_000_000:
        return "4k"
    if edge >= 1600 or pixels >= 2_000_000:
        return "2k"
    return "1k"

def tudou_async_size(size, aspect_ratio=""):
    ratio = str(aspect_ratio or "").strip()
    if re.fullmatch(r"\d+\s*:\s*\d+", ratio):
        return ratio.replace(" ", "")
    value = str(size or "").strip().lower().replace("*", "x")
    if re.fullmatch(r"\d{2,5}x\d{2,5}", value):
        return value
    return "1:1"

def tudou_png_or_jpeg_data_url(value):
    text = str(value or "").strip()
    if re.match(r"^data:image/(?:png|jpe?g);base64,", text, re.I):
        return text
    if not text.startswith("data:image/"):
        return text
    header, sep, encoded = text.partition(",")
    if not sep or not encoded:
        return text
    try:
        raw = base64.b64decode(encoded, validate=False)
        with Image.open(BytesIO(raw)) as img:
            img.load()
            img = ImageOps.exif_transpose(img)
            if img.mode in ("RGBA", "LA", "P"):
                fmt, mime = "PNG", "image/png"
                converted = img.convert("RGBA")
            else:
                fmt, mime = "JPEG", "image/jpeg"
                converted = img.convert("RGB")
            buf = BytesIO()
            converted.save(buf, format=fmt, quality=90 if fmt == "JPEG" else None)
        return f"data:{mime};base64,{base64.b64encode(buf.getvalue()).decode('ascii')}"
    except Exception:
        return text

async def tudou_async_reference_images(reference_images):
    images = []
    for ref in (reference_images or [])[:ONLINE_IMAGE_REFERENCE_MAX]:
        value = str((ref or {}).get("url") or "").strip()
        if value.startswith(("http://", "https://")):
            images.append(value)
            continue
        if value.startswith("data:image/"):
            images.append(tudou_png_or_jpeg_data_url(value))
            continue
        data_url = reference_to_data_url(ref, max_size=1536)
        if data_url and data_url.startswith("data:image/"):
            images.append(tudou_png_or_jpeg_data_url(data_url))
    return images

async def generate_tudou_async_image(prompt, size, quality, model, reference_images, provider, aspect_ratio="", resolution=""):
    """Tudou's GPT-Image-2 async route, isolated from generic OpenAI image calls."""
    base_url = str((provider or {}).get("base_url") or "").strip().rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider.get('id') or '土豆'} 未配置 Base URL")
    body = {
        "model": "gpt-image-2-all",
        "prompt": str(prompt or "").strip(),
        "size": tudou_async_size(size, aspect_ratio),
        "resolution": tudou_async_resolution(model, resolution, size),
        "quality": str(quality or "").strip().lower() if str(quality or "").strip().lower() in {"low", "medium", "high"} else "medium",
    }
    images = await tudou_async_reference_images(reference_images)
    if images:
        body["images"] = images
    endpoint = provider_endpoint_url(provider, "image_generation_endpoint", "/v1/images/generations/async")
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=httpx.Timeout(connect=20.0, read=180.0, write=120.0, pool=20.0)) as client:
        response = await client.post(endpoint, headers=api_headers(provider=provider, model=body["model"]), json=body)
        response.raise_for_status()
        raw = response.json()
        task_id = extract_task_id(raw) if isinstance(raw, dict) else None
        if not task_id:
            try:
                return extract_image(raw), raw
            except HTTPException as exc:
                raise HTTPException(status_code=502, detail=f"土豆异步生图未返回 task_id：{str(raw)[:500]}") from exc
        result = await wait_for_image_task(client, task_id, provider)
        return extract_image(result), result



# --- Sliced from online.py lines 3424-3513 ---
def image_task_url_for_provider(provider, task_id):
    base_url = (provider.get("base_url") if provider else AI_BASE_URL).rstrip("/")
    provider_id = str((provider or {}).get("id") or "").lower()
    if "bfl.ml" in base_url.lower() or provider_id == "bfl":
        return f"{base_url}/get_result?id={task_id}"
    if is_tudou_async_image_mode(provider):
        return f"{base_url}/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{task_id}"
    # 异步生图（openai-video-proxy）模式优先于 apimart 协议判断：
    # 提交走 /v1/videos，轮询必须走 /v1/videos/{id}；否则 protocol=apimart 的平台会错走 /v1/tasks/{id}
    if normalize_image_request_mode((provider or {}).get("image_request_mode")) == "openai-video-proxy":
        return f"{base_url}/videos/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/videos/{task_id}"
    if is_apimart_provider(provider):
        return f"{base_url}/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/tasks/{task_id}"
    return f"{base_url}/images/tasks/{task_id}" if base_url.endswith("/v1") else f"{base_url}/v1/images/tasks/{task_id}"

def image_task_data(payload):
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload if isinstance(payload, dict) else {}

def image_task_status(payload):
    task_data = image_task_data(payload)
    status = str(task_data.get("status") or task_data.get("task_status") or (payload.get("status") if isinstance(payload, dict) else "") or "").upper()
    return status

def image_task_fail_reason(payload):
    task_data = image_task_data(payload)
    error = task_data.get("error") if isinstance(task_data.get("error"), dict) else {}
    return task_data.get("fail_reason") or task_data.get("message") or error.get("message") or (payload.get("message") if isinstance(payload, dict) else "") or "生图任务失败"

async def httpx_request_with_transient_retries(client, method, upstream_url, attempts=2, retry_delay=1.2, **kwargs):
    attempts = max(1, int(attempts or 1))
    last_exc = None
    retry_statuses = {502, 503, 504, 520, 522, 524}
    for attempt in range(attempts):
        try:
            response = await client.request(method, upstream_url, **kwargs)
            if response.status_code in retry_statuses and attempt + 1 < attempts:
                await asyncio.sleep(retry_delay * (attempt + 1))
                continue
            return response
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.PoolTimeout) as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                raise
            logger.info(f"[HTTPX-RETRY] {method} {upstream_url} transient error: {exc}; retry {attempt + 2}/{attempts}")
            await asyncio.sleep(retry_delay * (attempt + 1))
    if last_exc:
        raise last_exc
    raise httpx.HTTPError(f"请求失败：{method} {upstream_url}")

async def fetch_image_task_payload(client, task_id, provider=None):
    task_url = image_task_url_for_provider(provider, task_id)
    response = await httpx_request_with_transient_retries(
        client,
        "GET",
        task_url,
        attempts=3,
        headers=api_headers(provider=provider),
    )
    response.raise_for_status()
    return response.json()

async def wait_for_image_task(client, task_id, provider=None):
    is_apimart = is_apimart_provider(provider)
    is_tudou_async = is_tudou_async_image_mode(provider)
    timeout = TUDOU_ASYNC_IMAGE_TASK_TIMEOUT if is_tudou_async else APIMART_IMAGE_TASK_TIMEOUT if is_apimart else IMAGE_TASK_TIMEOUT
    interval = TUDOU_ASYNC_IMAGE_POLL_INTERVAL if is_tudou_async else APIMART_IMAGE_POLL_INTERVAL if is_apimart else IMAGE_POLL_INTERVAL
    initial_delay = TUDOU_ASYNC_IMAGE_INITIAL_POLL_DELAY if is_tudou_async else APIMART_IMAGE_INITIAL_POLL_DELAY if is_apimart else 0
    deadline = time.monotonic() + timeout
    last_payload = {}
    while time.monotonic() < deadline:
        if initial_delay:
            await asyncio.sleep(min(initial_delay, max(0.0, deadline - time.monotonic())))
            initial_delay = 0
            if time.monotonic() >= deadline:
                break
        last_payload = await fetch_image_task_payload(client, task_id, provider)
        status = image_task_status(last_payload)
        if not status:
            try:
                if extract_image(last_payload):
                    return last_payload
            except HTTPException:
                pass
        if status in IMAGE_TASK_SUCCESS_STATUSES:
            return last_payload
        if status in IMAGE_TASK_FAILED_STATUSES:
            raise HTTPException(status_code=502, detail=f"生图任务失败：{image_task_fail_reason(last_payload)}")
        await asyncio.sleep(min(interval, max(0.0, deadline - time.monotonic())))
    raw_text = json.dumps(last_payload, ensure_ascii=False)[:800] if last_payload else ""
    extra = f"，最后响应：{raw_text}" if raw_text else ""
    raise HTTPException(status_code=504, detail=f"生图任务超时（已等待 {int(timeout)} 秒），task_id={task_id}{extra}")

# --- Sliced from online.py lines 3517-3568 ---
def normalize_local_image_path(value):
    text = str(value or "").strip().strip('"').strip("'")
    if not text:
        raise HTTPException(status_code=400, detail="本地图片路径为空")
    if text.startswith(("/output/", "/assets/")):
        p = output_file_from_url(text)
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    if text.lower().startswith("file:"):
        parsed = urllib.parse.urlparse(text)
        if parsed.scheme.lower() != "file":
            raise HTTPException(status_code=400, detail="只支持本地图片路径")
        if parsed.netloc and re.match(r"^[a-zA-Z]:$", parsed.netloc) and os.name == "nt":
            path = f"{parsed.netloc}{urllib.request.url2pathname(parsed.path or '')}"
        elif parsed.netloc and parsed.netloc.lower() not in ("localhost",):
            raise HTTPException(status_code=400, detail="只支持本机图片路径")
        else:
            path = urllib.request.url2pathname(parsed.path or "")
    else:
        path = text
    path = path.strip().strip('"').strip("'")
    if re.match(r"^/[a-zA-Z]:[\\/]", path):
        path = path[1:]
    if re.match(r"^[a-zA-Z]:[\\/]", path) and os.name != "nt":
        raise HTTPException(status_code=400, detail="只支持导入项目目录或已配置存储根内的本地图片")
    if re.match(r"^[a-zA-Z]:[\\/]", path) or (path.startswith("/") and os.name != "nt"):
        resolved = os.path.abspath(path)
    else:
        resolved = os.path.abspath(os.path.join(BASE_DIR, path))

    allowed_roots = [os.path.abspath(BASE_DIR)]
    for d in (OUTPUT_DIR, ASSETS_DIR, DATA_DIR, LOCAL_UPLOAD_DIR):
        if d:
            allowed_roots.append(os.path.abspath(d))

    is_safe = False
    for root in allowed_roots:
        try:
            if os.path.commonpath([resolved, root]) == root:
                is_safe = True
                break
        except ValueError:
            continue

    if not is_safe:
        raise HTTPException(status_code=400, detail="只支持导入项目目录或已配置存储根内的本地图片")

    return resolved

def import_local_image_file(path):
    ext = os.path.splitext(path)[1].lower()
    if ext not in LOCAL_IMAGE_IMPORT_EXTS:
        raise HTTPException(status_code=400, detail="仅支持 PNG、JPG、JPEG、WEBP、GIF 图片")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="本地图片不存在或无法读取")
    try:
        size = os.path.getsize(path)
    except OSError:
        raise HTTPException(status_code=404, detail="本地图片不存在或无法读取")
    if size <= 0:
        raise HTTPException(status_code=400, detail="本地图片为空")
    if size > LOCAL_IMAGE_IMPORT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="本地图片过大，请使用 50MB 以内的图片")
    try:
        with Image.open(path) as img:
            img.verify()
    except Exception:
        raise HTTPException(status_code=400, detail="文件不是可识别的图片")
    filename = f"ai_ref_{uuid.uuid4().hex[:12]}{ext}"
    dest = output_path_for(filename, "input")
    try:
        shutil.copyfile(path, dest)
    except OSError:
        raise HTTPException(status_code=500, detail="导入本地图片失败")
    return {"url": output_url_for(filename, "input"), "name": os.path.basename(path) or filename, "kind": "image"}



# --- Sliced from online.py lines 4146-4152 ---
def modelscope_image_url(value, max_size=1536):
    if not value:
        return value
    if isinstance(value, str) and (value.startswith("/output/") or value.startswith("/assets/")):
        return reference_to_data_url({"url": value}, max_size=max_size)
    return value



# --- Sliced from online.py lines 5072-5245 ---
def chat_prompt_size_override(message, current_size=""):
    text = str(message or "")
    direct = re.search(r"(?<!\d)([1-9]\d{2,4})\s*[xX×*]\s*([1-9]\d{2,4})(?!\d)", text)
    if direct:
        width, height = int(direct.group(1)), int(direct.group(2))
        if width >= 256 and height >= 256:
            return f"{width}x{height}"

    normalized = (
        text.replace("：", ":")
        .replace("﹕", ":")
        .replace("∶", ":")
        .replace("比", ":")
        .replace("／", "/")
        .replace("/", ":")
    )
    ratio_match = re.search(r"(?<!\d)(1|2|3|4|9|16)\s*:\s*(1|2|3|4|9|16)(?!\d)", normalized)
    if not ratio_match:
        return ""
    ratio = f"{int(ratio_match.group(1))}:{int(ratio_match.group(2))}"
    options = CHAT_RATIO_SIZE_OPTIONS.get(ratio)
    if not options:
        return ""
    width, height = parse_size_pair(current_size)
    wants_4k = bool(re.search(r"(?i)\b4\s*k\b|4K|超清|超高分辨率", text))
    wants_2k = bool(re.search(r"(?i)\b2\s*k\b|2K|高清|高分辨率", text))
    long_edge = max(width, height)
    if wants_4k or long_edge >= 2400:
        return options[2] if len(options) > 2 else options[-1]
    if wants_2k or long_edge >= 1500:
        return options[1] if len(options) > 1 else options[0]
    return options[0]

# GPT-Image-2 限制：长边最大 3840，主要受最大像素限制（约 829 万 = 3840x2160）。
# 这里只用于上游报错后给出友好的像素上限提示；不对尺寸做任何缩小（用户选什么就原样发送）。
GPT_IMAGE2_MAX_EDGE = 3840
GPT_IMAGE2_MAX_PIXELS = 8_294_400
GPT_IMAGE2_MIN_PIXELS = 655_360

def is_gpt_image_2_model(model):
    raw = str(model or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    return (
        normalized == "gpt-image-2"
        or normalized.startswith("gpt-image-2-")
        or normalized.endswith("-gpt-image-2")
        or "-gpt-image-2-" in normalized
        or compact == "gptimage2"
        or compact.startswith("gptimage2")
        or compact.endswith("gptimage2")
    )

def normalize_gpt_image_2_size(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        return size or "auto"
    # 已在 GPT 支持范围内（长边≤3840 且 总像素≤约829万）的尺寸原样返回，不做任何改动。
    if max(width, height) <= GPT_IMAGE2_MAX_EDGE and width * height <= GPT_IMAGE2_MAX_PIXELS:
        return f"{width}x{height}"
    # 超限时按比例等比缩小到 GPT 上限，保持原始宽高比（例如 4096x4096 → ~2864x2864，仍是 1:1）。
    ratio = width / height
    if ratio > 3:
        width = height * 3
    elif ratio < 1 / 3:
        height = width * 3
    scale = min(
        1.0,
        GPT_IMAGE2_MAX_EDGE / max(width, height),
        (GPT_IMAGE2_MAX_PIXELS / max(1, width * height)) ** 0.5,
    )
    width = max(16, int((width * scale) // 16) * 16)
    height = max(16, int((height * scale) // 16) * 16)
    if width * height < GPT_IMAGE2_MIN_PIXELS:
        grow = (GPT_IMAGE2_MIN_PIXELS / max(1, width * height)) ** 0.5
        width = int((width * grow + 15) // 16) * 16
        height = int((height * grow + 15) // 16) * 16
    return f"{width}x{height}"

def gpt_image_2_size_error_message(size):
    width, height = parse_size_pair(size)
    display_size = size or "未指定"
    return (
        f"GPT-Image-2 不支持当前尺寸 {display_size}：它有最大像素限制"
        "（长边最大 3840、总像素约 829 万）。请改用更小的尺寸，"
        "或切换到 nano-banana 生成更高分辨率。"
    )

def gpt_image_2_size_exceeds_supported(size):
    width, height = parse_size_pair(size)
    return bool(width and height and (max(width, height) > GPT_IMAGE2_MAX_EDGE or width * height > GPT_IMAGE2_MAX_PIXELS))

def apimart_size_resolution(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        raw = str(size or "").strip().lower()
        if raw in {"1k", "2k", "4k"}:
            return "1:1", raw
        if re.fullmatch(r"(auto|\d+\s*:\s*\d+)", raw):
            return raw.replace(" ", ""), "1k"
        return "1:1", "1k"
    long_edge = max(width, height)
    pixels = width * height
    if long_edge >= 3000 or pixels > 4_500_000:
        resolution = "4k"
    elif long_edge >= 1800 or pixels > 1_800_000:
        resolution = "2k"
    else:
        resolution = "1k"
    common = [
        (1, 1, "1:1"), (3, 2, "3:2"), (2, 3, "2:3"), (4, 3, "4:3"), (3, 4, "3:4"),
        (5, 4, "5:4"), (4, 5, "4:5"), (16, 9, "16:9"), (9, 16, "9:16"),
        (2, 1, "2:1"), (1, 2, "1:2"), (3, 1, "3:1"), (1, 3, "1:3"),
        (21, 9, "21:9"), (9, 21, "9:21"),
    ]
    ratio = width / height
    best = min(common, key=lambda item: abs(ratio - item[0] / item[1]))
    return best[2], resolution

VOLCENGINE_MIN_PIXELS = 3_686_400
VOLCENGINE_MIN_EDGE = 1536
VOLCENGINE_MAX_EDGE = 4096
VOLCENGINE_RATIO_CHOICES = [
    (1, 1, "1:1"),
    (4, 3, "4:3"),
    (3, 4, "3:4"),
    (16, 9, "16:9"),
    (9, 16, "9:16"),
    (21, 9, "21:9"),
    (9, 21, "9:21"),
    (3, 2, "3:2"),
    (2, 3, "2:3"),
    (5, 4, "5:4"),
    (4, 5, "4:5"),
]

def is_volcengine_seedream_model(model):
    value = str(model or "").strip().lower()
    return "seedream" in value or "doubao-seedream" in value

def normalize_volcengine_size(size, model=""):
    width, height = parse_size_pair(size)
    raw = str(size or "").strip().lower()
    if not width or not height:
        if raw == "4k":
            return "4096x4096"
        if raw == "2k":
            return "2048x2048"
        return "2048x2048" if is_volcengine_seedream_model(model) else (size or "1024x1024")
    if not is_volcengine_seedream_model(model):
        return f"{width}x{height}"
    ratio = width / max(1, height)
    best_ratio = min(VOLCENGINE_RATIO_CHOICES, key=lambda item: abs(ratio - item[0] / item[1]))
    rw, rh = best_ratio[0], best_ratio[1]
    scale = max(
        (VOLCENGINE_MIN_PIXELS / max(1, rw * rh)) ** 0.5,
        VOLCENGINE_MIN_EDGE / max(1, min(rw, rh)),
    )
    target_w = rw * scale
    target_h = rh * scale
    cap = min(1.0, VOLCENGINE_MAX_EDGE / max(target_w, target_h))
    target_w *= cap
    target_h *= cap
    snapped_w = max(64, int(target_w // 16) * 16)
    snapped_h = max(64, int(target_h // 16) * 16)
    while snapped_w * snapped_h < VOLCENGINE_MIN_PIXELS:
        if snapped_w <= snapped_h:
            snapped_w += 16
        else:
            snapped_h += 16
        if max(snapped_w, snapped_h) > VOLCENGINE_MAX_EDGE:
            break
    return f"{snapped_w}x{snapped_h}"



# --- Sliced from online.py lines 5328-5388 ---
async def generate_modelscope_provider_image(prompt, size, model, reference_images=None, provider=None):
    clean_token = modelscope_api_key()
    if not clean_token:
        raise HTTPException(status_code=400, detail="未配置 ModelScope API Key，请在 API 设置中填写。")
    width, height = parse_size_pair(size)
    refs = []
    for ref in (reference_images or [])[:ONLINE_IMAGE_REFERENCE_MAX]:
        if not ref.get("url"):
            continue
        # 本地参考图转为 data URL；前端已生成的 data URL 保持原样，贴近旧版稳定链路。
        refs.append(modelscope_image_url(ref.get("url", ""), max_size=1536))
    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true",
    }
    payload = {
        "model": selected_model(model, "Tongyi-MAI/Z-Image-Turbo"),
        "prompt": prompt.strip(),
    }
    if width and height:
        payload["width"] = width
        payload["height"] = height
        payload["size"] = f"{width}x{height}"
    if refs:
        payload["image_url"] = refs

    api_root = modelscope_image_api_root()
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=AI_REQUEST_TIMEOUT) as client:
        submit_res = await client.post(f"{api_root}/images/generations", headers=headers, json=payload)
        submit_res.raise_for_status()
        raw = submit_res.json()
        task_id = raw.get("task_id")
        if not task_id:
            try:
                return extract_image(raw), raw
            except HTTPException:
                raise HTTPException(status_code=502, detail=f"ModelScope 未返回 task_id：{raw}")

        deadline = time.monotonic() + AI_REQUEST_TIMEOUT
        last_payload = raw
        while time.monotonic() < deadline:
            await asyncio.sleep(IMAGE_POLL_INTERVAL)
            result = await client.get(
                f"{api_root}/tasks/{task_id}",
                headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
            )
            result.raise_for_status()
            data = result.json()
            last_payload = data
            status = str(data.get("task_status") or "").upper()
            if status == "SUCCEED":
                images = data.get("output_images") or []
                if not images:
                    raise HTTPException(status_code=502, detail=f"ModelScope 成功但没有返回图片：{data}")
                return {"type": "url", "value": images[0]}, data
            if status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                detail = data.get("error_info") or data.get("message") or data.get("detail") or str(data)
                raise HTTPException(status_code=502, detail=f"ModelScope 任务失败：{detail}")
        raise HTTPException(status_code=504, detail=f"ModelScope 生图任务超时：{last_payload}")



# --- Sliced from online.py lines 5389-5445 ---
def gemini_model_name(model):
    value = selected_model(model, "gemini-3-pro-image-preview").strip()
    return value[len("models/"):] if value.startswith("models/") else value

def gemini_endpoint_url(provider, model):
    model_name = urllib.parse.quote(gemini_model_name(model), safe="")
    if is_apimart_provider(provider) and not str((provider or {}).get("image_generation_endpoint") or "").strip():
        return f"https://api.apimart.ai/v1beta/models/{model_name}:generateContent"
    return provider_endpoint_url(provider, "image_generation_endpoint", f"/v1beta/models/{model_name}:generateContent")

def gemini_image_config(size):
    width, height = parse_size_pair(size)
    if not width or not height:
        raw = str(size or "").strip().upper()
        if raw in {"1K", "2K", "4K"}:
            return {"aspectRatio": "1:1", "imageSize": raw}
        if re.fullmatch(r"\d+\s*:\s*\d+", raw):
            return {"aspectRatio": raw.replace(" ", ""), "imageSize": "1K"}
        return {"aspectRatio": "1:1", "imageSize": "2K"}
    aspect_ratio, resolution = apimart_size_resolution(size)
    return {"aspectRatio": aspect_ratio, "imageSize": resolution.upper()}

def gemini_reference_part(ref):
    value = reference_to_data_url(ref, max_size=1536)
    if not value:
        return None
    if isinstance(value, str) and value.startswith("data:image/") and ";base64," in value:
        header, encoded = value.split(";base64,", 1)
        mime_type = header.replace("data:", "", 1) or "image/png"
        return {"inlineData": {"mimeType": mime_type, "data": encoded}}
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return {"fileData": {"mimeType": "image/png", "fileUri": value}}
    return None

async def generate_gemini_provider_image(prompt, size, model, reference_images=None, provider=None):
    model_name = gemini_model_name(model)
    endpoint = gemini_endpoint_url(provider, model_name)
    parts = [{"text": prompt.strip()}]
    for ref in (reference_images or [])[:ONLINE_IMAGE_REFERENCE_MAX]:
        part = gemini_reference_part(ref)
        if part:
            parts.append(part)
    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "imageConfig": gemini_image_config(size),
        },
    }
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=httpx.Timeout(connect=20.0, read=1800.0, write=120.0, pool=20.0)) as client:
        response = await client.post(endpoint, headers=api_headers(provider=provider), json=body)
        response.raise_for_status()
        # APIMart wraps the native Gemini body in {code, data}; unwrap it before
        # image extraction so candidates[].content.parts[].inlineData is visible.
        raw = unwrap_apimart_response(response.json()) if is_apimart_provider(provider) else response.json()
        return extract_image(raw), raw



async def generate_pollinations_provider_image(prompt, size, model, reference_images, provider):
    """Pollinations: GET https://image.pollinations.ai/prompt/{prompt}
    ?model=flux&width=&height=&seed=&nologo=true&safe=false (anonymous tier, no key).
    Returns (image_data, raw). Supports optional public reference image via &image=URL."""
    from urllib.parse import quote
    model_name = str(model or "flux").strip() or "flux"
    width, height = parse_size_pair(size)
    if not width or not height:
        try:
            width, height = (int(x) for x in str(size or "1024x1024").lower().split("x", 1))
        except Exception:
            width, height = 1024, 1024
    width = max(256, min(2048, width))
    height = max(256, min(2048, height))
    seed = random.randint(1, 2_147_483_000)

    clean_prompt = sanitize_video_prompt(prompt, "pollinations", model_name)
    url = (
        "https://image.pollinations.ai/prompt/" + quote(clean_prompt, safe="")
        + f"?model={quote(model_name)}&width={width}&height={height}&seed={seed}&nologo=true&safe=false"
    )

    if reference_images:
        for ref in reference_images:
            raw_url = str((ref.get("url") if isinstance(ref, dict) else ref) or "").strip()
            if raw_url.startswith(("http://", "https://")):
                parsed = urllib.parse.urlsplit(raw_url)
                host = (parsed.hostname or "").lower()
                if host not in {"127.0.0.1", "localhost", "::1"} and not re.match(r"^(192\.168\.|10\.|172\.(1[6-9]|2\d|3[01])\.)", host):
                    url += f"&image={quote(raw_url, safe='')}"
                    break

    headers = {"User-Agent": "Infinite-Canvas/1.0"}
    api_key = provider_env_key_value(provider["id"]) if isinstance(provider, dict) else ""
    if api_key and "…" not in api_key and "••••••••" not in api_key:
        headers["Authorization"] = bearer_auth_value(api_key)

    t0 = time.monotonic()
    try:
        image_bytes, content_type = await safe_download_bytes(
            url,
            timeout=240.0,
            max_bytes=50 * 1024 * 1024,
            headers=headers,
            allow_private=False,
        )
        log_upstream_call(
            "GET", url.split("?")[0], status=200,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            provider=provider.get("id") if isinstance(provider, dict) else "pollinations",
            model=model_name
        )
    except HTTPException:
        raise
    except Exception as exc:
        elapsed = int((time.monotonic() - t0) * 1000)
        log_upstream_call(
            "GET", url.split("?")[0], status=502,
            elapsed_ms=elapsed,
            provider=provider.get("id") if isinstance(provider, dict) else "pollinations",
            model=model_name
        )
        raise HTTPException(status_code=502, detail=f"Pollinations 图片获取失败: {exc}")

    if "image/" not in content_type:
        raise HTTPException(status_code=502, detail=f"Pollinations 返回非图片内容：{content_type}")

    image_data = {
        "type": "b64",
        "mime_type": content_type.split(";")[0].strip() or "image/jpeg",
        "value": base64.b64encode(image_bytes).decode("ascii"),
    }
    raw = {
        "id": f"poll-{seed}",
        "provider": provider.get("id") or "pollinations",
        "model": model_name,
        "seed": seed,
    }
    return image_data, raw



# --- Sliced from online.py lines 6664-6907 ---
async def generate_ai_image(prompt, size, quality, model, reference_images=None, provider_id="comfly", aspect_ratio="", resolution=""):
    provider = get_api_provider(provider_id)
    if is_tudou_provider(provider):
        model = tudou_image_model_for_request(model)
    if provider["id"] == "modelscope":
        return await generate_modelscope_provider_image(prompt, size, model, reference_images, provider)
    if effective_protocol(provider, model) == "pollinations" or (is_pollinations_provider(provider) and effective_protocol(provider, model) != "openai"):
        return await generate_pollinations_provider_image(prompt, size, model, reference_images, provider)
    if is_codex_provider(provider):
        return await generate_codex_provider_image(prompt, size, model, reference_images, provider)
    if is_gemini_cli_provider(provider):
        return await generate_gemini_cli_provider_image(prompt, size, model, reference_images, provider)
    if is_jimeng_provider(provider):
        return await generate_jimeng_provider_image(prompt, size, model, reference_images, provider)
    if is_runninghub_provider(provider):
        return await generate_runninghub_provider_image(prompt, size, model, reference_images, provider)
    if effective_protocol(provider, model) == "gemini":
        return await generate_gemini_provider_image(prompt, size, model, reference_images, provider)
    if is_volcengine_provider(provider):
        return await generate_volcengine_provider_image(prompt, size, model, reference_images, provider)
    if is_tudou_async_image_mode(provider, model):
        return await generate_tudou_async_image(prompt, size, quality, model, reference_images, provider, aspect_ratio, resolution)
    if is_tudou_provider(provider) and is_tudou_grok_image_model(model):
        return await generate_tudou_grok_image(prompt, size, model, reference_images, provider, aspect_ratio)
    is_gpt2 = is_gpt_image_2_model(model)
    is_apimart = is_apimart_provider(provider)
    # 不对 GPT 尺寸做任何缩小/拦截：用户选什么尺寸就原样发给上游；
    # 若超过 GPT 的最大像素限制被上游拒绝，再由 friendly_image_error_detail 给出友好的像素上限提示。
    quality = str(quality or "").strip().lower()
    if quality not in {"low", "medium", "high"}:
        quality = ""
    base_url = (provider.get("base_url") or AI_BASE_URL).rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    gen_url = provider_endpoint_url(provider, "image_generation_endpoint", "/v1/images/generations")
    edit_url = provider_endpoint_url(provider, "image_edit_endpoint", "/v1/images/edits")
    refs = [ref for ref in (reference_images or []) if ref.get("url")]
    mask_refs = [ref for ref in refs if str(ref.get("role") or "").strip().lower() == "mask" or str(ref.get("name") or "").lower().endswith("_mask.png")]
    image_refs = [ref for ref in refs if ref not in mask_refs]
    image_request_mode = effective_image_request_mode(provider, model)
    request_timeout = httpx.Timeout(connect=20.0, read=300.0, write=120.0, pool=20.0) if (is_gpt2 or is_apimart or image_request_mode in {"openai-json", "openai-video-proxy", "openai-responses"}) else httpx.Timeout(connect=15.0, read=min(float(AI_REQUEST_TIMEOUT), 300.0), write=60.0, pool=15.0)
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=request_timeout) as client:
        response = None
        async def post_openai_edits(edit_files=None):
            data = {"model": model, "prompt": prompt, "size": size}
            if quality:
                data["quality"] = quality
            return await client.post(
                edit_url,
                headers=api_headers(json_body=False, provider=provider, model=model),
                data=data,
                files=edit_files if edit_files is not None else {},
            )

        if image_request_mode == "openai-video-proxy":
            body = {
                "model": model,
                "prompt": prompt,
                "aspect_ratio": runninghub_aspect_from_size(size, "1:1"),
            }
            video_url = f"{base_url}/videos" if base_url.endswith("/v1") else f"{base_url}/v1/videos"
            refs_for_proxy = image_refs[:6]
            local_image_paths = [openai_video_proxy_local_image_path(ref) for ref in refs_for_proxy]
            has_local_images = any(local_image_paths)
            if has_local_images:
                form_data = [(key, value) for key, value in body.items()]
                for ref, local_path in zip(refs_for_proxy, local_image_paths):
                    if local_path:
                        continue
                    url = await openai_video_proxy_public_reference_url(ref)
                    if url:
                        form_data.append(("images", url))
                files = []
                opened = []
                try:
                    for local_path in local_image_paths:
                        if not local_path:
                            continue
                        fh = open(local_path, "rb")
                        opened.append(fh)
                        files.append(("images", (os.path.basename(local_path), fh, content_type_for_path(local_path))))
                    response = await client.post(
                        video_url,
                        headers=api_headers(json_body=False, provider=provider, model=model),
                        data=form_data,
                        files=files,
                    )
                finally:
                    for fh in opened:
                        fh.close()
            else:
                if refs_for_proxy:
                    body["images"] = [await openai_video_proxy_public_reference_url(ref) for ref in refs_for_proxy]
                response = await httpx_request_with_transient_retries(
                    client,
                    "POST",
                    video_url,
                    attempts=2,
                    headers=api_headers(provider=provider, model=model),
                    json=body,
                )
        elif image_request_mode == "openai-responses":
            tool = {"type": "image_generation"}
            tool["action"] = "edit" if image_refs else "generate"
            if size and str(size).strip().lower() != "auto":
                tool["size"] = responses_proxy_tool_size(size)
            if quality:
                tool["quality"] = quality
            size_instruction = responses_image_size_instruction(size)
            input_text = f"{size_instruction}\n\n{prompt}" if size_instruction else prompt
            content = [{"type": "input_text", "text": input_text}]
            force_public_refs = bool(locked_recommended_provider_rule(provider.get("id"), provider.get("name"), base_url))
            for ref in image_refs[:ONLINE_IMAGE_REFERENCE_MAX]:
                image_url = await responses_input_image_url(ref, require_public_url=force_public_refs)
                if image_url:
                    content.append({"type": "input_image", "image_url": image_url})
            body = {
                "model": model,
                "input": [{"role": "user", "content": content}],
                "tools": [tool],
                "tool_choice": {"type": "image_generation"},
            }
            responses_url = provider_endpoint_url(provider, "image_generation_endpoint", "/v1/responses")
            response = await post_openai_responses(client, responses_url, api_headers(provider=provider, model=model), body)
        elif image_request_mode == "openai-json":
            # Agnes 等“OpenAI JSON 图片接口”统一走 /images/generations：
            # 不使用 /images/edits，不传顶层 response_format/n/quality；
            # 文生图只传 extra_body.response_format，图生图把参考图放进 extra_body.image。
            extra_body = {"response_format": "url"}
            if image_refs:
                extra_body["image"] = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:ONLINE_IMAGE_REFERENCE_MAX]]
            body = {"model": model, "prompt": prompt, "size": size, "extra_body": extra_body}
            response = await client.post(gen_url, headers=api_headers(provider=provider, model=model), json=body)
        elif is_apimart:
            apimart_size, resolution = apimart_size_resolution(size)
            # APIMart 的 GPT-Image-2 图生图仍走 /images/generations，
            # 通过 image_urls 传参考图，不使用 OpenAI multipart /images/edits。
            body = {
                "model": model,
                "prompt": prompt,
                "n": 1,
                "size": apimart_size,
                "resolution": resolution,
                "official_fallback": False,
            }
            if image_refs:
                body["image_urls"] = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:ONLINE_IMAGE_REFERENCE_MAX]]
            response = await client.post(gen_url, headers=api_headers(provider=provider, model=model), json=body)
        elif is_gpt2 and not image_refs and not mask_refs:
            body = {"model": model, "prompt": prompt, "size": size}
            if quality:
                body["quality"] = quality
            response = await client.post(gen_url, headers=api_headers(provider=provider, model=model), json=body)
            if response.status_code >= 400 and images_api_unsupported(response):
                response = await post_openai_edits()
        elif image_refs:
            # 1) OpenAI 协议的图生图/编辑用 multipart 提交到 /images/edits；
            # GPT-Image-2 参考图不能走 /images/generations JSON，否则部分平台会忽略原图或报 Images API unsupported。
            files = []
            opened = []
            edit_failed_status = None
            edit_failed_text = ""
            try:
                for ref in image_refs[:ONLINE_IMAGE_REFERENCE_MAX]:
                    path = output_file_from_url(ref.get("url", ""))
                    if not path:
                        continue
                    fh = open(path, "rb")
                    opened.append(fh)
                    files.append(("image", (os.path.basename(path), fh, content_type_for_path(path))))
                if mask_refs:
                    mask_path = output_file_from_url(mask_refs[0].get("url", ""))
                    if mask_path:
                        fh = open(mask_path, "rb")
                        opened.append(fh)
                        files.append(("mask", (os.path.basename(mask_path), fh, content_type_for_path(mask_path))))
                try:
                    response = await post_openai_edits(files)
                    if response.status_code >= 400:
                        edit_failed_status = response.status_code
                        edit_failed_text = response.text[:500]
                        response = None
                except httpx.HTTPError as e:
                    edit_failed_status = -1
                    edit_failed_text = str(e)
                    response = None
            finally:
                for fh in opened:
                    fh.close()
            # 2) edits 失败 → 非 GPT-Image-2 可回退到 /images/generations + JSON image:[urls/base64]（grsai 风格）
            if response is None:
                if is_gpt2:
                    raise HTTPException(
                        status_code=502,
                        detail=f"GPT-Image-2 编辑接口 /images/edits 调用失败：{edit_failed_text[:300] or edit_failed_status}。已停止自动重试，避免上游可能已扣费后再次请求。"
                    )
                logger.info(f"/images/edits failed ({edit_failed_status}): {edit_failed_text[:200]} → 回退到 /images/generations + image:[] JSON")
                image_payload = [reference_to_data_url(ref, max_size=1536) for ref in image_refs[:ONLINE_IMAGE_REFERENCE_MAX]]
                body = {
                    "model": model, "prompt": prompt, "size": size,
                    "response_format": "url", "n": 1,
                    "image": image_payload,
                }
                if quality:
                    body["quality"] = quality
                response = await client.post(gen_url, headers=api_headers(provider=provider, model=model), json=body)
                if response.status_code >= 400 and images_api_unsupported(response):
                    raise HTTPException(
                        status_code=502,
                        detail=f"编辑接口 /images/edits 调用失败，且该平台不支持 /images/generations：{edit_failed_text[:300] or edit_failed_status}"
                    )
        else:
            body = {"model": model, "prompt": prompt, "size": size, "response_format": "url", "n": 1}
            if quality:
                body["quality"] = quality
            response = await client.post(
                gen_url,
                headers=api_headers(provider=provider, model=model),
                json=body,
            )
            if response.status_code >= 400 and images_api_unsupported(response):
                response = await post_openai_edits()
        response.raise_for_status()
        raw = response.json()
        try:
            return extract_image(raw), raw
        except HTTPException as exc:
            if image_request_mode == "openai-responses":
                fallback_image = responses_output_text_image(raw)
                if fallback_image:
                    return fallback_image, raw
                try:
                    logger.warning(f"RS 响应中没有图片，原始返回（截断）：{json.dumps(raw, ensure_ascii=False)[:800]}")
                except Exception:
                    pass
                raise HTTPException(status_code=502, detail=responses_no_image_detail(raw) or exc.detail)
            task_id = extract_task_id(raw)
            if not task_id:
                raise
        try:
            task_result = await wait_for_image_task(client, task_id, provider)
            return extract_image(task_result), task_result
        except HTTPException as exc:
            setattr(exc, "upstream_task_id", task_id)
            raise



# --- Sliced from online.py lines 7252-7425 ---
@router.post("/api/upload")
async def upload_image(files: List[UploadFile] = File(...)):
    uploaded_files = []
    files_content = []
    for file in files:
        content = await file.read()
        files_content.append((file, content))

    for file, content in files_content:
        success_count = 0
        last_result = None
        for addr in COMFYUI_INSTANCES:
            try:
                files_data = {'image': (file.filename, content, file.content_type)}
                response = await asyncio.to_thread(
                    requests.post,
                    f"http://{addr}/upload/image",
                    files=files_data,
                    proxies={"http": None, "https": None},
                    timeout=15,
                )
                if response.status_code == 200:
                    last_result = response.json()
                    success_count += 1
            except Exception as e:
                logger.warning(f"Upload error for {addr}: {e}")

        if success_count > 0 and last_result:
            comfy_name = last_result.get("name", file.filename)
            try:
                lp = output_path_for(os.path.basename(comfy_name), "input")
                os.makedirs(os.path.dirname(lp), exist_ok=True)
                with open(lp, "wb") as fp:
                    fp.write(content)
            except Exception:
                pass
            uploaded_files.append({"comfy_name": comfy_name})
        else:
            instances_str = ", ".join(COMFYUI_INSTANCES) if COMFYUI_INSTANCES else "127.0.0.1:8188"
            raise HTTPException(status_code=503, detail=f"未连接到本地 ComfyUI 服务 ({instances_str})。请先启动本地 ComfyUI，或在「工作流设置」中检查服务地址。")

    return {"files": uploaded_files}

def slim_image_bytes(data: bytes, filename: str = ""):
    """Return slimmed (bytes, ext) for large images; passthrough otherwise.

    Rules: only JPEG/PNG/WEBP get slimmed; long edge > 2048 or size > 3MB
    triggers downscale to long edge 2048 + JPEG q85 (PNG keeps alpha via WEBP
    fallback avoided for compatibility -> PNG stays PNG but re-encoded)."""
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return data, None
    lower = str(filename or "").lower()
    if lower.endswith((".png", ".jpg", ".jpeg", ".webp")) or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff":
        pass
    else:
        return data, None
    if len(data) <= 3 * 1024 * 1024:
        try:
            with Image.open(BytesIO(data)) as img:
                w, h = img.size
            if max(w, h) <= 2048:
                return data, None
        except Exception:  # noqa: BLE001
            return data, None
    try:
        with Image.open(BytesIO(data)) as img:
            img.load()
            has_alpha = img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info)
            w, h = img.size
            if max(w, h) > 2048:
                img.thumbnail((2048, 2048), Image.LANCZOS)
            buf = BytesIO()
            if has_alpha:
                img = img.convert("RGBA")
                img.save(buf, format="PNG", optimize=True)
                return buf.getvalue(), ".png"
            img = img.convert("RGB")
            img.save(buf, format="JPEG", quality=85, optimize=True)
            return buf.getvalue(), ".jpg"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"image slim failed, keep original: {exc}")
        return data, None


@router.post("/api/ai/upload")
async def upload_ai_reference(files: List[UploadFile] = File(...)):
    uploaded = []
    image_exts = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
    video_exts = {".mp4", ".webm", ".mov", ".m4v", ".flv"}
    audio_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    doc_exts = {".pdf", ".txt", ".md", ".markdown", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".json", ".zip", ".yaml", ".yml", ".log"}
    max_upload_bytes = 50 * 1024 * 1024
    for file in files:
        chunks = []
        total_read = 0
        chunk_size = 1024 * 1024  # 1MB
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total_read += len(chunk)
            if total_read > max_upload_bytes:
                raise HTTPException(status_code=413, detail=f"{file.filename or '文件'} 超过 50MB，无法上传")
            chunks.append(chunk)
        if not chunks:
            continue
        content = b"".join(chunks)
        ext = os.path.splitext(file.filename or "")[1].lower()
        content_type = (file.content_type or "").lower()
        kind = "image"
        if ext in video_exts or content_type.startswith("video/"):
            kind = "video"
            if ext not in video_exts:
                ext = ".webm" if "webm" in content_type else ".mov" if "quicktime" in content_type else ".mp4"
        elif ext in audio_exts or content_type.startswith("audio/"):
            kind = "audio"
            if ext not in audio_exts:
                ext = ".wav" if "wav" in content_type else ".ogg" if "ogg" in content_type else ".m4a" if "mp4" in content_type else ".mp3"
        elif ext in image_exts or content_type.startswith("image/"):
            kind = "image"
            if ext not in image_exts:
                ext = ".jpg" if "jpeg" in content_type else ".webp" if "webp" in content_type else ".gif" if "gif" in content_type else ".png"
        elif ext in doc_exts or content_type.startswith(("text/", "application/")):
            kind = "file"
            if not ext:
                ext = mimetypes.guess_extension(content_type) or ".bin"
        else:
            kind = "file"
            if not ext:
                ext = ".bin"
        if kind == "image":
            slimmed, new_ext = slim_image_bytes(content, file.filename or ext)
            if new_ext and new_ext != ext:
                ext = new_ext
                content = slimmed
            elif new_ext:
                content = slimmed
            if slimmed is not content:
                content = slimmed
            orig_kb = max(1, len(content) // 1024)
        filename = f"ai_ref_{uuid.uuid4().hex[:12]}{ext}"
        path = output_path_for(filename, "input")
        with open(path, "wb") as f:
            f.write(content)
        uploaded.append({"url": output_url_for(filename, "input"), "name": file.filename or filename, "kind": kind, "mime": content_type})
    return {"files": uploaded}

class Base64UploadRequest(BaseModel):
    data: str = Field(default="", max_length=70 * 1024 * 1024)            # 限制最多约 70MB 字符串（对应 50MB 原始二进制）
    name: str = Field(default="", max_length=255)
    content_type: str = Field(default="", max_length=100)

@router.post("/api/ai/upload-base64")
async def upload_ai_base64(payload: Base64UploadRequest):
    """以 base64 JSON 方式上传字节到 assets/input，返回 /assets 地址。
    给不便用 multipart/FormData 的客户端（如 PS UXP 面板）用——UXP 的 fetch+FormData 经常发不出有效 multipart。"""
    raw = (payload.data or "").strip()
    ct = (payload.content_type or "").split(";", 1)[0].strip().lower()
    if raw.startswith("data:"):
        header, _, raw = raw.partition(",")
        if not ct:
            ct = header[5:].split(";", 1)[0].strip().lower()
    try:
        content = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="数据无法解码")
    if not content:
        raise HTTPException(status_code=400, detail="内容为空")
    if len(content) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="超过 50MB")
    kind, ext = _local_upload_kind_ext(payload.name or "", ct or "image/png")
    if kind is None:
        kind, ext = "image", ".png"
    if kind == "image":
        slimmed, slim_ext = slim_image_bytes(content, payload.name or ext)
        if slimmed is not None:
            content = slimmed
            if slim_ext and slim_ext != ext:
                ext = slim_ext
    filename = f"ai_ref_{uuid.uuid4().hex[:12]}{ext}"
    path = output_path_for(filename, "input")
    with open(path, "wb") as f:
        f.write(content)
    url = output_url_for(filename, "input")
    return {"url": url, "files": [{"url": url, "name": payload.name or filename, "kind": kind}]}



# --- Sliced from online.py lines 7430-7434 ---
@router.post("/api/temp-sh/upload")
async def temp_sh_upload(payload: TempShUploadRequest, request: Request):
    ensure_same_origin_request(request)
    return await upload_local_video_to_cloud(payload.url, "auto")



# --- Sliced from online.py lines 7440-7449 ---
@router.post("/api/ai/import-local-image")
async def import_local_ai_reference(payload: LocalImageImportRequest, request: Request):
    ensure_same_origin_request(request)
    requested = [payload.path] if payload.path else []
    requested.extend(payload.paths or [])
    requested = [p for p in requested if str(p or "").strip()][:20]
    if not requested:
        raise HTTPException(status_code=400, detail="没有可导入的本地图片")
    return {"files": [import_local_image_file(normalize_local_image_path(path)) for path in requested]}



# --- Sliced from online.py lines 8970-9071 ---
async def build_online_image_result(payload: OnlineImageRequest):
    provider = get_api_provider(payload.provider_id)
    default_model = (provider.get("image_models") or [IMAGE_MODEL])[0]
    model = selected_model(payload.model, default_model)
    request_size = snap_size_to_multiple(payload.size, 16)
    refs = [dump_pydantic(ref) for ref in payload.reference_images if ref.url]
    image_refs = image_references(refs)
    count = max(1, min(8, int(payload.n or 1)))
    operation = str(payload.operation or "").strip().lower()
    if operation == "upscale":
        if not is_jimeng_provider(provider):
            raise HTTPException(status_code=400, detail="图片放大目前仅支持即梦（Dreamina）平台")
        if not image_refs:
            raise HTTPException(status_code=400, detail="请先选择要放大的图片")
        count = 1
    async def generate_one():
        if operation == "upscale":
            image_data, raw_item = await generate_jimeng_upscale_image(image_refs, payload.resolution_type)
        else:
            image_data, raw_item = await generate_ai_image(
                payload.prompt, request_size, payload.quality, model, image_refs, provider["id"],
                payload.aspect_ratio, payload.resolution,
            )
        try:
            image_items = extract_images(raw_item) if isinstance(raw_item, dict) else [image_data]
        except HTTPException:
            image_items = [image_data]
        local_urls = []
        local_items = []
        for item in image_items:
            local_url = await save_ai_image_to_output(item, prefix="online_")
            if local_url:
                local_urls.append(local_url)
                local_items.append(image_output_meta(local_url, item))
        return local_urls, local_items, raw_item
    try:
        generated = await asyncio.gather(*(generate_one() for _ in range(count)))
    except httpx.HTTPStatusError as exc:
        log_net_error(f"生图 HTTP状态错误 provider={provider.get('id')} model={model} size={request_size}", exc)
        text = exc.response.text or ''
        friendly = friendly_image_error_detail(text, request_size, model)
        detail = friendly or f"上游生图接口错误：{text[:300]}"
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        log_net_error(f"生图 网络/TLS错误 provider={provider.get('id')} model={model}", exc)
        raise HTTPException(status_code=502, detail=f"请求上游生图接口失败：{exc}") from exc

    local_urls = [url for urls, _items, _raw in generated for url in (urls or []) if url]
    local_items = [item for _urls, items, _raw in generated for item in (items or []) if item.get("url")]
    raw = generated[0][2] if generated else {}
    if not local_urls:
        provider_name = provider.get("name") or provider["id"]
        raw_text = json.dumps(raw, ensure_ascii=False)[:800] if isinstance(raw, (dict, list)) else str(raw)[:800]
        raise HTTPException(status_code=502, detail=f"{provider_name} 没有返回图片：{raw_text}")
    result = {
        "prompt": payload.prompt,
        "images": local_urls,
        "image_items": local_items,
        "timestamp": time.time(),
        "type": "online",
        "model": model,
        "provider_id": provider["id"],
        "provider_name": provider.get("name") or provider["id"],
        "task_id": extract_task_id(raw) if isinstance(raw, dict) else None,
        "request_id": raw.get("id") if isinstance(raw, dict) else None,
        "params": {"provider_id": provider["id"], "model": model, "size": request_size, "requested_size": payload.size, "quality": payload.quality, "n": count, "reference_images": refs},
        "raw_usage": raw.get("usage") if isinstance(raw, dict) else None,
    }
    save_to_history(result)
    loop = get_global_loop()
    if loop:
        asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), loop)
    return result

@router.post("/api/online-image")
async def online_image(payload: OnlineImageRequest):
    _t0 = time.monotonic()
    try:
        result = await _online_image_dispatch(payload)
    except HTTPException as exc:
        record_task_event(getattr(payload, 'provider_id', ''), getattr(payload, 'model', ''), 'failed',
                          duration_ms=int((time.monotonic() - _t0) * 1000),
                          error=str(exc.detail)[:500], kind='image')
        raise
    except Exception as exc:  # noqa: BLE001
        record_task_event(getattr(payload, 'provider_id', ''), getattr(payload, 'model', ''), 'failed',
                          duration_ms=int((time.monotonic() - _t0) * 1000),
                          error=str(exc), kind='image')
        raise
    try:
        outputs = []
        if isinstance(result, dict):
            outputs = result.get('image_urls') or result.get('urls') or result.get('images') or []
            if isinstance(outputs, str):
                outputs = [outputs]
        record_task_event(getattr(payload, 'provider_id', ''), getattr(payload, 'model', ''), 'success',
                          task_id=(result or {}).get('task_id', '') if isinstance(result, dict) else '',
                          duration_ms=int((time.monotonic() - _t0) * 1000),
                          outputs=outputs, kind='image', raw=result.get("raw") if isinstance(result, dict) else None,
                          prompt=getattr(payload, 'prompt', ''))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f'task record failed: {exc}')
    return result



# --- Sliced from online.py lines 9112-9208 ---
def _meigen_aspect_from_size(size):
    """'1024x1024' -> '1:1'; '1344x768' -> closest supported ratio."""
    try:
        width, height = (int(x) for x in str(size).lower().split("x", 1))
    except Exception:  # noqa: BLE001
        return "1:1"
    if width <= 0 or height <= 0:
        return "1:1"
    candidates = {"1:1": 1.0, "16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3,
                  "3:4": 3 / 4, "3:2": 3 / 2, "2:3": 2 / 3, "21:9": 21 / 9, "2.35:1": 2.35}
    ratio = width / height
    return min(candidates.items(), key=lambda kv: abs(kv[1] - ratio))[0]


async def generate_meigen_image(payload, provider):
    """MeiGen Cloud image generation via MCP tools/call generate_image.
    Supports reference images (URL or local path -> base64 dataURL)."""
    token = meigen_env_key(provider)
    if not token:
        raise HTTPException(status_code=401, detail="MeiGen API token 缺失：请在 API/.env 配置 MEIGEN_API_TOKEN（meigen_sk_...）")
    model = str(payload.model or "flux2-klein").strip()
    session = "dx-" + uuid.uuid4().hex[:12]
    rpc_id = 10

    def _rpc(method, params, rid, notify=False):
        return _meigen_rpc(method, params, rid, token, session=session, notify=notify)

    await _rpc("initialize", {"protocolVersion": "2026-07-28", "capabilities": {},
                        "clientInfo": {"name": "infinite-canvas", "version": "1.0"}}, rpc_id)
    rpc_id += 1
    try:
        await _rpc("notifications/initialized", None, None, notify=True)
    except Exception:  # noqa: BLE001
        pass
    rpc_id += 1

    arguments = {"prompt": sanitize_video_prompt(payload.prompt, "meigen", model),
                 "model": model, "attemptId": uuid.uuid4().hex}
    if payload.size:
        arguments["aspectRatio"] = _meigen_aspect_from_size(payload.size)
    resolution_value = getattr(payload, "resolution", "")
    if resolution_value:
        arguments["resolution"] = str(resolution_value).upper()
    quality_value = getattr(payload, "quality", "")
    if quality_value and str(quality_value) != "auto":
        arguments["quality"] = str(quality_value)
    seed_value = getattr(payload, "seed", None)
    if seed_value:
        arguments["seed"] = seed_value

    refs = []
    for ref in (getattr(payload, "reference_images", None) or [])[:4]:
        url = str(getattr(ref, "url", "") or "").strip()
        if not url:
            continue
        if url.startswith("http"):
            refs.append(url)
        else:
            data_url = await asyncio.to_thread(reference_to_data_url, {"url": url}, 1536)
            if data_url:
                refs.append(data_url)
    if refs:
        arguments["referenceImages"] = refs[:4]

    t0 = time.monotonic()
    try:
        res = await _rpc("tools/call", {"name": "generate_image", "arguments": arguments}, rpc_id)
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text[:200]
        raise HTTPException(status_code=502, detail=f"MeiGen 生成失败：HTTP {exc.response.status_code} {detail}") from exc

    text_out = "\n".join(c.get("text", "") for c in res.get("result", {}).get("content", []) if c.get("type") == "text")
    image_urls = re.findall(r"https://[^\s\)\"']+", text_out)
    image_urls = [u for u in image_urls if re.search(r"\.(png|jpe?g|webp)(\?|$)", u, re.I) or "images.meigen" in u or "r2" in u or "pub-" in u]
    if not image_urls:
        for c in res.get("result", {}).get("content", []):
            if c.get("type") == "image" and c.get("data"):
                image_data = {"type": "b64", "mime_type": c.get("mimeType", "image/png"),
                              "value": c["data"]}
                url_out = await save_ai_image_to_output(image_data, prefix="meigen_")
                image_urls = [url_out]
                break
    if not image_urls:
        raise HTTPException(status_code=502, detail=f"MeiGen 未返回图片：{text_out[:300]}")

    first = image_urls[0]
    if first.startswith("http"):
        try:
            raw_bytes, ctype = await safe_download_bytes(
                first,
                timeout=120.0,
                max_bytes=50 * 1024 * 1024,
                headers={"User-Agent": _MEIGEN_UA},
                allow_private=False,
            )
            mime = (ctype or "image/png").split(";")[0].strip()
            image_data = {"type": "b64", "mime_type": mime,
                          "value": base64.b64encode(raw_bytes).decode("ascii")}
            url_out = await save_ai_image_to_output(image_data, prefix="meigen_")
            image_urls = [url_out]
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"下载 MeiGen 图片失败: {e}")
    return image_urls


# --- Sliced from online.py lines 9295-9364 ---
async def generate_pollinations_image(payload, provider):
    """Pollinations: GET https://image.pollinations.ai/prompt/{prompt}
    ?model=flux&width=&height=&seed=&nologo=true  (anonymous tier, no key).
    Returns image bytes directly (no async task)."""
    refs = [dump_pydantic(ref) for ref in (getattr(payload, "reference_images", None) or []) if getattr(ref, "url", None)]
    image_data, raw = await generate_pollinations_provider_image(
        payload.prompt, payload.size, payload.model, refs, provider
    )
    url_out = await save_ai_image_to_output(image_data, prefix="pollinations_")
    return {"images": [url_out], "image_urls": [url_out], "task_id": raw.get("id", f"poll-{uuid.uuid4().hex[:8]}"), "raw": raw}


async def _online_image_dispatch(payload):
    if is_pollinations_provider(payload.provider_id):
        return await build_online_image_result(payload)
    try:
        provider = get_api_provider(payload.provider_id)
    except HTTPException:
        provider = {"id": getattr(payload, "provider_id", "meigen") or "meigen", "name": "MeiGen Cloud",
                    "base_url": "https://www.meigen.ai", "protocol": "meigen"}
    if is_meigen_provider(provider) or is_meigen_provider(getattr(payload, "provider_id", "")):
        model = str(getattr(payload, "model", "") or (provider.get("image_models") or ["nanobanana2"])[0])
        prompt = str(getattr(payload, "prompt", "") or "")
        image_urls = await generate_meigen_image(payload, provider)
        if isinstance(image_urls, dict):
            image_urls = image_urls.get("image_urls") or image_urls.get("images") or []
        image_items, task_id = [{"url": u, "kind": "image"} for u in image_urls], f"meigen_{uuid.uuid4().hex[:8]}"
        pid, pname = provider.get("id", "meigen"), provider.get("name", "MeiGen")
        save_to_history({
            "prompt": prompt, "images": image_urls, "media_items": image_items, "mode": "online",
            "provider_id": pid, "provider_name": pname, "model": model,
            "timestamp": int(time.time() * 1000), "params": {"model": model, "provider_id": pid},
        })
        return {
            "status": "succeeded", "provider_id": pid, "provider_name": pname, "model": model,
            "images": image_urls, "image_urls": image_urls, "image_items": image_items,
            "task_id": task_id, "upstream_task_id": task_id, "raw": {},
        }
    return await build_online_image_result(payload)


# --- Sliced from online.py lines 9383-9843 ---
def apimart_midjourney_provider(provider_id: str):
    provider = get_api_provider_exact(provider_id)
    if not is_apimart_provider(provider):
        raise HTTPException(status_code=400, detail="Midjourney 节点仅支持已配置为 APIMart 协议的平台。")
    # This also validates the configured key, while all MJ requests below keep
    # using APIMart's official host instead of inheriting a custom image URL.
    api_headers(provider=provider)
    return provider

def midjourney_response_data(raw):
    if not isinstance(raw, dict):
        return {}
    data = raw.get("data")
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return next((item for item in data if isinstance(item, dict)), raw)
    return raw

def midjourney_task_id(raw):
    data = midjourney_response_data(raw)
    for key in ("task_id", "taskId", "id"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""

def midjourney_task_status(raw):
    data = midjourney_response_data(raw)
    return str(data.get("status") or data.get("task_status") or "").strip().upper()

def midjourney_error_detail(raw, fallback="Midjourney 请求失败"):
    data = midjourney_response_data(raw)
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    return str(
        error.get("message")
        or data.get("message")
        or data.get("fail_reason")
        or (raw.get("message") if isinstance(raw, dict) else "")
        or fallback
    )

def midjourney_remote_images(raw):
    data = midjourney_response_data(raw)
    values = data.get("image_urls") or data.get("imageUrls") or []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        values = []
    urls = [str(value or "").strip() for value in values if str(value or "").strip()]
    if not urls:
        result = data.get("result") if isinstance(data.get("result"), dict) else {}
        result_images = result.get("images") if isinstance(result.get("images"), list) else []
        for item in result_images:
            value = str((item or {}).get("url") if isinstance(item, dict) else item or "").strip()
            if value:
                urls.append(value)
    if not urls:
        for key in ("image_url", "imageUrl", "grid_image_url", "gridImageUrl"):
            value = str(data.get(key) or "").strip()
            if value:
                urls.append(value)
    return list(dict.fromkeys(urls))

async def midjourney_reference_urls(reference_images):
    urls = []
    for ref in (reference_images or [])[:4]:
        value = str((ref or {}).get("url") or "").strip()
        if value.startswith(("http://", "https://", "data:image/")):
            urls.append(value)
            continue
        data_url = reference_to_data_url(ref, max_size=1536)
        if data_url.startswith("data:image/"):
            urls.append(data_url)
    return urls

async def midjourney_modal_mask_url(reference):
    """Convert this canvas's white edit-mask convention to APIMart's alpha mask.
    Existing remote masks are treated as already compliant; locally generated
    `role=mask` images have white editable pixels and are inverted to transparent.
    """
    if not reference:
        return ""
    ref = dump_pydantic(reference) if isinstance(reference, AIReference) else dict(reference)
    value = str(ref.get("url") or "").strip()
    if value.startswith(("http://", "https://")):
        return value
    data_url = reference_to_data_url(ref, max_size=2048)
    if not data_url.startswith("data:image/"):
        return ""
    try:
        _header, encoded = data_url.split(",", 1)
        with Image.open(BytesIO(base64.b64decode(encoded))) as image:
            luminance = ImageOps.exif_transpose(image).convert("L")
            rgba = Image.new("RGBA", luminance.size, (255, 255, 255, 255))
            rgba.putalpha(ImageOps.invert(luminance))
            out = BytesIO()
            rgba.save(out, format="PNG")
        return f"data:image/png;base64,{base64.b64encode(out.getvalue()).decode('ascii')}"
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"无法读取 Midjourney 遮罩图片：{exc}") from exc

async def apimart_midjourney_request(provider, path, body):
    timeout = httpx.Timeout(connect=20.0, read=180.0, write=120.0, pool=20.0)
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=timeout, follow_redirects=True) as client:
        response = await client.post(
            f"{APIMART_MIDJOURNEY_API_ROOT}{path}",
            headers=api_headers(provider=provider),
            json=body,
        )
    try:
        raw = response.json()
    except ValueError:
        raw = {"message": response.text[:500]}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=midjourney_error_detail(raw, f"Midjourney 接口错误（{response.status_code}）"))
    task_id = midjourney_task_id(raw)
    if not task_id:
        raise HTTPException(status_code=502, detail=f"Midjourney 未返回任务 ID：{json.dumps(raw, ensure_ascii=False)[:500]}")
    return raw, task_id

async def midjourney_result(provider, task_id: str):
    safe_task_id = str(task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", safe_task_id):
        raise HTTPException(status_code=400, detail="Midjourney 任务 ID 不合法。")
    timeout = httpx.Timeout(connect=20.0, read=180.0, write=60.0, pool=20.0)
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            f"{APIMART_MIDJOURNEY_API_ROOT}/v1/midjourney/{urllib.parse.quote(safe_task_id, safe='')}",
            headers=api_headers(provider=provider),
        )
    try:
        raw = response.json()
    except ValueError:
        raw = {"message": response.text[:500]}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=midjourney_error_detail(raw, f"查询 Midjourney 任务失败（{response.status_code}）"))
    status = midjourney_task_status(raw)
    if status in IMAGE_TASK_FAILED_STATUSES:
        return {"status": "failed", "task_id": safe_task_id, "error": midjourney_error_detail(raw), "raw": raw}
    if status not in IMAGE_TASK_SUCCESS_STATUSES:
        return {"status": "running", "task_id": safe_task_id, "message": "Midjourney 任务仍在生成中", "raw": raw}
    remote_urls = midjourney_remote_images(raw)
    if not remote_urls:
        return {"status": "failed", "task_id": safe_task_id, "error": "Midjourney 任务成功但没有返回图片。", "raw": raw}
    local_urls, image_items = [], []
    for remote_url in remote_urls:
        local_url = await save_ai_image_to_output({"type": "url", "value": remote_url}, prefix="midjourney_")
        if local_url:
            local_urls.append(local_url)
            item = image_output_meta(local_url)
            item["mj_task_id"] = safe_task_id
            image_items.append(item)
    return {
        "status": "succeeded",
        "task_id": safe_task_id,
        "images": local_urls,
        "image_items": image_items,
        "provider_id": provider["id"],
        "provider_name": provider.get("name") or provider["id"],
        "raw": raw,
    }

@router.post("/api/midjourney/submit")
async def submit_midjourney(payload: MidjourneySubmitRequest):
    provider = apimart_midjourney_provider(payload.provider_id)
    speed = str(payload.speed or "relax").strip().lower()
    if speed not in MIDJOURNEY_SPEEDS:
        raise HTTPException(status_code=400, detail="Midjourney 速度仅支持 relax、fast 或 turbo。")
    size = str(payload.size or "1:1").strip()
    if not re.fullmatch(r"\d{1,2}:\d{1,2}", size):
        raise HTTPException(status_code=400, detail="Midjourney 画幅应为宽:高，例如 16:9。")
    mode = str(payload.mode or "imagine").strip().lower()
    if mode not in {"imagine", "blend", "edit"}:
        raise HTTPException(status_code=400, detail="不支持的 Midjourney 节点模式。")
    image_urls = await midjourney_reference_urls([dump_pydantic(ref) for ref in payload.reference_images if ref.url])
    prompt = str(payload.prompt or "").strip()
    if mode == "blend":
        if not 2 <= len(image_urls) <= 4:
            raise HTTPException(status_code=400, detail="Midjourney 多图融合需要连接 2 到 4 张图片。")
        body = {"image_urls": image_urls, "size": size, "speed": speed, "metadata": {"source": "infinite-canvas"}}
        path = "/v1/midjourney/generations/blend"
    elif mode == "edit":
        if not prompt:
            raise HTTPException(status_code=400, detail="Midjourney 图片编辑需要提示词。")
        if not image_urls:
            raise HTTPException(status_code=400, detail="Midjourney 图片编辑需要连接至少一张图片。")
        body = {"prompt": prompt, "image_urls": image_urls, "size": size, "speed": speed, "metadata": {"source": "infinite-canvas"}}
        path = "/v1/midjourney/generations/edits"
    else:
        if not prompt:
            raise HTTPException(status_code=400, detail="Midjourney 文生图需要提示词。")
        body = {"prompt": prompt, "size": size, "version": str(payload.version or "6.1").strip()[:24], "speed": speed, "metadata": {"source": "infinite-canvas"}}
        if image_urls:
            body["image_urls"] = image_urls
        path = "/v1/midjourney/generations"
    raw, task_id = await apimart_midjourney_request(provider, path, body)
    return {"task_id": task_id, "status": midjourney_task_status(raw) or "queued", "provider_id": provider["id"], "mode": mode, "raw": raw}

@router.post("/api/midjourney/actions")
async def submit_midjourney_action(payload: MidjourneyActionRequest):
    provider = apimart_midjourney_provider(payload.provider_id)
    action = str(payload.action or "").strip().lower()
    if action not in MIDJOURNEY_ACTION_PATHS:
        raise HTTPException(status_code=400, detail="不支持的 Midjourney 操作。")
    task_id = str(payload.task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", task_id):
        raise HTTPException(status_code=400, detail="Midjourney 任务 ID 不合法。")
    speed = str(payload.speed or "relax").strip().lower()
    if speed not in MIDJOURNEY_SPEEDS:
        raise HTTPException(status_code=400, detail="Midjourney 速度仅支持 relax、fast 或 turbo。")
    body = {"task_id": task_id, "speed": speed, "metadata": {"source": "infinite-canvas"}}
    custom_id = str(payload.custom_id or "").strip()
    if custom_id:
        body["custom_id"] = custom_id
    if action in {"upscale", "variation", "low_variation", "high_variation", "remix_subtle", "remix_strong"} and not custom_id:
        index = int(payload.index or 0)
        if index not in {1, 2, 3, 4}:
            raise HTTPException(status_code=400, detail="Midjourney 图片序号应为 1 到 4。")
        body["index"] = index
    elif action in {"zoom", "pan"} and int(payload.index or 0) in {1, 2, 3, 4}:
        body["index"] = int(payload.index)
    if action == "zoom" and payload.zoom_ratio is not None:
        zoom_ratio = float(payload.zoom_ratio)
        if zoom_ratio <= 1 or zoom_ratio > 4:
            raise HTTPException(status_code=400, detail="Midjourney 缩放比例应大于 1 且不超过 4。")
        body["zoom_ratio"] = zoom_ratio
    if action == "pan" and not custom_id:
        direction = str(payload.direction or "").strip().lower()
        if direction not in {"left", "right", "up", "down"}:
            raise HTTPException(status_code=400, detail="Midjourney 平移方向仅支持 left、right、up 或 down。")
        body["direction"] = direction
    if action in {"remix_subtle", "remix_strong"}:
        prompt = str(payload.prompt or "").strip()
        if prompt:
            body["prompt"] = prompt
    raw, new_task_id = await apimart_midjourney_request(provider, MIDJOURNEY_ACTION_PATHS[action], body)
    return {"task_id": new_task_id, "status": midjourney_task_status(raw) or "queued", "provider_id": provider["id"], "action": action, "raw": raw}

@router.post("/api/midjourney/modal")
async def submit_midjourney_modal(payload: MidjourneyModalRequest):
    provider = apimart_midjourney_provider(payload.provider_id)
    task_id = str(payload.task_id or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,240}", task_id):
        raise HTTPException(status_code=400, detail="Midjourney 任务 ID 不合法。")
    speed = str(payload.speed or "relax").strip().lower()
    if speed not in MIDJOURNEY_SPEEDS:
        raise HTTPException(status_code=400, detail="Midjourney 速度仅支持 relax、fast 或 turbo。")
    mask_url = await midjourney_modal_mask_url(payload.mask_image)
    if not mask_url:
        raise HTTPException(status_code=400, detail="Midjourney 局部重绘需要连接一个遮罩图片节点。")
    body = {
        "task_id": task_id,
        "prompt": str(payload.prompt or "").strip(),
        "mask_url": mask_url,
        "speed": speed,
        "metadata": {"source": "infinite-canvas"},
    }
    raw, submitted_task_id = await apimart_midjourney_request(provider, "/v1/midjourney/generations/modal", body)
    return {"task_id": submitted_task_id, "status": midjourney_task_status(raw) or "submitted", "provider_id": provider["id"], "raw": raw}

@router.get("/api/midjourney/tasks/{task_id}")
async def get_midjourney_task(task_id: str, provider_id: str):
    provider = apimart_midjourney_provider(provider_id)
    return await midjourney_result(provider, task_id)

@router.post("/api/image-task-query")
async def query_image_task(payload: ImageTaskQueryRequest):
    provider = get_api_provider(payload.provider_id)
    task_id = str(payload.task_id or "").strip()
    if is_runninghub_provider(provider):
        api_key = runninghub_api_key(provider)
        url = runninghub_endpoint_url(provider, "/task/openapi/outputs")
        try:
            async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=httpx.Timeout(connect=20.0, read=240.0, write=30.0, pool=20.0)) as client:
                response = await client.post(url, headers=runninghub_app_headers(True), json={"apiKey": api_key, "taskId": task_id})
                response.raise_for_status()
                raw = response.json()
                code = raw.get("code") if isinstance(raw, dict) else None
                if code in (0, "0"):
                    local_urls = []
                    local_items = []
                    for remote in runninghub_extract_outputs(raw.get("data")):
                        try:
                            local_url = await runninghub_store_remote_output(client, remote)
                        except Exception:
                            local_url = rewrite_runninghub_file_url(remote)
                        if local_url:
                            local_urls.append(local_url)
                            local_items.append(image_output_meta(local_url))
                    result = {
                        "status": "succeeded",
                        "prompt": "",
                        "images": local_urls,
                        "image_items": local_items,
                        "timestamp": time.time(),
                        "type": "online",
                        "model": "",
                        "provider_id": provider["id"],
                        "provider_name": provider.get("name") or provider["id"],
                        "task_id": task_id,
                        "request_id": "",
                        "params": {"provider_id": provider["id"]},
                        "raw": raw,
                    }
                    save_to_history(result)
                    loop = get_global_loop()
                    if loop:
                        asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), loop)
                    return result
                if code in (805, "805"):
                    return {
                        "status": "failed",
                        "task_id": task_id,
                        "provider_id": provider["id"],
                        "provider_name": provider.get("name") or provider["id"],
                        "error": runninghub_fail_reason(raw),
                        "raw": raw,
                    }
                return {
                    "status": "running",
                    "task_id": task_id,
                    "provider_id": provider["id"],
                    "provider_name": provider.get("name") or provider["id"],
                    "message": "RunningHub 任务仍在生成中",
                    "raw": raw,
                }
        except httpx.HTTPStatusError as exc:
            text = exc.response.text or ""
            raise HTTPException(status_code=exc.response.status_code, detail=f"查询 RunningHub 任务失败：{text[:300]}") from exc
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"查询 RunningHub 任务失败：{exc}") from exc
    timeout = httpx.Timeout(connect=20.0, read=300.0, write=60.0, pool=20.0)
    try:
        async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=timeout, follow_redirects=True) as client:
            raw = await fetch_image_task_payload(client, task_id, provider)
    except httpx.HTTPStatusError as exc:
        log_net_error(f"查询生图任务 HTTP状态错误 provider={provider.get('id')} task_id={task_id}", exc)
        text = exc.response.text or ""
        raise HTTPException(status_code=exc.response.status_code, detail=f"查询上游生图任务失败：{text[:300]}") from exc
    except httpx.HTTPError as exc:
        log_net_error(f"查询生图任务 网络/TLS错误 provider={provider.get('id')} task_id={task_id}", exc)
        raise HTTPException(status_code=502, detail=f"查询上游生图任务失败：{exc}") from exc

    status = image_task_status(raw)
    image_items = []
    try:
        image_items = extract_images(raw)
    except HTTPException:
        image_items = []
    if image_items:
        local_urls = []
        local_items = []
        for item in image_items:
            local_url = await save_ai_image_to_output(item, prefix="online_")
            if local_url:
                local_urls.append(local_url)
                local_items.append(image_output_meta(local_url, item))
        result = {
            "status": "succeeded",
            "prompt": "",
            "images": local_urls,
            "image_items": local_items,
            "timestamp": time.time(),
            "type": "online",
            "model": "",
            "provider_id": provider["id"],
            "provider_name": provider.get("name") or provider["id"],
            "task_id": task_id,
            "request_id": raw.get("id") if isinstance(raw, dict) else "",
            "params": {"provider_id": provider["id"]},
            "raw": raw,
        }
        save_to_history(result)
        loop = get_global_loop()
        if loop:
            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(result), loop)
        return result
    if status in IMAGE_TASK_FAILED_STATUSES:
        return {
            "status": "failed",
            "task_id": task_id,
            "provider_id": provider["id"],
            "provider_name": provider.get("name") or provider["id"],
            "error": image_task_fail_reason(raw),
            "raw": raw,
        }
    return {
        "status": "running",
        "task_id": task_id,
        "provider_id": provider["id"],
        "provider_name": provider.get("name") or provider["id"],
        "message": "任务仍在生成中",
        "raw": raw,
    }

async def run_canvas_image_task(task_id: str, payload: OnlineImageRequest):
    _t0 = time.monotonic()
    with CANVAS_TASK_LOCK:
        if task_id in CANVAS_TASKS:
            CANVAS_TASKS[task_id]["status"] = "running"
            CANVAS_TASKS[task_id]["updated_at"] = time.time()
    try:
        result = await asyncio.wait_for(
            _online_image_dispatch(payload),
            timeout=CANVAS_IMAGE_TASK_TIMEOUT,
        )
        duration_ms = int((time.monotonic() - _t0) * 1000)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "succeeded",
                "result": result,
                "error": "",
                "updated_at": time.time(),
            })
        try:
            outputs = []
            if isinstance(result, dict):
                outputs = result.get('images') or result.get('image_urls') or []
                if isinstance(outputs, str):
                    outputs = [outputs]
            record_task_event(
                getattr(payload, 'provider_id', ''),
                getattr(payload, 'model', ''),
                'success',
                task_id=task_id,
                duration_ms=duration_ms,
                outputs=outputs,
                raw=result.get("raw") if isinstance(result, dict) else None,
                kind='image',
                prompt=getattr(payload, 'prompt', ''),
            )
        except Exception as exc:
            logger.warning(f"task record failed: {exc}")
    except asyncio.TimeoutError:
        duration_ms = int((time.monotonic() - _t0) * 1000)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "failed",
                "error": "生图任务超时（超过 5 分钟未响应），已自动中止。请检查所选渠道/模型是否正常或更换模型。",
                "status_code": 504,
                "upstream_task_id": "",
                "updated_at": time.time(),
            })
        try:
            record_task_event(
                getattr(payload, 'provider_id', ''),
                getattr(payload, 'model', ''),
                'failed',
                task_id=task_id,
                duration_ms=duration_ms,
                error="生图任务超时（超过 5 分钟未响应）",
                kind='image',
            )
        except Exception:
            pass
    except JimengPendingError as exc:
        # 即梦云端还在排队：标记为 jimeng_pending，前端据 submit_id 持久续查（任务未丢失）
        info = jimeng_pending_payload(exc)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "jimeng_pending",
                "jimeng_pending": True,
                "submit_id": exc.submit_id,
                "kind": exc.kind,
                "queue_info": exc.queue_info,
                "message": info["message"],
                "error": "",
                "updated_at": time.time(),
            })
    except Exception as exc:
        duration_ms = int((time.monotonic() - _t0) * 1000)
        detail = getattr(exc, "detail", None) or str(exc)
        status_code = getattr(exc, "status_code", 500)
        upstream_task_id = getattr(exc, "upstream_task_id", "") or extract_task_id_from_text(detail)
        with CANVAS_TASK_LOCK:
            CANVAS_TASKS[task_id].update({
                "status": "failed",
                "error": str(detail),
                "status_code": status_code,
                "upstream_task_id": upstream_task_id,
                "updated_at": time.time(),
            })
        try:
            record_task_event(
                getattr(payload, 'provider_id', ''),
                getattr(payload, 'model', ''),
                'failed',
                task_id=task_id,
                duration_ms=duration_ms,
                error=str(detail)[:500],
                kind='image',
            )
        except Exception:
            pass

@router.post("/api/canvas-image-tasks")
async def create_canvas_image_task(payload: OnlineImageRequest):
    task_id = f"canvas_img_{uuid.uuid4().hex}"
    with CANVAS_TASK_LOCK:
        _prune_canvas_tasks_locked()
        CANVAS_TASKS[task_id] = {
            "id": task_id,
            "type": "online-image",
            "status": "queued",
            "created_at": time.time(),
            "updated_at": time.time(),
            "result": None,
            "error": "",
            "provider_id": payload.provider_id,
            "model": payload.model,
        }
    async def _guarded_task():
        async with _CANVAS_TASK_SEMAPHORE:
            await run_canvas_image_task(task_id, payload)
    asyncio.create_task(_guarded_task())
    return {"task_id": task_id, "status": "queued"}

@router.get("/api/canvas-image-tasks/{task_id}")
async def get_canvas_image_task(task_id: str):
    with CANVAS_TASK_LOCK:
        task = dict(CANVAS_TASKS.get(task_id) or {})
    if not task:
        raise HTTPException(status_code=404, detail="画布任务不存在，可能服务已重启或任务已过期")
    return task



# --- Sliced from online.py lines 9847-9916 ---
IMAGE_PARAM_RATIOS = [
    {"value": "1:1", "label": "1:1"},
    {"value": "3:4", "label": "3:4"},
    {"value": "4:3", "label": "4:3"},
    {"value": "16:9", "label": "16:9"},
    {"value": "9:16", "label": "9:16"},
    {"value": "2:3", "label": "2:3"},
    {"value": "3:2", "label": "3:2"},
]
IMAGE_PARAM_RESOLUTIONS = [
    {"value": "1k", "label": "1K"},
    {"value": "2k", "label": "2K"},
    {"value": "4k", "label": "4K"},
]

def build_image_param_fields(engine: str, provider: dict, model: str):
    """返回某平台/引擎的图像生成参数字段定义。客户端按 type 动态渲染并回填到生成请求。
    字段 key 直接对应 OnlineImageRequest 的字段名（size/quality/n/reference_images）。"""
    gpt_auto_size = engine == "api" and is_gpt_image_2_model(model)
    image_resolutions = ([{"value": "auto", "label": "自动"}] + IMAGE_PARAM_RESOLUTIONS) if gpt_auto_size else IMAGE_PARAM_RESOLUTIONS
    size_field = {
        "key": "size", "type": "size", "label": "尺寸",
        "ratios": IMAGE_PARAM_RATIOS, "resolutions": image_resolutions,
        "default": {"ratio": "1:1", "resolution": "auto" if gpt_auto_size else "1k"},
    }
    count_field = {
        "key": "n", "type": "int", "label": "数量", "control": "chips",
        "options": [1, 2, 3, 4], "default": 1,
    }
    refs_field = {"key": "reference_images", "type": "refs", "label": "参考图", "max": ONLINE_IMAGE_REFERENCE_MAX}

    if engine == "runninghub":
        # RunningHub 参数按 app/工作流动态，需先选工作流再用 /api/runninghub/workflow-info 拉字段。
        return [{"key": "_rh_notice", "type": "notice",
                 "label": "RunningHub 工作流参数将按所选工作流动态加载（开发中）。"}]

    fields = [size_field]
    if engine in ("api", "volcengine"):
        fields.append({
            "key": "quality", "type": "select", "label": "质量", "control": "chips",
            "options": [
                {"value": "auto", "label": "自动"},
                {"value": "low", "label": "低"},
                {"value": "medium", "label": "中"},
                {"value": "high", "label": "高"},
            ],
            "default": "auto",
        })
    fields.append(count_field)
    fields.append(refs_field)
    return fields

@router.get("/api/image-params")
async def image_params(provider_id: str = "", model: str = ""):
    providers = load_api_providers()
    provider = next((p for p in providers if p.get("id") == (provider_id or "").strip().lower()), None) or {}
    if is_runninghub_provider(provider):
        engine = "runninghub"
    elif (provider_id or "").strip().lower() == "modelscope":
        engine = "modelscope"
    elif is_volcengine_provider(provider):
        engine = "volcengine"
    else:
        engine = "api"
    return {
        "engine": engine,
        "submit": "/api/canvas-image-tasks",
        "fields": build_image_param_fields(engine, provider, model),
    }



# --- Sliced from online.py lines 10639-10718 ---
def tudou_grok_image_size(size, aspect_ratio=""):
    value = str(size or "").strip().lower().replace("*", "x").replace("×", "x")
    if value in TUDOU_GROK_IMAGE_SIZES:
        return value
    if str(aspect_ratio or "").strip() in TUDOU_GROK_IMAGE_ASPECT_SIZES:
        return TUDOU_GROK_IMAGE_ASPECT_SIZES[str(aspect_ratio).strip()]
    match = re.match(r"^(\d+)\s*x\s*(\d+)$", value)
    if match:
        width, height = int(match.group(1)), int(match.group(2))
        if width and height:
            ratio = width / height
            choices = {"1024x1024": 1.0, "1280x720": 16 / 9, "720x1280": 9 / 16, "1792x1024": 3 / 2, "1024x1792": 2 / 3}
            return min(choices, key=lambda candidate: abs(choices[candidate] - ratio))
    return "1024x1024"

def tudou_api_endpoint(base_url, path):
    base = str(base_url or "").rstrip("/")
    return f"{base}{path}" if base.endswith("/v1") else f"{base}/v1{path}"

def tudou_reference_urls(ref):
    source = ref if isinstance(ref, dict) else {}
    urls = []
    for key in ("url", "original_url", "originalLocalUrl", "source_url"):
        value = str(source.get(key) or "").strip()
        if value and value not in urls:
            urls.append(value)
    return urls

async def generate_tudou_grok_image(prompt, size, model, reference_images, provider, aspect_ratio=""):
    """Use Tudou's Grok multipart edit route without changing generic OpenAI image behavior."""
    model = selected_model(model, "grok-imagine-image")
    base_url = str(provider.get("base_url") or "").rstrip("/")
    if not base_url:
        raise HTTPException(status_code=400, detail=f"{provider.get('name') or provider['id']} 未配置 Base URL")
    refs = [item for item in (reference_images or []) if isinstance(item, dict) and item.get("url")]
    is_edit = "edit" in model.lower() or bool(refs)
    timeout = httpx.Timeout(connect=20.0, read=600.0, write=120.0, pool=20.0)
    async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=timeout, follow_redirects=True) as client:
        if is_edit:
            edit_model = model if "edit" in model.lower() else "grok-imagine-image-edit"
            parts = [(key, (None, str(value))) for key, value in {
                "model": edit_model,
                "prompt": str(prompt or ""),
                "n": "1",
                "size": "1024x1024",
                "response_format": "url",
            }.items()]
            attached = 0
            for ref in refs[:7]:
                for value in tudou_reference_urls(ref):
                    if value.lower().startswith("asset://"):
                        continue
                    ref_file = await yuli_fetch_reference_bytes(client, value)
                    if ref_file:
                        parts.append(("image[]", ref_file))
                        attached += 1
                        break
            if attached <= 0:
                raise HTTPException(status_code=400, detail="土豆 Grok 图像编辑没有成功读取参考图。请使用本地图片、公网图片 URL 或 data:image;base64。")
            response = await client.post(
                tudou_api_endpoint(base_url, "/images/edits"),
                headers=api_headers(json_body=False, provider=provider, model=edit_model),
                files=parts,
            )
        else:
            response = await client.post(
                tudou_api_endpoint(base_url, "/images/generations"),
                headers=api_headers(json_body=True, provider=provider, model=model),
                json={"model": model, "prompt": str(prompt or ""), "n": 1, "size": tudou_grok_image_size(size, aspect_ratio), "response_format": "url"},
            )
        response.raise_for_status()
        try:
            raw = response.json()
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"土豆 Grok 图像接口返回非 JSON 响应：{(response.text or '')[:500]}") from exc
    images = extract_images(raw)
    if not images:
        raise HTTPException(status_code=502, detail=f"土豆 Grok 图像接口没有返回图片：{str(raw)[:400]}")
    return images[0], raw



async def _safe_download_image_to_local(img_url: str, filename_prefix: str = "cloud_angle_") -> str:
    if not img_url:
        return ""
    if img_url.startswith("/output/") or img_url.startswith("/assets/"):
        return img_url
    try:
        from core.security import safe_download_bytes
        content, ctype = await safe_download_bytes(
            img_url,
            timeout=60.0,
            max_bytes=50 * 1024 * 1024,
            allow_private=False,
        )
        ext = "jpg" if "jpeg" in (ctype or "").lower() else "png"
        filename = f"{filename_prefix}{int(time.time())}_{uuid.uuid4().hex[:6]}.{ext}"
        file_path = output_path_for(filename, "output")
        with open(file_path, "wb") as f:
            f.write(content)
        return output_url_for(filename, "output")
    except Exception as e:
        logger.warning(f"下载上游图片失败: {e}; url={img_url}")
        return img_url


# --- Sliced from online.py lines 12181-12538 ---
@router.post("/api/angle/poll_status")
async def poll_angle_cloud(req: CloudPollRequest):
    api_root = modelscope_image_api_root()
    clean_token = modelscope_api_key(req.api_key)
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    task_id = req.task_id
    logger.info(f"Resuming polling for Angle Task: {task_id}")

    try:
        async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=30) as client:
            for i in range(300):
                await asyncio.sleep(2)
                result = await client.get(
                    f"{api_root}/tasks/{task_id}",
                    headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                )
                result.raise_for_status()
                data = result.json()
                status = str(data.get("task_status") or "").upper()

                if status == "SUCCEED":
                    img_url = data["output_images"][0]
                    local_path = await _safe_download_image_to_local(img_url, "cloud_angle_")
                    record = {"timestamp": time.time(), "prompt": f"Resumed {task_id}", "images": [local_path], "type": "angle"}
                    save_to_history(record)
                    if req.client_id:
                        await manager.send_personal_message({"type": "cloud_status", "status": "SUCCEED", "task_id": task_id}, req.client_id)
                    return {"url": local_path}

                elif status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                    if req.client_id:
                        await manager.send_personal_message({"type": "cloud_status", "status": "FAILED", "task_id": task_id}, req.client_id)
                    raise HTTPException(status_code=502, detail=f"ModelScope task failed: {data}")

                if i % 5 == 0 and req.client_id:
                    await manager.send_personal_message({
                        "type": "cloud_status", "status": f"{status} ({i}/300)",
                        "task_id": task_id, "progress": i, "total": 300
                    }, req.client_id)

            if req.client_id:
                await manager.send_personal_message({"type": "cloud_status", "status": "TIMEOUT", "task_id": task_id}, req.client_id)
            return {"status": "timeout", "task_id": task_id, "message": "Task still pending"}

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Angle polling error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/api/angle/generate")
async def generate_angle_cloud(req: CloudGenRequest):
    api_root = modelscope_image_api_root()
    clean_token = modelscope_api_key(req.api_key)
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    model = selected_model(req.model, "Qwen/Qwen-Image-Edit-2511")
    payload = {
        "model": model,
        "prompt": req.prompt.strip(),
        "image_url": [modelscope_image_url(url, max_size=1536) for url in req.image_urls]
    }
    if req.resolution:
        payload["size"] = modelscope_size(req.resolution)
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=30) as client:
            submit_res = await client.post(f"{api_root}/images/generations", headers=headers, json=payload)
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except Exception:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            logger.info(f"Angle Task submitted, ID: {task_id}")

            for i in range(300):
                await asyncio.sleep(2)
                result = await client.get(
                    f"{api_root}/tasks/{task_id}",
                    headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                )
                result.raise_for_status()
                data = result.json()
                status = str(data.get("task_status") or "").upper()

                if status == "SUCCEED":
                    img_url = data["output_images"][0]
                    local_path = await _safe_download_image_to_local(img_url, "cloud_angle_")
                    record = {"timestamp": time.time(), "prompt": req.prompt, "images": [local_path], "type": "angle"}
                    save_to_history(record)
                    if req.client_id:
                        await manager.send_personal_message({"type": "cloud_status", "status": "SUCCEED", "task_id": task_id}, req.client_id)
                    loop = get_global_loop()
                    if loop:
                        asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(record), loop)
                    return {"url": local_path, "task_id": task_id}

                elif status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                    if req.client_id:
                        await manager.send_personal_message({"type": "cloud_status", "status": "FAILED", "task_id": task_id}, req.client_id)
                    raise HTTPException(status_code=502, detail=f"ModelScope task failed: {data}")

                if i % 5 == 0 and req.client_id:
                    await manager.send_personal_message({
                        "type": "cloud_status", "status": f"{status} ({i}/300)",
                        "task_id": task_id, "progress": i, "total": 300
                    }, req.client_id)

            if req.client_id:
                await manager.send_personal_message({"type": "cloud_status", "status": "TIMEOUT", "task_id": task_id}, req.client_id)
            return {"status": "timeout", "task_id": task_id, "message": "Task still pending"}

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Angle generation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ModelScope Z-Image 云端生图 ---

@router.post("/generate")
async def generate_cloud(req: CloudGenRequest):
    api_root = modelscope_image_api_root()
    clean_token = modelscope_api_key(req.api_key)
    if not clean_token:
        raise HTTPException(status_code=400, detail="未提供 ModelScope API Key")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "Tongyi-MAI/Z-Image-Turbo",
        "prompt": req.prompt.strip(),
        "size": modelscope_size(req.resolution),
        "n": 1
    }
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=30) as client:
            submit_res = await client.post(
                f"{api_root}/images/generations",
                headers={**headers, "X-ModelScope-Async-Mode": "true"},
                json=payload
            )
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except Exception:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            logger.info(f"Z-Image Task submitted, ID: {task_id}")

            for i in range(200):
                await asyncio.sleep(3)
                result = await client.get(
                    f"{api_root}/tasks/{task_id}",
                    headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                )
                result.raise_for_status()
                data = result.json()
                status = str(data.get("task_status") or "").upper()

                if i % 5 == 0:
                    logger.info(f"Task {task_id} status check {i}: {status}")

                if status == "SUCCEED":
                    img_url = data["output_images"][0]
                    local_path = await _safe_download_image_to_local(img_url, "cloud_")
                    record = {"timestamp": time.time(), "prompt": req.prompt, "images": [local_path], "type": "cloud"}
                    save_to_history(record)
                    try:
                        await manager.broadcast_new_image(record)
                    except Exception:
                        pass
                    return {"url": local_path}

                elif status in {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}:
                    raise HTTPException(status_code=502, detail=f"ModelScope task failed: {data}")

            raise Exception("Cloud generation timeout")

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"Cloud generation error: {e}")
        raise HTTPException(status_code=400, detail=str(e))

# --- ModelScope 通用图片生成（支持图生图） ---

@router.post("/api/ms/generate")
async def ms_generate(req: MsGenerateRequest):
    api_root = modelscope_image_api_root()
    clean_token = modelscope_api_key(req.api_key)
    if not clean_token:
        raise HTTPException(status_code=400, detail="未配置 ModelScope API Key，请在 API 设置中填写，或重新保存 ModelScope Token。")

    headers = {
        "Authorization": f"Bearer {clean_token}",
        "Content-Type": "application/json",
        "X-ModelScope-Async-Mode": "true"
    }
    payload = {
        "model": req.model,
        "prompt": req.prompt.strip(),
    }
    if req.width and req.height:
        payload["width"] = req.width
        payload["height"] = req.height
        payload["size"] = modelscope_size(req.size or f"{req.width}x{req.height}")
    elif req.size:
        payload["size"] = modelscope_size(req.size)
    if req.image_urls:
        payload["image_url"] = [modelscope_image_url(url, max_size=1536) for url in req.image_urls]
    if req.loras is not None:
        payload["loras"] = req.loras

    try:
        async with httpx.AsyncClient(event_hooks={"response": [_upstream_response_hook], "request": [_upstream_request_hook]}, timeout=30) as client:
            submit_res = await client.post(
                f"{api_root}/images/generations",
                headers=headers,
                json=payload
            )
            if submit_res.status_code != 200:
                try:
                    detail = submit_res.json()
                except Exception:
                    detail = submit_res.text
                raise HTTPException(status_code=submit_res.status_code, detail=detail)

            task_id = submit_res.json().get("task_id")
            logger.info(f"MS Generate Task submitted ({req.model}), ID: {task_id}")

            TERMINAL_FAILED_STATUSES = {"FAILED", "FAIL", "ERROR", "CANCELED", "CANCELLED", "TIMEOUT", "REVOKED"}

            for i in range(300):
                await asyncio.sleep(2)
                try:
                    result = await client.get(
                        f"{api_root}/tasks/{task_id}",
                        headers={**headers, "X-ModelScope-Task-Type": "image_generation"},
                    )
                    data = result.json()
                    status = data.get("task_status")
                    logger.info(f"MS Task {task_id} poll {i}: status={status}")

                    if status == "SUCCEED":
                        img_url = data["output_images"][0]
                        prefix = f"ms_{req.model.replace('/', '_').replace(':', '_')}_"
                        local_path = await _safe_download_image_to_local(img_url, prefix)
                        record = {
                            "timestamp": time.time(),
                            "prompt": req.prompt,
                            "images": [local_path],
                            "type": "klein",
                            "model": req.model,
                        }
                        save_to_history(record)
                        loop = get_global_loop()
                        if loop:
                            asyncio.run_coroutine_threadsafe(manager.broadcast_new_image(record), loop)
                        return {"url": local_path, "task_id": task_id}

                    elif status in TERMINAL_FAILED_STATUSES:
                        error_info = data.get("error_info") or data.get("message") or data.get("detail") or str(data)
                        raise HTTPException(status_code=502, detail=f"MS task {status}: {error_info}")

                except HTTPException:
                    raise
                except Exception as loop_e:
                    logger.warning(f"MS polling error: {loop_e}")
                    continue

            raise HTTPException(status_code=504, detail="MS 生图超时")

    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"MS generate error: {e}")
        raise HTTPException(status_code=400, detail=str(e))


class CanvasEditRequest(BaseModel):
    source_image_url: str
    prompt: str
    provider_id: Optional[str] = None
    model: Optional[str] = None
    region: Optional[Dict[str, Any]] = None


@router.post("/api/canvas-edit")
async def api_canvas_edit(payload: CanvasEditRequest):
    if not str(payload.prompt or "").strip():
        raise HTTPException(status_code=400, detail="提示词不能为空")
    if not str(payload.source_image_url or "").strip():
        raise HTTPException(status_code=400, detail="缺少源图片地址")
    url = str(payload.source_image_url).strip()
    if url.startswith(("http://", "https://")):
        try:
            validate_safe_url(url, allow_private=False)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    from core.image_edit import execute_canvas_edit
    return await execute_canvas_edit(payload.model_dump(), history_saver=save_to_history)
