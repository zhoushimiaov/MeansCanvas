"""Canvas and Project Management Router for Infinite-Canvas.

Decoupled from monolithic main.py to provide clean, modular, and high-performance
canvas persistence, metadata management, project categorization, and trash lifecycle.
"""

import os
import re
import json
import uuid
import time
import logging
import asyncio
from threading import Lock, RLock
from typing import Dict, Any, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.canvas_cache import CanvasCache

logger = logging.getLogger("infinite_canvas.canvas")

router = APIRouter(tags=["canvas"])

# ===== 目录与路径常量 =====
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CANVAS_DIR = os.path.join(DATA_DIR, "canvases")
RHINO_UPLOAD_DIR = os.path.join(BASE_DIR, "assets", "uploads", "rhino")
PROJECTS_PATH = os.path.join(DATA_DIR, "projects.json")
DEFAULT_PROJECT_ID = "default"
CANVAS_TRASH_RETENTION_MS = 30 * 24 * 60 * 60 * 1000
CANVAS_COLORS = {"", "red", "orange", "amber", "green", "teal", "blue", "violet", "pink", "slate"}

# Reentrant so a single request can hold the lock across the whole
# load -> version-check -> save transaction while the reentrant load_canvas /
# save_canvas helpers (which also take CANVAS_LOCK) run inside it. (P0 concurrency fix)
CANVAS_LOCK = RLock()

os.makedirs(CANVAS_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(RHINO_UPLOAD_DIR, exist_ok=True)

_latest_rhino_capture: Optional[Dict[str, Any]] = None
RHINO_LOCK = Lock()

# ===== WebSocket 广播注入钩子 =====
_ws_manager = None

def set_ws_manager(mgr):
    """注入 SafeConnectionManager 实例以支持多端实时协同广播。"""
    global _ws_manager
    _ws_manager = mgr

async def _broadcast_canvas_updated(canvas_id: str, updated_at: int, client_id: str = ""):
    if _ws_manager and hasattr(_ws_manager, "broadcast_canvas_updated"):
        try:
            await _ws_manager.broadcast_canvas_updated(canvas_id, updated_at, client_id)
        except Exception as exc:
            logger.debug(f"Failed to broadcast canvas update: {exc}")

# ===== Pydantic 数据传输模型 =====
class CanvasCreateRequest(BaseModel):
    title: str = "未命名画布"
    icon: str = "layers"
    kind: str = "classic"
    project: Optional[str] = None
    board_x: Optional[float] = None
    board_y: Optional[float] = None

class CanvasMetaUpdate(BaseModel):
    title: Optional[str] = None
    icon: Optional[str] = None
    owner: Optional[str] = None
    color: Optional[str] = None
    pinned: Optional[bool] = None
    project: Optional[str] = None
    board_x: Optional[float] = None
    board_y: Optional[float] = None
    base_updated_at: Optional[int] = None

class ProjectCreateRequest(BaseModel):
    name: str = "新项目"

class ProjectUpdateRequest(BaseModel):
    name: Optional[str] = None
    order: Optional[int] = None

class CanvasSaveRequest(BaseModel):
    title: str = "未命名画布"
    icon: str = "layers"
    nodes: List[Dict[str, Any]] = []
    connections: List[Dict[str, Any]] = []
    viewport: Dict[str, Any] = {}
    logs: List[Dict[str, Any]] = []
    settings: Dict[str, Any] = {}
    client_id: str = ""
    base_updated_at: int = 0

# ===== 基础工具函数 =====
def now_ms() -> int:
    return int(time.time() * 1000)

def normalize_canvas_kind(kind="classic") -> str:
    return "smart" if str(kind or "").strip().lower() == "smart" else "classic"

def normalize_canvas_color(value) -> str:
    color = str(value or "").strip().lower()
    return color if color in CANVAS_COLORS else ""

def canvas_path(canvas_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", canvas_id or "")
    if not cleaned:
        raise HTTPException(status_code=400, detail="无效的画布 ID")
    return os.path.join(CANVAS_DIR, f"{cleaned}.json")

def canvas_record(data: dict) -> dict:
    return {
        "id": data.get("id"),
        "title": data.get("title", "未命名画布"),
        "icon": data.get("icon", "layers"),
        "kind": normalize_canvas_kind(data.get("kind")),
        "owner": str(data.get("owner") or "")[:40],
        "color": normalize_canvas_color(data.get("color")),
        "pinned": bool(data.get("pinned") or False),
        "project": str(data.get("project") or "").strip() or DEFAULT_PROJECT_ID,
        "board_x": data.get("board_x"),
        "board_y": data.get("board_y"),
        "created_at": data.get("created_at", 0),
        "updated_at": data.get("updated_at", 0),
        "deleted_at": data.get("deleted_at", 0),
        "node_count": len(data.get("nodes", [])),
    }

# ===== 高性能纳秒级缓存 =====
canvas_cache = CanvasCache(CANVAS_DIR, record_builder=canvas_record, trash_retention_ms=CANVAS_TRASH_RETENTION_MS)

def cleanup_expired_canvas_trash(force=False) -> int:
    return canvas_cache.cleanup_expired_trash_if_needed(force=force)

def iter_canvas_records(include_deleted=False) -> list:
    return canvas_cache.iter_records(include_deleted=include_deleted)

def list_canvases() -> list:
    records = iter_canvas_records(include_deleted=False)
    return sorted(
        records,
        key=lambda item: (
            0 if item.get("pinned") else 1,
            -int(item.get("updated_at") or item.get("created_at") or 0),
        ),
    )

def list_deleted_canvases() -> list:
    records = iter_canvas_records(include_deleted=True)
    return sorted(records, key=lambda item: item["deleted_at"], reverse=True)

def save_canvas(canvas: dict) -> None:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", canvas.get("id") or "")
    filename = f"{cleaned}.json"
    target = canvas_path(canvas["id"])
    tmp_path = f"{target}.tmp"
    with CANVAS_LOCK:
        # updated_at is also the optimistic concurrency version. Wall-clock
        # collisions or clock rollback must not let a stale snapshot overwrite it.
        canvas["updated_at"] = max(now_ms(), int(canvas.get("updated_at") or 0) + 1)
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(canvas, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target)
        if canvas_cache:
            canvas_cache.update(filename, canvas)

def load_canvas(canvas_id: str) -> dict:
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    with CANVAS_LOCK:
        with open(path, 'r', encoding='utf-8') as f:
            canvas = json.load(f)
    if canvas.get("deleted_at"):
        raise HTTPException(status_code=404, detail="画布已在回收站")
    return canvas

def load_canvas_any(canvas_id: str) -> dict:
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    with CANVAS_LOCK:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

def new_canvas(title="未命名画布", icon="layers", kind="classic", project=None, board_x=None, board_y=None) -> dict:
    timestamp = now_ms()
    canvas_kind = normalize_canvas_kind(kind)
    canvas = {
        "id": uuid.uuid4().hex,
        "title": (title or ("智能画布" if canvas_kind == "smart" else "未命名画布"))[:80],
        "icon": (icon or ("sparkles" if canvas_kind == "smart" else "layers"))[:32],
        "kind": canvas_kind,
        "owner": "",
        "color": "",
        "pinned": False,
        "project": str(project or "").strip() or DEFAULT_PROJECT_ID,
        "created_at": timestamp,
        "updated_at": timestamp,
        "nodes": [],
        "connections": [],
        "viewport": {"x": 0, "y": 0, "scale": 1},
    }
    if board_x is not None:
        canvas["board_x"] = float(board_x)
    if board_y is not None:
        canvas["board_y"] = float(board_y)
    save_canvas(canvas)
    return canvas

# ===== 项目管理核心 =====
def load_projects() -> list:
    if not os.path.exists(PROJECTS_PATH):
        return []
    try:
        with CANVAS_LOCK:
            with open(PROJECTS_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
        projects = data.get("projects") if isinstance(data, dict) else data
        if isinstance(projects, list):
            return [p for p in projects if isinstance(p, dict) and p.get("id")]
    except Exception as exc:
        logger.warning(f"Failed to load projects from {PROJECTS_PATH}: {exc}")
    return []

def save_projects(projects: list) -> None:
    tmp_path = f"{PROJECTS_PATH}.tmp"
    with CANVAS_LOCK:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump({"projects": projects}, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, PROJECTS_PATH)

def project_record(p: dict) -> dict:
    return {
        "id": p.get("id"),
        "name": (p.get("name") or "未命名项目")[:60],
        "order": int(p.get("order") or 0),
        "created_at": p.get("created_at", 0),
        "updated_at": p.get("updated_at", 0),
    }

def ensure_default_project() -> list:
    """保证存在一个“默认项目”，并把没有归属项目的画布迁移进去（一次性、幂等）。"""
    projects = load_projects()
    changed = False
    if not any(p.get("id") == DEFAULT_PROJECT_ID for p in projects):
        ts = now_ms()
        projects.insert(0, {"id": DEFAULT_PROJECT_ID, "name": "默认项目", "order": 0, "created_at": ts, "updated_at": ts})
        changed = True
    if changed:
        save_projects(projects)
    return projects

def new_project(name="新项目") -> dict:
    projects = ensure_default_project()
    ts = now_ms()
    clean = (str(name or "").strip() or "新项目")[:60]
    order = max([int(p.get("order") or 0) for p in projects], default=0) + 1
    proj = {"id": uuid.uuid4().hex, "name": clean, "order": order, "created_at": ts, "updated_at": ts}
    projects.append(proj)
    save_projects(projects)
    return proj

def list_projects() -> list:
    projects = ensure_default_project()
    counts = {}
    for rec in iter_canvas_records(include_deleted=False):
        pid = rec.get("project") or DEFAULT_PROJECT_ID
        counts[pid] = counts.get(pid, 0) + 1
    out = []
    for p in sorted(projects, key=lambda x: (int(x.get("order") or 0), x.get("created_at") or 0)):
        rec = project_record(p)
        rec["canvas_count"] = counts.get(rec["id"], 0)
        out.append(rec)
    return out


# ==========================================
# API 路由定义 (15 个核心端点)
# ==========================================

# 1. 项目 CRUD
@router.get("/api/projects")
def api_list_projects():
    return {"projects": list_projects()}

@router.post("/api/projects")
def api_create_project(payload: ProjectCreateRequest):
    with CANVAS_LOCK:  # 读取列表→追加→整体替换必须是同一事务
        return {"project": project_record(new_project(payload.name))}

@router.post("/api/projects/{project_id}")
def api_update_project(project_id: str, payload: ProjectUpdateRequest):
    with CANVAS_LOCK:
        projects = ensure_default_project()
        target = next((p for p in projects if p.get("id") == project_id), None)
        if not target:
            raise HTTPException(status_code=404, detail="项目不存在")
        if payload.name is not None:
            target["name"] = (str(payload.name).strip() or target.get("name") or "未命名项目")[:60]
        if payload.order is not None:
            target["order"] = int(payload.order)
        target["updated_at"] = now_ms()
        save_projects(projects)
        return {"project": project_record(target)}

@router.delete("/api/projects/{project_id}")
def api_delete_project(project_id: str):
    """删除项目；默认项目不可删除；项目删除后其下画布自动归入默认项目。"""
    if project_id == DEFAULT_PROJECT_ID:
        raise HTTPException(status_code=400, detail="默认项目不可删除")
    # 项目列表修改与画布迁移处于同一把锁内
    moved = 0
    with CANVAS_LOCK:
        projects = ensure_default_project()
        if not any(p.get("id") == project_id for p in projects):
            raise HTTPException(status_code=404, detail="项目不存在")
        projects = [p for p in projects if p.get("id") != project_id]
        save_projects(projects)
        # 把该项目下的画布迁移到默认项目
        for filename in os.listdir(CANVAS_DIR):
            if not filename.endswith(".json"):
                continue
            path = os.path.join(CANVAS_DIR, filename)
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            except Exception:
                continue
            if str(data.get("project") or "") == project_id:
                data["project"] = DEFAULT_PROJECT_ID
                tmp_file = f"{path}.tmp"
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, path)
                if canvas_cache:
                    canvas_cache.update(filename, data)
                moved += 1
    return {"ok": True, "moved": moved}

# 2. 画布 CRUD 与回收站
@router.get("/api/canvases")
def api_list_canvases():
    return {"canvases": list_canvases()}

@router.get("/api/canvases/trash")
def api_trashed_canvases():
    return {"canvases": list_deleted_canvases(), "retention_days": 30}

@router.post("/api/canvases")
def api_create_canvas(payload: CanvasCreateRequest):
    return {"canvas": new_canvas(payload.title, payload.icon, payload.kind, payload.project, payload.board_x, payload.board_y)}

@router.get("/api/canvases/{canvas_id}/meta")
def api_get_canvas_meta(canvas_id: str):
    path = canvas_path(canvas_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="画布不存在")
    record = canvas_cache.get_record(os.path.basename(path))
    if not record:
        raise HTTPException(status_code=404, detail="画布已在回收站")
    return {
        "id": record.get("id"),
        "updated_at": record.get("updated_at", 0),
        "title": record.get("title", "未命名画布"),
        "icon": record.get("icon", "layers"),
        "kind": normalize_canvas_kind(record.get("kind")),
    }

@router.post("/api/canvases/{canvas_id}/meta")
def api_update_canvas_meta(canvas_id: str, payload: CanvasMetaUpdate):
    """更新画布元数据（标题/图标/标签/颜色/置顶等），避免 save_canvas 刷新 updated_at 导致列表跳动。"""
    with CANVAS_LOCK:
        canvas = load_canvas(canvas_id)
        current_updated_at = int(canvas.get("updated_at") or 0)
        if payload.base_updated_at is not None and current_updated_at and int(payload.base_updated_at) < current_updated_at:
            raise HTTPException(status_code=409, detail={
                "message": "画布元数据已被其他页面更新，已拒绝旧版本覆盖。",
                "canvas": canvas_record(canvas),
                "updated_at": current_updated_at,
            })
        if payload.title is not None:
            canvas["title"] = (payload.title or canvas.get("title") or "未命名画布")[:80]
        if payload.icon is not None:
            canvas["icon"] = (payload.icon or "layers")[:32]
        if payload.owner is not None:
            canvas["owner"] = str(payload.owner).strip()[:40]
        if payload.color is not None:
            canvas["color"] = normalize_canvas_color(payload.color)
        if payload.pinned is not None:
            canvas["pinned"] = bool(payload.pinned)
        if payload.project is not None:
            canvas["project"] = str(payload.project).strip() or DEFAULT_PROJECT_ID
        if payload.board_x is not None:
            canvas["board_x"] = float(payload.board_x)
        if payload.board_y is not None:
            canvas["board_y"] = float(payload.board_y)
        target = canvas_path(canvas["id"])
        tmp_path = f"{target}.tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(canvas, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, target)
        if canvas_cache:
            cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", canvas.get("id") or "")
            canvas_cache.update(f"{cleaned}.json", canvas)
    return {"canvas": canvas_record(canvas)}

@router.get("/api/canvases/{canvas_id}")
def api_get_canvas(canvas_id: str):
    return {"canvas": load_canvas(canvas_id)}

@router.post("/api/canvases/{canvas_id}/touch")
def api_touch_canvas(canvas_id: str):
    with CANVAS_LOCK:  # 读取与写回之间不得被保存/删除插入
        canvas = load_canvas(canvas_id)
        save_canvas(canvas)
        return {"canvas": canvas_record(canvas), "updated_at": canvas.get("updated_at", 0)}

MAX_CANVAS_NODES = 2000
MAX_CANVAS_CONNECTIONS = 5000
MAX_INLINE_BASE64_BYTES = 15 * 1024 * 1024  # 15MB
MAX_CANVAS_LOGS = 200

def _commit_canvas_save(canvas_id: str, payload: CanvasSaveRequest):
    with CANVAS_LOCK:
        canvas = load_canvas(canvas_id)
        current_updated_at = int(canvas.get("updated_at") or 0)
        if payload.base_updated_at and current_updated_at and int(payload.base_updated_at) < current_updated_at:
            raise HTTPException(status_code=409, detail={
                "message": "画布已被其他页面更新，已拒绝旧版本覆盖。",
                "canvas": canvas,
                "updated_at": current_updated_at,
            })

        canvas["title"] = (payload.title or canvas.get("title") or "未命名画布")[:80]
        canvas["icon"] = (payload.icon or canvas.get("icon") or "layers")[:32]
        canvas["kind"] = normalize_canvas_kind(canvas.get("kind"))
        canvas["nodes"] = payload.nodes
        canvas["connections"] = payload.connections
        if canvas["kind"] == "smart":
            canvas["viewport"] = payload.viewport
        else:
            canvas["viewport"] = canvas.get("viewport") or {"x": 0, "y": 0, "scale": 1}
        canvas["logs"] = payload.logs[-MAX_CANVAS_LOGS:]
        canvas["settings"] = payload.settings or {}
        save_canvas(canvas)
        new_updated_at = int(canvas.get("updated_at") or now_ms())
        return canvas, new_updated_at


@router.put("/api/canvases/{canvas_id}")
async def api_update_canvas(canvas_id: str, payload: CanvasSaveRequest):
    # --- Payload-only guards (no shared state): fail fast before taking the lock ---
    if len(payload.nodes) > MAX_CANVAS_NODES:
        raise HTTPException(
            status_code=422,
            detail=f"画布节点数超出安全上限 ({len(payload.nodes)} > {MAX_CANVAS_NODES})，请拆分或精简画布。"
        )
    if len(payload.connections) > MAX_CANVAS_CONNECTIONS:
        raise HTTPException(
            status_code=422,
            detail=f"画布连线数超出安全上限 ({len(payload.connections)} > {MAX_CANVAS_CONNECTIONS})。"
        )

    for node in payload.nodes:
        if not isinstance(node, dict):
            continue
        for field in ("url", "image", "src", "data", "preview"):
            val = node.get(field)
            if isinstance(val, str) and val.startswith("data:") and len(val) > MAX_INLINE_BASE64_BYTES:
                node_name = node.get("title") or node.get("name") or node.get("id") or "未命名"
                mb_size = round(len(val) / (1024 * 1024), 1)
                raise HTTPException(
                    status_code=422,
                    detail=f"节点 [{node_name}] 内嵌的 Base64 数据过大 ({mb_size}MB，安全上限 15MB)，请先将其转存为本地图片资产。"
                )

    # --- Atomic optimistic-concurrency transaction (P0 fix) ---------------------
    # The version check and the write MUST run under one continuous hold of
    # CANVAS_LOCK (a reentrant RLock, so the load_canvas/save_canvas helpers that
    # also take it are safe). Run off the event loop via asyncio.to_thread so
    # JSON serialization and file I/O do not block async request handling.
    canvas, new_updated_at = await asyncio.to_thread(_commit_canvas_save, canvas_id, payload)

    await _broadcast_canvas_updated(canvas_id, new_updated_at, payload.client_id)
    return {"canvas": canvas}

@router.delete("/api/canvases/{canvas_id}")
def api_delete_canvas(canvas_id: str):
    with CANVAS_LOCK:
        canvas = load_canvas_any(canvas_id)
        if not canvas.get("deleted_at"):
            canvas["deleted_at"] = now_ms()
            save_canvas(canvas)
        return {"ok": True}

@router.post("/api/canvases/{canvas_id}/restore")
def api_restore_canvas(canvas_id: str):
    with CANVAS_LOCK:
        canvas = load_canvas_any(canvas_id)
        if canvas.get("deleted_at"):
            canvas.pop("deleted_at", None)
            save_canvas(canvas)
        return {"canvas": canvas}

@router.delete("/api/canvases/{canvas_id}/purge")
def api_purge_canvas(canvas_id: str):
    path = canvas_path(canvas_id)
    with CANVAS_LOCK:  # 与保存串行：删除与缓存失效对并发保存呈现单一先后顺序
        if os.path.exists(path):
            os.remove(path)
        if canvas_cache:
            filename = f"{re.sub(r'[^a-zA-Z0-9_-]', '', canvas_id)}.json"
            canvas_cache.invalidate(filename)
    return {"ok": True}


# ===== Rhino 3D Viewport Direct-Push Bridge =====

class RhinoCaptureRequest(BaseModel):
    image_base64: str
    view_name: Optional[str] = "Perspective"
    camera: Optional[Dict[str, Any]] = None
    timestamp: Optional[int] = None
    canvas_id: Optional[str] = None


def _save_rhino_capture_file(filepath: str, raw_b64: str) -> int:
    import base64
    if "," in raw_b64 and raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[1]
    try:
        img_bytes = base64.b64decode(raw_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid base64 data: {exc}")

    if len(img_bytes) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image exceeds 25MB maximum limit")

    try:
        with open(filepath, "wb") as f:
            f.write(img_bytes)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save rhino capture: {exc}")

    return len(img_bytes)


@router.post("/api/canvas/rhino-capture")
async def api_post_rhino_capture(payload: RhinoCaptureRequest):
    global _latest_rhino_capture
    raw_b64 = payload.image_base64
    if not raw_b64:
        raise HTTPException(status_code=400, detail="Missing image_base64 data")

    timestamp = payload.timestamp or int(time.time() * 1000)
    capture_id = f"rhino_{timestamp}_{uuid.uuid4().hex[:8]}"
    filename = f"{capture_id}.png"
    filepath = os.path.join(RHINO_UPLOAD_DIR, filename)

    img_size = await asyncio.to_thread(_save_rhino_capture_file, filepath, raw_b64)

    record = {
        "id": capture_id,
        "url": f"/assets/uploads/rhino/{filename}",
        "view_name": (payload.view_name or "Perspective")[:50],
        "camera": payload.camera or {},
        "timestamp": timestamp,
        "canvas_id": payload.canvas_id or "",
        "size_bytes": img_size,
    }

    with RHINO_LOCK:
        _latest_rhino_capture = record

    # Broadcast to all connected clients via websocket
    if _ws_manager and hasattr(_ws_manager, "broadcast_rhino_capture"):
        try:
            await _ws_manager.broadcast_rhino_capture(record)
        except Exception as exc:
            logger.debug(f"Failed to broadcast rhino capture: {exc}")

    return {"ok": True, "capture": record}


@router.get("/api/canvas/rhino-capture/latest")
def api_get_latest_rhino_capture(since: int = 0):
    global _latest_rhino_capture
    with RHINO_LOCK:
        if not _latest_rhino_capture:
            return {"capture": None}
        if _latest_rhino_capture.get("timestamp", 0) > since:
            return {"capture": _latest_rhino_capture}
        return {"capture": None}


# ===== 移动端直推桌面画布桥接 (Mobile-to-Desktop Direct Push) =====

class MobilePushRequest(BaseModel):
    url: str
    name: Optional[str] = "移动端图片"
    canvas_id: Optional[str] = None
    prompt: Optional[str] = None


def _push_node_to_canvas_sync(target_canvas_id: Optional[str], node_id: str, payload: MobilePushRequest) -> tuple:
    with CANVAS_LOCK:
        target_canvas_id = (target_canvas_id or "").strip()
        if not target_canvas_id or target_canvas_id in ("all", "recent-online", "unknown"):
            canvases = list_canvases()
            if canvases:
                target_canvas_id = canvases[0]["id"]
                target_canvas = load_canvas(target_canvas_id)
            else:
                target_canvas = new_canvas("移动端创作")
                target_canvas_id = target_canvas["id"]
        else:
            target_canvas = load_canvas(target_canvas_id)

        target_canvas_title = target_canvas.get("title", "画布")
        existing_nodes = target_canvas.get("nodes", [])
        max_x = max([int(n.get("x", 0)) + int(n.get("width", 300)) for n in existing_nodes if isinstance(n, dict)], default=100)

        new_node = {
            "id": node_id,
            "type": "image",
            "x": max_x + 50,
            "y": 100,
            "url": payload.url.strip(),
            "title": (payload.name or "移动端图片")[:80],
            "prompt": (payload.prompt or "")[:1000],
        }
        existing_nodes.append(new_node)
        target_canvas["nodes"] = existing_nodes
        save_canvas(target_canvas)
        return target_canvas_id, target_canvas_title


@router.post("/api/canvas/mobile-push")
async def api_mobile_push_image(payload: MobilePushRequest):
    if not payload.url or not payload.url.strip():
        raise HTTPException(status_code=400, detail="缺少图片 URL")

    target_canvas_id = (payload.canvas_id or "").strip()
    node_id = f"node_{now_ms()}_{uuid.uuid4().hex[:6]}"

    try:
        target_canvas_id, target_canvas_title = await asyncio.to_thread(
            _push_node_to_canvas_sync, target_canvas_id, node_id, payload
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning(f"Failed to persist pushed node into canvas {target_canvas_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"保存推送到画布失败: {exc}")

    push_record = {
        "node_id": node_id,
        "url": payload.url.strip(),
        "name": (payload.name or "移动端图片")[:80],
        "canvas_id": target_canvas_id,
        "canvas_title": target_canvas_title,
        "prompt": payload.prompt or "",
        "timestamp": now_ms(),
    }
    if _ws_manager and hasattr(_ws_manager, "broadcast_mobile_push"):
        try:
            await _ws_manager.broadcast_mobile_push(push_record)
        except Exception as exc:
            logger.debug(f"Failed to broadcast mobile push: {exc}")

    return {"ok": True, "node_id": node_id, "canvas_id": target_canvas_id, "canvas_title": target_canvas_title}

