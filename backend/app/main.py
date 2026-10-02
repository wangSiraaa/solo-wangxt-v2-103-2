"""FastAPI 入口：版本化拓扑、草案发布、阀门锁定、隔离方案与历史。"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import isolation, versions
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .models import Valve, VersionLock
from .schemas import (
    IsolationIn,
    IsolationOut,
    TopologyOut,
    ValveLockIn,
    VersionLocksIn,
)
from .seed import reset_database, seed_database


@asynccontextmanager
async def lifespan(app: FastAPI):  # pragma: no cover
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        seed_database(db)
        versions.ensure_versioned_seed(db)
    finally:
        db.close()
    yield


app = FastAPI(
    title="管网隔离方案演示 API（培训用，含版本化拓扑草案）",
    description=(
        "基于固定/版本化拓扑与阀门模型的检修隔离候选集合计算，不连接真实控制系统。"
        "已发布拓扑版本不可变；隔离计算、锁定记录与无解见证均绑定版本。"
    ),
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


def _version_error(exc: versions.VersionError) -> HTTPException:
    detail: Any = {"code": exc.code, "message": str(exc)}
    if exc.extra:
        detail.update(exc.extra)
    return HTTPException(status_code=exc.status, detail=detail)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


# ============================================================ 版本

@app.get("/api/versions")
def api_list_versions(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    versions.ensure_versioned_seed(db)
    return versions.list_versions(db)


@app.get("/api/versions/{version_no}")
def api_get_version(version_no: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    return versions.get_version(db, version_no)


@app.get("/api/versions/{version_no}/topology", response_model=TopologyOut)
def api_version_topology(version_no: int, db: Session = Depends(get_db)) -> TopologyOut:
    return TopologyOut(**versions.topology_payload_for_version(db, version_no))


@app.get("/api/versions/{version_no}/locks")
def api_version_get_locks(version_no: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        locks = versions.get_locks(db, version_no)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc
    return {"version_no": version_no, "locks": locks}


@app.put("/api/versions/{version_no}/locks")
def api_version_put_locks(
    version_no: int, body: VersionLocksIn, db: Session = Depends(get_db)
) -> dict[str, Any]:
    snap = versions.load_snapshot(db, version_no)
    valid = {
        s["valve"]["id"] for s in snap["segments"] if s.get("valve")
    }
    try:
        locks = versions.replace_locks(db, version_no, body.locks, valid)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc
    return {"version_no": version_no, "locks": locks}


@app.post("/api/versions/{version_no}/reset")
def api_version_reset(version_no: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    versions.reset_locks(db, version_no)
    return {"version_no": version_no, "status": "reset"}


# ============================================================ 草案

@app.get("/api/drafts")
def api_list_drafts(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return versions.list_drafts(db)


@app.post("/api/versions/{version_no}/drafts")
def api_create_draft(
    version_no: int, body: dict[str, Any] | None = None, db: Session = Depends(get_db)
) -> dict[str, Any]:
    body = body or {}
    try:
        return versions.create_draft(
            db, version_no, body.get("name"), body.get("created_by", "trainer")
        )
    except versions.VersionError as exc:
        raise _version_error(exc) from exc


@app.get("/api/drafts/{draft_id}")
def api_get_draft(draft_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return versions.get_draft(db, draft_id)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc


@app.put("/api/drafts/{draft_id}")
def api_update_draft(draft_id: str, body: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return versions.update_draft(db, draft_id, body)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc


@app.delete("/api/drafts/{draft_id}")
def api_delete_draft(draft_id: str, db: Session = Depends(get_db)) -> dict[str, str]:
    try:
        versions.delete_draft(db, draft_id)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc
    return {"status": "deleted", "id": draft_id}


@app.get("/api/drafts/{draft_id}/validate")
def api_validate_draft(draft_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        result = versions.validate_draft(db, draft_id)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc
    return result


@app.post("/api/drafts/{draft_id}/publish")
def api_publish_draft(draft_id: str, body: dict[str, Any] | None = None, db: Session = Depends(get_db)) -> dict[str, Any]:
    body = body or {}
    expected = body.get("expected_base_version_no")
    try:
        meta = versions.publish_draft(
            db,
            draft_id,
            expected_base_version_no=expected,
            note=body.get("note", ""),
            created_by=body.get("created_by", "trainer"),
        )
    except versions.VersionError as exc:
        raise _version_error(exc) from exc
    return {"published": meta, "draft_id": draft_id}


# ============================================================ 历史 / 导入导出

@app.get("/api/history")
def api_history(version_no: int | None = None, db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return versions.list_records(db, version_no)


@app.get("/api/history/{record_id}")
def api_history_record(record_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return versions.get_record(db, record_id)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc


@app.get("/api/export")
def api_export(db: Session = Depends(get_db)) -> dict[str, Any]:
    return versions.export_bundle(db)


@app.post("/api/import")
def api_import(body: dict[str, Any], db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        return versions.import_bundle(db, body)
    except versions.VersionError as exc:
        raise _version_error(exc) from exc


# ============================================================ 兼容 v1 的旧接口

@app.get("/api/topology", response_model=TopologyOut)
def get_topology(db: Session = Depends(get_db)) -> TopologyOut:
    seed_database(db)
    versions.ensure_versioned_seed(db)
    payload = isolation.topology_payload(db)
    payload["topo_version"] = versions.get_version(db, 1)
    return TopologyOut(**payload)


@app.post("/api/valves/{valve_id}/lock")
def set_valve_lock(valve_id: str, body: ValveLockIn, db: Session = Depends(get_db)) -> dict[str, object]:
    versions.ensure_versioned_seed(db)
    valve = db.scalar(select(Valve).where(Valve.id == valve_id))
    if valve is None:
        raise HTTPException(status_code=404, detail=f"阀门不存在: {valve_id}")
    valve.locked = body.locked
    db.commit()
    # 同步到 v1 的版本锁定表，使按版本查询与旧接口保持一致
    existing = db.scalar(
        select(VersionLock).where(
            VersionLock.version_no == 1,
            VersionLock.valve_id == valve_id,
        )
    )
    if body.locked and existing is None:
        db.add(VersionLock(version_no=1, valve_id=valve_id, locked=True))
    elif not body.locked and existing is not None:
        db.delete(existing)
    db.commit()
    return {"id": valve.id, "locked": valve.locked}


@app.post("/api/reset")
def reset(db: Session = Depends(get_db)) -> dict[str, str]:
    reset_database(db)
    versions.reset_locks(db, 1)
    return {"status": "reset"}


@app.post("/api/isolation", response_model=IsolationOut)
def calc_isolation(body: IsolationIn, db: Session = Depends(get_db)) -> IsolationOut:
    versions.ensure_versioned_seed(db)
    version_no = body.topo_version or 1
    version_obj = versions.get_version_obj(db, version_no)

    if version_no == 1:
        return _calc_v1_legacy(body, db, version_obj)
    return _calc_versioned(body, db, version_obj)


def _calc_v1_legacy(
    body: IsolationIn, db: Session, version_obj
) -> IsolationOut:
    """v1 固定样例路径：保留旧表锁定行为（旧测试与旧脚本不受影响）。"""
    seed_database(db)
    if body.locks:
        known = {v.id for v in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in body.locks if vid not in known]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        for vid, locked in body.locks.items():
            valve = db.get(Valve, vid)
            if valve is not None:
                valve.locked = locked
        db.commit()
        # 镜像到版本锁定表
        for vid, locked in body.locks.items():
            row = db.scalar(
                select(VersionLock).where(
                    VersionLock.version_no == 1,
                    VersionLock.valve_id == vid,
                )
            )
            if locked and row is None:
                db.add(VersionLock(version_no=1, valve_id=vid, locked=True))
            elif not locked and row is not None:
                db.delete(row)
        db.commit()

    try:
        payload = isolation.compute_isolation(db, body.target_id)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    locks = versions.get_locks(db, 1)
    payload["topo_version"] = versions.get_version(db, 1)
    payload["locked_valves"] = sorted(locks)
    record_id = versions.add_record(
        db, version_obj, body.target_id, locks, payload
    )
    payload["record_id"] = record_id
    return IsolationOut(**payload)


def _calc_versioned(
    body: IsolationIn, db: Session, version_obj
) -> IsolationOut:
    snapshot = json.loads(version_obj.snapshot)
    valid_valve_ids = {
        s["valve"]["id"] for s in snapshot["segments"] if s.get("valve")
    }

    if body.locks is None:
        locks = versions.get_locks(db, version_obj.version_no)
    else:
        unknown = [vid for vid in body.locks if vid not in valid_valve_ids]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知阀门: {', '.join(unknown)}")
        try:
            versions.replace_locks(db, version_obj.version_no, body.locks, valid_valve_ids)
        except versions.VersionError as exc:
            raise _version_error(exc) from exc
        locks = {vid: True for vid, on in body.locks.items() if on}

    try:
        payload = isolation.compute_snapshot(snapshot, body.target_id, locks)
    except isolation.TopologyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    meta = versions.get_version(db, version_obj.version_no)
    payload["topo_version"] = meta
    payload["locked_valves"] = sorted(locks)
    record_id = versions.add_record(db, version_obj, body.target_id, locks, payload)
    payload["record_id"] = record_id
    return IsolationOut(**payload)


# 前端静态资源（ng build 产物）挂在根路径。挂载放在所有 /api 路由之后，
# 因此 API 请求不会被静态应用截获；html=True 同时提供 SPA 回退。
if settings.frontend_dist.exists():
    app.mount(
        "/",
        StaticFiles(directory=settings.frontend_dist, html=True),
        name="frontend",
    )
