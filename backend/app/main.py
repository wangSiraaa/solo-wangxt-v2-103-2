"""FastAPI 入口：版本化拓扑、草案、锁阀、隔离计算、历史、导入导出。"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from . import versions
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .schemas import (
    DraftContentIn,
    DraftCreateIn,
    DraftPublishIn,
    IsolationIn,
    IsolationOut,
    PublishOut,
    SwitchVersionIn,
    TopologyOut,
    ValveLockIn,
)
from .seed import seed_database


@asynccontextmanager
async def lifespan(_app: FastAPI):  # pragma: no cover
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
        versions.ensure_version_rows(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="管网隔离方案演示 API（培训用）",
    description="基于版本化拓扑与阀门模型的检修隔离候选集合计算，不连接真实控制系统。",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _current(db: Session, explicit: str | None) -> str:
    return explicit or versions.get_current_version_id(db)


def _not_found(exc: versions.NotFound) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


# ---------------- 基础 ----------------

@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ---------------- 版本 ----------------

@app.get("/api/versions")
def api_list_versions(db: Session = Depends(get_db)) -> dict[str, object]:
    return {
        "current_version": versions.get_current_version_id(db),
        "versions": versions.list_versions(db),
    }


@app.get("/api/versions/{version_id}")
def api_get_version(version_id: str, db: Session = Depends(get_db)) -> TopologyOut:
    try:
        return TopologyOut(**versions.get_version_payload(db, version_id))
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.post("/api/current-version")
def api_switch_version(body: SwitchVersionIn, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        versions.set_current_version_id(db, body.version_id)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc
    return {"current_version": body.version_id}


# ---------------- 拓扑 / 计算（版本绑定） ----------------

@app.get("/api/topology")
def get_topology(
    topology_version: str | None = Query(None),
    db: Session = Depends(get_db),
) -> TopologyOut:
    versions.ensure_version_rows(db)
    vid = _current(db, topology_version)
    try:
        return TopologyOut(**versions.get_version_payload(db, vid))
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(
    valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)
) -> dict[str, object]:
    vid = _current(db, body.topology_version)
    try:
        return versions.set_lock(db, vid, valve_id, body.locked)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.post("/api/reset")
def reset(
    topology_version: str | None = Query(None),
    db: Session = Depends(get_db),
) -> dict[str, str]:
    vid = _current(db, topology_version)
    versions.reset_locks(db, vid)
    return {"status": "reset", "topology_version": vid}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    versions.ensure_version_rows(db)
    vid = _current(db, body.topology_version)
    try:
        payload = versions.calculate(db, vid, body.target_id, body.locks)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return IsolationOut(**payload)


# ---------------- 计算历史 / 旧结果 ----------------

@app.get("/api/calculations")
def api_list_calculations(db: Session = Depends(get_db)) -> dict[str, object]:
    return {"calculations": versions.get_history_payload(db)}


@app.get("/api/calculations/{calc_id}")
def api_get_calculation(calc_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return versions.get_calculation(db, calc_id)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


# ---------------- 草案 ----------------

@app.get("/api/drafts")
def api_list_drafts(db: Session = Depends(get_db)) -> dict[str, object]:
    return {"drafts": versions.list_drafts(db)}


@app.post("/api/drafts")
def api_create_draft(body: DraftCreateIn, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return versions.create_draft(db, body.base_version_id, body.name or "")
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.get("/api/drafts/{draft_id}")
def api_get_draft(draft_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return versions.get_draft(db, draft_id)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.put("/api/drafts/{draft_id}")
def api_update_draft(
    draft_id: str, body: DraftContentIn, db: Session = Depends(get_db)
) -> dict[str, object]:
    try:
        return versions.update_draft(db, draft_id, body.content)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/drafts/{draft_id}")
def api_delete_draft(draft_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        versions.delete_draft(db, draft_id)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc
    return {"deleted": draft_id}


@app.post("/api/drafts/{draft_id}/validate")
def api_validate_draft(draft_id: str, db: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return versions.validate_draft(db, draft_id)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.post("/api/drafts/{draft_id}/publish", response_model=PublishOut)
def api_publish_draft(
    draft_id: str, body: DraftPublishIn, db: Session = Depends(get_db)
) -> PublishOut:
    try:
        out = versions.publish_draft(db, draft_id, body.name)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc
    except versions.RevisionConflict as exc:
        # 两个草案基于同一基线发布：后一份得到版本冲突（409），不覆盖前一份
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PublishOut(**out)


# ---------------- 导入 / 导出 ----------------

@app.get("/api/versions/{version_id}/export")
def api_export_version(
    version_id: str,
    include_calculations: bool = Query(True),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return versions.export_bundle(db, version_id, include_calculations)
    except versions.NotFound as exc:
        raise _not_found(exc) from exc


@app.post("/api/import")
def api_import_bundle(body: dict[str, object], db: Session = Depends(get_db)) -> dict[str, object]:
    # 兼容 {"bundle": {...}} 与直接贴入整包两种前端用法
    bundle = body.get("bundle", body) if isinstance(body, dict) else body
    try:
        return versions.import_bundle(db, bundle)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
