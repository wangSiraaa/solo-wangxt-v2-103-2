"""版本化拓扑服务层：版本、草案、锁阀记录、计算记录、导入导出。

关键不变量：
1. 已发布版本（TopologyVersion）与计算记录（CalculationRecord）内容不可变；
   v1 样例行同样不可改结构（只能改 Valve 表的锁定态，与旧行为一致）。
2. 发布必须通过 topo.validate_content；任何一步失败整体回滚，不留半张图。
3. 线性修订链：同一基线版本只能发布出一个后继，第二个发布得到
   RevisionConflict（HTTP 409），不会覆盖前一个版本。
4. v2+ 的锁阀状态写 LockRecord（按版本隔离），不触碰 v1 表。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import isolation, topo
from .models import (
    AppState,
    CalculationRecord,
    LockRecord,
    Node,
    SEED_VERSION_ID,
    SEED_VERSION_NAME,
    TopologyDraft,
    TopologyVersion,
    Valve,
)

CURRENT_VERSION_KEY = "current_version"


class RevisionConflict(RuntimeError):
    """两个草案基于同一基线发布时，后一份触发修订冲突。"""


class NotFound(RuntimeError):
    pass


# ------------------------------------------------------------ 初始化 / v1

def ensure_version_rows(db: Session) -> None:
    """保证 v1 固定样例版本行存在（幂等）。

    v1 的结构仍以 Node/Segment/Valve 原表为权威来源；版本行保存一份
    发布时刻的 JSON 快照，作为“不可变版本”的归档与复制来源。
    """
    if db.get(TopologyVersion, SEED_VERSION_ID) is not None:
        return
    existing = db.scalar(select(Node).limit(1))
    if existing is None:
        return  # 原表尚未播种（seed_database 负责播种），本次不建版本行
    # 入库即规范化，保证 v1 快照与导出/导入往返（normalize_for_publish）字节等价
    snapshot = topo.normalize_for_publish(topo.content_from_v1_tables(db))
    row = TopologyVersion(
        id=SEED_VERSION_ID,
        name=SEED_VERSION_NAME,
        base_version_id=None,
        content=snapshot,
        note="随系统内置的固定培训样例，结构永久不可变。",
        immutable=True,
    )
    db.add(row)
    db.commit()


def _ensure(db: Session) -> None:
    """各服务入口调用：保证版本行与当前版本指针就绪（幂等）。"""
    ensure_version_rows(db)
    if db.get(AppState, CURRENT_VERSION_KEY) is None:
        db.add(AppState(key=CURRENT_VERSION_KEY, value=SEED_VERSION_ID))
        db.commit()


def get_current_version_id(db: Session) -> str:
    _ensure(db)
    row = db.get(AppState, CURRENT_VERSION_KEY)
    return row.value if row else SEED_VERSION_ID


def set_current_version_id(db: Session, version_id: str) -> None:
    if db.get(TopologyVersion, version_id) is None:
        raise NotFound(f"拓扑版本不存在: {version_id}")
    row = db.get(AppState, CURRENT_VERSION_KEY)
    if row is None:
        db.add(AppState(key=CURRENT_VERSION_KEY, value=version_id))
    else:
        row.value = version_id
    db.commit()


# ------------------------------------------------------------ 读取

def _version_summary(v: TopologyVersion) -> dict[str, Any]:
    return {
        "id": v.id,
        "name": v.name,
        "note": v.note,
        "base_version_id": v.base_version_id,
        "immutable": v.immutable,
        "created_at": v.created_at.isoformat() if v.created_at else None,
        "node_count": len(v.content.get("nodes", [])),
        "segment_count": len(v.content.get("segments", [])),
        "valve_count": sum(1 for s in v.content.get("segments", []) if s.get("valve")),
    }


def list_versions(db: Session) -> list[dict[str, Any]]:
    _ensure(db)
    rows = list(db.scalars(select(TopologyVersion).order_by(TopologyVersion.id)).all())
    return [_version_summary(v) for v in rows]


def _require_version(db: Session, version_id: str) -> TopologyVersion:
    v = db.get(TopologyVersion, version_id)
    if v is None:
        raise NotFound(f"拓扑版本不存在: {version_id}")
    return v


def _locks_for_version(db: Session, version_id: str) -> dict[str, bool]:
    if version_id == SEED_VERSION_ID:
        return {v.id: v.locked for v in db.scalars(select(Valve)).all()}
    return {
        r.valve_id: r.locked
        for r in db.scalars(select(LockRecord).where(LockRecord.version_id == version_id)).all()
    }


def get_version_payload(db: Session, version_id: str) -> dict[str, Any]:
    """返回某版本的完整拓扑（含运行时锁阀状态），供图形与计算使用。"""
    v = _require_version(db, version_id)
    locks = _locks_for_version(db, version_id)
    content = v.content
    if version_id == SEED_VERSION_ID:
        # v1 结构不可变，阀门开闭/可操作性以原表实时状态为准（与旧样例一致）
        live = {x.id: x for x in db.scalars(select(Valve)).all()}
        content = {
            "nodes": v.content["nodes"],
            "segments": [
                {
                    **s,
                    "valve": (
                        None
                        if s.get("valve") is None
                        else {
                            **s["valve"],
                            "is_open": live[s["valve"]["id"]].is_open
                            if s["valve"]["id"] in live
                            else s["valve"]["is_open"],
                            "operable": live[s["valve"]["id"]].operable
                            if s["valve"]["id"] in live
                            else s["valve"]["operable"],
                        }
                    ),
                }
                for s in v.content["segments"]
            ],
        }
    nodes, edges = topo.nodes_edges_from_content(content, locks)
    payload = isolation.topology_payload_from(nodes, edges, version_id)
    payload["version_name"] = v.name
    payload["base_version_id"] = v.base_version_id
    payload["immutable"] = v.immutable
    return payload


def get_history_payload(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    """最近的计算记录，全部带拓扑版本标识与版本名。"""
    rows = list(
        db.scalars(
            select(CalculationRecord).order_by(CalculationRecord.created_at.desc()).limit(limit)
        ).all()
    )
    version_names = {vid: _name(db, vid) for vid in {r.topology_version_id for r in rows}}
    out = []
    for r in rows:
        out.append(
            {
                "id": r.id,
                "topology_version": r.topology_version_id,
                "version_id": r.topology_version_id,
                "version_name": version_names.get(r.topology_version_id, r.topology_version_id),
                "target_id": r.target_id,
                "feasible": r.feasible,
                "best_solution": r.result.get("best_solution", []),
                "locks_snapshot": r.locks_snapshot,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return out


def _name(db: Session, version_id: str) -> str | None:
    v = db.get(TopologyVersion, version_id)
    return v.name if v else None


def get_calculation(db: Session, calc_id: str) -> dict[str, Any]:
    r = db.get(CalculationRecord, calc_id)
    if r is None:
        raise NotFound(f"计算记录不存在: {calc_id}")
    result = dict(r.result)
    result["topology_version"] = r.topology_version_id
    result["version_id"] = r.topology_version_id
    result["version_name"] = _name(db, r.topology_version_id)
    result["calculation_id"] = r.id
    result["locks_snapshot"] = r.locks_snapshot
    return result


# ------------------------------------------------------------ 草案

def _draft_payload(d: TopologyDraft) -> dict[str, Any]:
    return {
        "id": d.id,
        "name": d.name,
        "base_version_id": d.base_version_id,
        "content": d.content,
        "validation_errors": topo.validate_content(d.content),
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def list_drafts(db: Session) -> list[dict[str, Any]]:
    rows = list(db.scalars(select(TopologyDraft).order_by(TopologyDraft.updated_at.desc())).all())
    return [_draft_payload(d) for d in rows]


def get_draft(db: Session, draft_id: str) -> dict[str, Any]:
    d = db.get(TopologyDraft, draft_id)
    if d is None:
        raise NotFound(f"草案不存在: {draft_id}")
    return _draft_payload(d)


def create_draft(db: Session, base_version_id: str, name: str) -> dict[str, Any]:
    """从已发布版本复制出一份可编辑草案（含当时结构，不含锁阀状态）。"""
    _ensure(db)
    base = _require_version(db, base_version_id)
    draft_id = "d" + uuid.uuid4().hex[:10]
    d = TopologyDraft(
        id=draft_id,
        name=name.strip() or f"{base.name} 草案",
        base_version_id=base.id,
        content=topo.clone_content(base.content),
    )
    db.add(d)
    db.commit()
    db.refresh(d)
    return _draft_payload(d)


def update_draft(db: Session, draft_id: str, content: dict[str, Any]) -> dict[str, Any]:
    d = db.get(TopologyDraft, draft_id)
    if d is None:
        raise NotFound(f"草案不存在: {draft_id}")
    # 草案允许保存校验不通过的中间状态；此处只做最基本的形状保护。
    if not isinstance(content, dict) or not isinstance(content.get("nodes"), list) \
            or not isinstance(content.get("segments"), list):
        raise ValueError("草案内容必须包含 nodes 与 segments 数组")
    d.content = content
    db.commit()
    db.refresh(d)
    return _draft_payload(d)


def delete_draft(db: Session, draft_id: str) -> None:
    d = db.get(TopologyDraft, draft_id)
    if d is None:
        raise NotFound(f"草案不存在: {draft_id}")
    db.delete(d)
    db.commit()


def validate_draft(db: Session, draft_id: str) -> dict[str, Any]:
    d = db.get(TopologyDraft, draft_id)
    if d is None:
        raise NotFound(f"草案不存在: {draft_id}")
    errors = topo.validate_content(d.content)
    return {"valid": not errors, "errors": errors}


def _next_version_id(db: Session) -> str:
    ids = [v.id for v in db.scalars(select(TopologyVersion)).all()]
    max_n = 1
    for vid in ids:
        if vid.startswith("v") and vid[1:].isdigit():
            max_n = max(max_n, int(vid[1:]))
    return f"v{max_n + 1}"


def publish_draft(db: Session, draft_id: str, name: str | None = None) -> dict[str, Any]:
    """校验通过后原子发布草案为新版本。

    - 校验失败：整体回滚，不产生任何版本/半成品数据。
    - 基线已有后继：抛 RevisionConflict（映射 HTTP 409），草案保留供重新基线。
    """
    d = db.get(TopologyDraft, draft_id)
    if d is None:
        raise NotFound(f"草案不存在: {draft_id}")

    errors = topo.validate_content(d.content)
    if errors:
        # 显式回滚（此前没有写入，但保证语义清晰：失败不留半张图）
        db.rollback()
        return {"published": False, "errors": errors, "version_id": None}

    base = _require_version(db, d.base_version_id)
    successor = db.scalar(
        select(TopologyVersion).where(TopologyVersion.base_version_id == base.id).limit(1)
    )
    if successor is not None:
        db.rollback()
        raise RevisionConflict(
            f"基线版本 {base.id} 已被 {successor.id} 发布为新版本，"
            f"请基于最新版本重新创建草案（修订冲突）"
        )

    version_id = _next_version_id(db)
    row = TopologyVersion(
        id=version_id,
        name=(name or d.name).strip() or f"拓扑版本 {version_id}",
        base_version_id=base.id,
        content=topo.normalize_for_publish(d.content),
        note=f"由草案 {draft_id} 从 {base.id} 发布。",
        immutable=False,
    )
    db.add(row)
    try:
        db.flush()  # 让唯一约束冲突在事务内暴露
    except IntegrityError as exc:  # 并发情况下另一编辑者抢先发布
        db.rollback()
        raise RevisionConflict(
            f"基线版本 {base.id} 刚刚已被其他编辑者发布新版本（修订冲突）"
        ) from exc
    db.delete(d)
    db.commit()
    db.refresh(row)
    return {"published": True, "errors": [], "version_id": version_id,
            "version": _version_summary(row)}


# ------------------------------------------------------------ 锁阀（版本绑定）

def set_lock(db: Session, version_id: str, valve_id: str, locked: bool) -> dict[str, Any]:
    """在指定版本上持久化单只阀门锁定；旧版本（v1）写原表，互不影响。"""
    v = _require_version(db, version_id)
    if version_id == SEED_VERSION_ID:
        valve = db.get(Valve, valve_id)
        if valve is None:
            raise NotFound(f"阀门不存在: {valve_id}")
        valve.locked = locked
        db.commit()
        return {"id": valve.id, "locked": valve.locked, "topology_version": version_id}

    known = {
        s["valve"]["id"]
        for s in v.content.get("segments", [])
        if isinstance(s.get("valve"), dict)
    }
    if valve_id not in known:
        raise NotFound(f"版本 {version_id} 中不存在阀门: {valve_id}")
    record = db.scalar(
        select(LockRecord)
        .where(LockRecord.version_id == version_id, LockRecord.valve_id == valve_id)
        .limit(1)
    )
    if record is None:
        db.add(LockRecord(version_id=version_id, valve_id=valve_id, locked=locked))
    else:
        record.locked = locked
    db.commit()
    return {"id": valve_id, "locked": locked, "topology_version": version_id}


def reset_locks(db: Session, version_id: str) -> None:
    """重置某版本的锁阀状态；v1 同时把阀门恢复打开（保持旧样例语义）。"""
    _require_version(db, version_id)
    if version_id == SEED_VERSION_ID:
        db.query(Valve).update({Valve.locked: False, Valve.is_open: True, Valve.operable: True})
    else:
        db.query(LockRecord).filter(LockRecord.version_id == version_id).delete()
    db.commit()


# ------------------------------------------------------------ 计算（版本绑定 + 归档）

def calculate(
    db: Session,
    version_id: str,
    target_id: str,
    locks: dict[str, bool] | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """在指定版本上计算；把锁阀落库后，用版本快照+锁快照归档不可变记录。

    返回结果带 topology_version；历史/导入重放都通过该标识取回对应拓扑，
    新版本永远不会重解释旧结果。
    """
    _ensure(db)
    v = _require_version(db, version_id)

    if locks:
        known = {
            s["valve"]["id"]
            for s in v.content.get("segments", [])
            if isinstance(s.get("valve"), dict)
        }
        if version_id == SEED_VERSION_ID:
            known = {x.id for x in db.scalars(select(Valve)).all()}
        unknown = [vid for vid in locks if vid not in known]
        if unknown:
            raise ValueError(f"未知阀门: {', '.join(sorted(unknown))}")
        for vid, locked in locks.items():
            set_lock(db, version_id, vid, bool(locked))

    active_locks = _locks_for_version(db, version_id)
    content = v.content
    if version_id == SEED_VERSION_ID:
        # v1 的结构以版本快照为准（不可变），但阀门开闭/可操作性仍以原表为准，
        # 与固定样例的历史行为完全一致；据此构造运行时视图。
        live = {x.id: x for x in db.scalars(select(Valve)).all()}
        content = {
            "nodes": v.content["nodes"],
            "segments": [
                {
                    **s,
                    "valve": (
                        None
                        if s.get("valve") is None
                        else {
                            **s["valve"],
                            "is_open": live[s["valve"]["id"]].is_open
                            if s["valve"]["id"] in live
                            else s["valve"]["is_open"],
                            "operable": live[s["valve"]["id"]].operable
                            if s["valve"]["id"] in live
                            else s["valve"]["operable"],
                        }
                    ),
                }
                for s in v.content["segments"]
            ],
        }
    nodes, edges = topo.nodes_edges_from_content(content, active_locks)
    result = isolation.compute_isolation_payload(nodes, edges, target_id, version_id)
    result["version_name"] = v.name

    if persist:
        calc_id = "c" + uuid.uuid4().hex[:12]
        record = CalculationRecord(
            id=calc_id,
            topology_version_id=version_id,
            target_id=target_id,
            locks_snapshot=dict(active_locks),
            result=result,
            feasible=bool(result.get("feasible")),
        )
        db.add(record)
        db.commit()
        result["calculation_id"] = calc_id
    return result


# ------------------------------------------------------------ 导入 / 导出

def export_bundle(
    db: Session, version_id: str, include_calculations: bool = True
) -> dict[str, Any]:
    """导出某版本及其计算记录；关系（基线、计算->版本）原样保留。"""
    v = _require_version(db, version_id)
    bundle: dict[str, Any] = {
        "format": "isolation-topology-bundle/1",
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "versions": [
            {
                "id": v.id,
                "name": v.name,
                "note": v.note,
                "base_version_id": v.base_version_id,
                "content": v.content,
            }
        ],
        "calculations": [],
    }
    if include_calculations:
        rows = db.scalars(
            select(CalculationRecord)
            .where(CalculationRecord.topology_version_id == version_id)
            .order_by(CalculationRecord.created_at)
        ).all()
        bundle["calculations"] = [
            {
                "id": r.id,
                "topology_version_id": r.topology_version_id,
                "target_id": r.target_id,
                "locks_snapshot": r.locks_snapshot,
                "result": r.result,
                "feasible": r.feasible,
            }
            for r in rows
        ]
    return bundle


def import_bundle(db: Session, bundle: dict[str, Any]) -> dict[str, Any]:
    """导入版本包（原子事务）。

    - 版本 id 与已存在版本冲突时整体拒绝（不覆盖不可变版本）。
    - 基线引用：若包内提供则必须随包存在或库中已存在。
    - 计算记录只允许引用包内/库内存在的版本，否则整体回滚。
    返回新入库的版本与计算 id。
    """
    _ensure(db)
    if not isinstance(bundle, dict) or bundle.get("format") != "isolation-topology-bundle/1":
        raise ValueError("无法识别的导入格式（缺少 format=isolation-topology-bundle/1）")
    raw_versions = bundle.get("versions", [])
    raw_calcs = bundle.get("calculations", [])
    if not isinstance(raw_versions, list) or not raw_versions:
        raise ValueError("导入包中没有任何版本")

    try:
        imported_versions: list[str] = []
        for item in raw_versions:
            vid = item.get("id")
            if not isinstance(vid, str) or not vid:
                raise ValueError("版本缺少 id")
            existing = db.get(TopologyVersion, vid)
            normalized = topo.normalize_for_publish(item["content"])
            if existing is not None:
                # 不可变版本（如固定样例 v1）内容一致时可安全共享，跳过即可；
                # 任何内容差异或可变版本冲突都拒绝，绝不覆盖。
                if not existing.immutable or existing.content != normalized:
                    raise ValueError(f"版本 {vid} 已存在，不可覆盖不可变版本")
                continue
            errors = topo.validate_content(item.get("content"))
            if errors:
                raise ValueError(
                    "版本 " + vid + " 校验未通过：" + "; ".join(e["message"] for e in errors[:3])
                )

        available = {v.id for v in db.scalars(select(TopologyVersion)).all()}
        for item in raw_versions:
            vid = item["id"]
            if vid in available and db.get(TopologyVersion, vid) is not None:
                continue  # 已存在且一致的不可变版本
            base = item.get("base_version_id")
            if base is not None and base not in available and not any(
                x.get("id") == base for x in raw_versions
            ):
                raise ValueError(f"版本 {vid} 引用的基线 {base} 在包与库中均不存在")
            if base is not None and db.scalar(
                select(TopologyVersion).where(TopologyVersion.base_version_id == base).limit(1)
            ) is not None:
                raise ValueError(f"基线 {base} 已有后继版本，导入会造成修订分叉")
            row = TopologyVersion(
                id=vid,
                name=item.get("name") or vid,
                base_version_id=base,
                content=topo.normalize_for_publish(item["content"]),
                note=item.get("note") or "导入版本",
                immutable=False,
            )
            db.add(row)
            available.add(vid)
            imported_versions.append(vid)

        imported_calcs: list[str] = []
        seen_calc_ids: set[str] = set()
        for c in raw_calcs:
            cid = c.get("id")
            if not isinstance(cid, str) or not cid:
                raise ValueError("计算记录缺少 id")
            if cid in seen_calc_ids or db.get(CalculationRecord, cid) is not None:
                raise ValueError(f"计算记录 {cid} 重复或已存在")
            ver = c.get("topology_version_id")
            if ver not in available:
                raise ValueError(f"计算记录 {cid} 引用的拓扑版本 {ver} 不存在")
            result = c.get("result")
            if not isinstance(result, dict):
                raise ValueError(f"计算记录 {cid} 缺少 result")
            # 关键：导入记录的结果强制绑定其自带版本，绝不按当前最新版本重解释
            result = dict(result)
            result["topology_version"] = ver
            result["version_id"] = ver
            db.add(
                CalculationRecord(
                    id=cid,
                    topology_version_id=ver,
                    target_id=c.get("target_id") or result.get("target_id", ""),
                    locks_snapshot=c.get("locks_snapshot", {}),
                    result=result,
                    feasible=bool(c.get("feasible", result.get("feasible"))),
                )
            )
            seen_calc_ids.add(cid)
            imported_calcs.append(cid)

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "imported_versions": imported_versions,
        "imported_calculations": imported_calcs,
    }
