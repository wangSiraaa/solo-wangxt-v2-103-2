"""版本化拓扑服务：快照、草案、校验、发布、按版本锁定、历史与导入导出。

设计要点：

- 已发布版本（TopologyVersion）不可变：内容以 JSON 快照整体保存，
  version 1 为固定演示样例（immutable=True），后续编辑只能产生新版本。
- 草案（TopologyDraft）保存从基线版本复制的可编辑内容；允许保存未通过
  校验的中间状态，但只有校验全部通过才能发布。
- 发布在单个事务内完成：先校验、再插入版本，任何失败都回滚，绝不留下
  半张图。两个草案基于同一基线时，后发布者基线 != 当前最新版本 => 409。
- 阀门锁定按版本存放在 VersionLock；每次计算生成绑定版本的 IsolationRecord。
"""
from __future__ import annotations

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .models import (
    IsolationRecord,
    Node,
    Segment,
    TopologyDraft,
    TopologyVersion,
    Valve,
    VersionLock,
)
from .seed import NODES, SEGMENTS

SNAPSHOT_FORMAT = "isolation-topology/1"
NODE_KINDS = {"source", "equipment", "consumer", "junction"}
SEGMENT_KINDS = {"main", "bypass", "branch"}

IMMUTABLE_V1_NAME = "固定演示样例 v1（不可变）"


class VersionError(RuntimeError):
    def __init__(self, message: str, *, code: str = "error", status: int = 400, extra=None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.extra = extra or {}


# ---------------------------------------------------------------- 快照

def _now() -> datetime:
    return datetime.now(timezone.utc)


def canonical_hash(snapshot: dict[str, Any]) -> str:
    body = {
        "nodes": sorted(
            [
                {k: n[k] for k in ("id", "name", "kind", "x", "y", "essential")}
                for n in snapshot["nodes"]
            ],
            key=lambda n: n["id"],
        ),
        "segments": sorted(
            [
                {
                    "id": s["id"],
                    "upstream_id": s["upstream_id"],
                    "downstream_id": s["downstream_id"],
                    "kind": s["kind"],
                    "is_bypass": s["is_bypass"],
                    "valve_id": (s.get("valve") or {}).get("id"),
                }
                for s in snapshot["segments"]
            ],
            key=lambda s: s["id"],
        ),
    }
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def snapshot_from_seed() -> dict[str, Any]:
    """从固定种子数据构造 v1 快照。"""
    nodes = [dict(n) for n in NODES]
    segments: list[dict[str, Any]] = []
    for seg_id, up, down, valve_id, valve_name, kind, is_bypass in SEGMENTS:
        segments.append(
            {
                "id": seg_id,
                "upstream_id": up,
                "downstream_id": down,
                "kind": kind,
                "is_bypass": is_bypass,
                "valve": {
                    "id": valve_id,
                    "name": valve_name,
                    "is_open": True,
                    "locked": False,
                    "operable": True,
                },
            }
        )
    return {"format": SNAPSHOT_FORMAT, "nodes": nodes, "segments": segments}


def snapshot_from_db(db: Session) -> dict[str, Any]:
    """从旧固定表读取当前结构（供运维导出；v1 快照本身以种子为准）。"""
    nodes = [
        {"id": n.id, "name": n.name, "kind": n.kind, "x": n.x, "y": n.y, "essential": n.essential}
        for n in db.scalars(select(Node)).all()
    ]
    valves = {v.segment_id: v for v in db.scalars(select(Valve)).all()}
    segments = []
    for seg in db.scalars(select(Segment)).all():
        v = valves.get(seg.id)
        segments.append(
            {
                "id": seg.id,
                "upstream_id": seg.upstream_id,
                "downstream_id": seg.downstream_id,
                "kind": seg.kind,
                "is_bypass": seg.is_bypass,
                "valve": (
                    {
                        "id": v.id,
                        "name": v.name,
                        "is_open": v.is_open,
                        "locked": v.locked,
                        "operable": v.operable,
                    }
                    if v
                    else None
                ),
            }
        )
    return {"format": SNAPSHOT_FORMAT, "nodes": nodes, "segments": segments}


def draft_content_to_snapshot(content: dict[str, Any]) -> dict[str, Any]:
    """把草案编辑体（source/target 命名）规范化为快照（upstream/downstream）。"""
    segments = []
    for s in content.get("segments", []):
        valve = s.get("valve")
        segments.append(
            {
                "id": s.get("id"),
                "upstream_id": s.get("source", s.get("upstream_id")),
                "downstream_id": s.get("target", s.get("downstream_id")),
                "kind": s.get("kind", "main"),
                "is_bypass": bool(s.get("is_bypass", False)),
                "valve": (
                    {
                        "id": valve.get("id"),
                        "name": valve.get("name", valve.get("id")),
                        "is_open": bool(valve.get("is_open", True)),
                        "locked": bool(valve.get("locked", False)),
                        "operable": bool(valve.get("operable", True)),
                    }
                    if isinstance(valve, dict) and valve.get("id")
                    else None
                ),
            }
        )
    nodes = [dict(n) for n in content.get("nodes", [])]
    return {"format": SNAPSHOT_FORMAT, "nodes": nodes, "segments": segments}


def snapshot_to_draft_content(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "nodes": [dict(n) for n in snapshot["nodes"]],
        "segments": [
            {
                "id": s["id"],
                "source": s["upstream_id"],
                "target": s["downstream_id"],
                "kind": s.get("kind", "main"),
                "is_bypass": bool(s.get("is_bypass", False)),
                "valve": dict(s["valve"]) if s.get("valve") else None,
            }
            for s in snapshot["segments"]
        ],
    }


# ---------------------------------------------------------------- 校验

def validate_topology(snapshot: dict[str, Any]) -> list[str]:
    """整体校验；返回错误信息列表（空列表 = 通过）。

    校验是“全量”的：任何错误都导致发布被整体拒绝，而不是拒绝部分编辑。
    """
    errors: list[str] = []

    raw_nodes = snapshot.get("nodes")
    raw_segments = snapshot.get("segments")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        errors.append("拓扑必须至少包含一个节点")
        return errors
    if not isinstance(raw_segments, list) or not raw_segments:
        errors.append("拓扑必须至少包含一条管段")
        return errors

    # ---- 节点 ----
    node_ids: set[str] = set()
    for i, n in enumerate(raw_nodes):
        if not isinstance(n, dict):
            errors.append(f"节点定义 #{i} 不是对象（不完整节点定义）")
            continue
        nid = n.get("id")
        where = f"节点 {nid or '#' + str(i)}"
        if not isinstance(nid, str) or not nid.strip():
            errors.append(f"节点 #{i} 缺少有效标识 id")
            continue
        if nid in node_ids:
            errors.append(f"重复节点标识：{nid}")
        node_ids.add(nid)
        if not str(n.get("name", "")).strip():
            errors.append(f"{where} 缺少名称 name")
        if n.get("kind") not in NODE_KINDS:
            errors.append(f"{where} 的 kind 非法（允许 {sorted(NODE_KINDS)}）")
        if not isinstance(n.get("x"), (int, float)) or not isinstance(n.get("y"), (int, float)):
            errors.append(f"{where} 缺少数值坐标 x/y")
        if not isinstance(n.get("essential"), bool):
            errors.append(f"{where} 的 essential 必须为布尔值")

    sources = {
        n["id"] for n in raw_nodes if isinstance(n, dict) and n.get("kind") == "source"
    }
    if not sources:
        errors.append("拓扑缺少介质来源节点（kind=source）")

    # ---- 管段与阀门 ----
    seg_ids: set[str] = set()
    endpoint_pairs: set[tuple[str, str]] = set()
    valve_segments: dict[str, str] = {}  # valve_id -> 第一条绑定的管段
    adjacency: dict[str, set[str]] = {nid: set() for nid in node_ids}

    for i, s in enumerate(raw_segments):
        if not isinstance(s, dict):
            errors.append(f"管段定义 #{i} 不是对象（不完整边定义）")
            continue
        sid = s.get("id")
        where = f"管段 {sid or '#' + str(i)}"
        if not isinstance(sid, str) or not sid.strip():
            errors.append(f"管段 #{i} 缺少有效标识 id")
        elif sid in seg_ids:
            errors.append(f"重复管段标识：{sid}")
        else:
            seg_ids.add(sid)

        up = s.get("upstream_id")
        down = s.get("downstream_id")
        endpoints_ok = True
        if not up or not down:
            errors.append(f"{where} 缺少起点/终点（不完整边定义）")
            endpoints_ok = False
        if up and up not in node_ids:
            errors.append(f"{where} 的起点 {up} 不存在")
            endpoints_ok = False
        if down and down not in node_ids:
            errors.append(f"{where} 的终点 {down} 不存在")
            endpoints_ok = False
        if up and down and up == down:
            errors.append(f"{where} 起点与终点不能相同（{up}）")
            endpoints_ok = False
        if endpoints_ok:
            pair = tuple(sorted((up, down)))
            if pair in endpoint_pairs:
                errors.append(f"{where} 与另一条管段具有相同端点 {up}-{down}（重复边定义）")
            else:
                endpoint_pairs.add(pair)
            adjacency.setdefault(up, set()).add(down)
            adjacency.setdefault(down, set()).add(up)

        if s.get("kind") not in SEGMENT_KINDS:
            errors.append(f"{where} 的 kind 非法（允许 {sorted(SEGMENT_KINDS)}）")
        if not isinstance(s.get("is_bypass"), bool):
            errors.append(f"{where} 的 is_bypass 必须为布尔值")

        v = s.get("valve")
        if v is None:
            # 演示模型约定阀门与管段 1:1；缺阀即无法构成隔离边界，属不完整边定义
            errors.append(f"{where} 缺少阀门定义（阀门与管段 1:1，不完整边定义）")
        elif not isinstance(v, dict):
            errors.append(f"{where} 的阀门定义不是对象（不完整边定义）")
        else:
            vid = v.get("id")
            if not isinstance(vid, str) or not vid.strip():
                errors.append(f"{where} 的阀门缺少有效标识 id")
            elif vid in valve_segments:
                errors.append(
                    f"阀门 {vid} 同时绑定多条管段：{valve_segments[vid]} 与 {sid or '#' + str(i)}"
                )
            else:
                valve_segments[vid] = sid or f"#{i}"
            if not str(v.get("name", "")).strip():
                errors.append(f"{where} 的阀门 {vid or ''} 缺少名称 name")
            for flag in ("is_open", "locked", "operable"):
                if not isinstance(v.get(flag, True), bool):
                    errors.append(f"{where} 的阀门 {vid or ''} 的 {flag} 必须为布尔值")

    # ---- 连通性：孤立目标 / 必要供给点（按无向物理连通，忽略阀门开闭状态）----
    if sources:
        reachable: set[str] = set()
        stack = [next(iter(sources))]
        # 多来源时分别扩展，全部并入 reachable
        for src in sources:
            stack = [src]
            while stack:
                cur = stack.pop()
                if cur in reachable:
                    continue
                reachable.add(cur)
                stack.extend(adjacency.get(cur, ()))
        for n in raw_nodes:
            if not isinstance(n, dict) or n.get("id") not in node_ids:
                continue
            nid = n["id"]
            if nid in reachable or nid in sources:
                continue
            if n.get("essential"):
                errors.append(f"必要供给点 {nid} 孤立：没有从任何来源可达的物理路径")
            elif n.get("kind") == "equipment":
                errors.append(f"目标设备 {nid} 孤立：没有从任何来源可达的物理路径")
            elif n.get("kind") == "consumer":
                errors.append(f"用户节点 {nid} 孤立：没有从任何来源可达的物理路径")
            else:
                errors.append(f"节点 {nid} 孤立：未接入任何管段")

    return errors


# ---------------------------------------------------------------- 版本

def ensure_versioned_seed(db: Session) -> None:
    """幂等确保 v1 不可变样例版本存在。"""
    first = db.scalar(select(TopologyVersion).where(TopologyVersion.version_no == 1))
    if first is not None:
        return
    snap = snapshot_from_seed()
    db.add(
        TopologyVersion(
            version_no=1,
            name=IMMUTABLE_V1_NAME,
            base_version_no=None,
            created_by="system",
            note="培训系统内置固定样例，不可修改、不可删除。",
            snapshot=json.dumps(snap, ensure_ascii=False),
            created_at=_now(),
            immutable=True,
        )
    )
    db.commit()


def _version_meta(v: TopologyVersion) -> dict[str, Any]:
    return {
        "version_no": v.version_no,
        "name": v.name,
        "base_version_no": v.base_version_no,
        "created_by": v.created_by,
        "note": v.note,
        "immutable": v.immutable,
        "created_at": v.created_at.isoformat(),
        "content_hash": canonical_hash(json.loads(v.snapshot)),
    }


def list_versions(db: Session) -> list[dict[str, Any]]:
    vs = db.scalars(select(TopologyVersion).order_by(TopologyVersion.version_no)).all()
    return [_version_meta(v) for v in vs]


def get_version_obj(db: Session, version_no: int) -> TopologyVersion:
    v = db.get(TopologyVersion, version_no)
    if v is None:
        raise VersionError(f"拓扑版本不存在: v{version_no}", code="not_found", status=404)
    return v


def get_version(db: Session, version_no: int) -> dict[str, Any]:
    return _version_meta(get_version_obj(db, version_no))


def load_snapshot(db: Session, version_no: int) -> dict[str, Any]:
    return json.loads(get_version_obj(db, version_no).snapshot)


def latest_version_no(db: Session) -> int:
    return db.scalar(select(TopologyVersion.version_no).order_by(
        TopologyVersion.version_no.desc()
    ).limit(1))


# ---------------------------------------------------------------- 草案

def list_drafts(db: Session) -> list[dict[str, Any]]:
    out = []
    for d in db.scalars(select(TopologyDraft).order_by(TopologyDraft.created_at)).all():
        item = {
            "id": d.id,
            "name": d.name,
            "base_version_no": d.base_version_no,
            "created_by": d.created_by,
            "created_at": d.created_at.isoformat(),
            "updated_at": d.updated_at.isoformat(),
            "published_version_no": d.published_version_no,
        }
        out.append(item)
    return out


def create_draft(db: Session, base_version_no: int, name: str | None, created_by: str = "trainer") -> dict[str, Any]:
    ensure_versioned_seed(db)
    base = get_version_obj(db, base_version_no)
    snap = json.loads(base.snapshot)
    draft = TopologyDraft(
        id=f"d-{uuid.uuid4().hex[:12]}",
        name=name or f"草案（基于 v{base.version_no}）",
        base_version_no=base.version_no,
        content=json.dumps(snapshot_to_draft_content(snap), ensure_ascii=False),
        created_by=created_by,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(draft)
    db.commit()
    return get_draft(db, draft.id)


def _draft_obj(db: Session, draft_id: str) -> TopologyDraft:
    d = db.get(TopologyDraft, draft_id)
    if d is None:
        raise VersionError(f"草案不存在: {draft_id}", code="not_found", status=404)
    return d


def get_draft(db: Session, draft_id: str) -> dict[str, Any]:
    d = _draft_obj(db, draft_id)
    content = json.loads(d.content)
    snapshot = draft_content_to_snapshot(content)
    return {
        "id": d.id,
        "name": d.name,
        "base_version_no": d.base_version_no,
        "created_by": d.created_by,
        "created_at": d.created_at.isoformat(),
        "updated_at": d.updated_at.isoformat(),
        "published_version_no": d.published_version_no,
        "content": content,
        "validation": {
            "valid": not validate_topology(snapshot),
            "errors": validate_topology(snapshot),
        },
    }


def update_draft(db: Session, draft_id: str, body: dict[str, Any]) -> dict[str, Any]:
    d = _draft_obj(db, draft_id)
    if d.published_version_no is not None:
        raise VersionError("草案已发布，不能继续编辑（请从新版本再复制草案）", code="published")
    if "name" in body and str(body["name"]).strip():
        d.name = str(body["name"])[:128]
    if "content" in body:
        content = body["content"]
        if not isinstance(content, dict) or not isinstance(content.get("nodes"), list):
            raise VersionError("草案内容格式不正确：需要 {nodes, segments}")
        # 规范化存储，失败不影响库里旧内容
        d.content = json.dumps(content, ensure_ascii=False)
    d.updated_at = _now()
    db.commit()
    return get_draft(db, draft_id)


def delete_draft(db: Session, draft_id: str) -> None:
    d = _draft_obj(db, draft_id)
    db.delete(d)
    db.commit()


def validate_draft(db: Session, draft_id: str) -> dict[str, Any]:
    return get_draft(db, draft_id)["validation"]


def publish_draft(
    db: Session,
    draft_id: str,
    *,
    expected_base_version_no: int | None = None,
    note: str = "",
    created_by: str = "trainer",
) -> dict[str, Any]:
    """校验通过后把草案发布为新版本。整个过程单事务，失败即回滚。"""
    d = _draft_obj(db, draft_id)
    if d.published_version_no is not None:
        raise VersionError(
            f"草案已发布为 v{d.published_version_no}，不能重复发布", code="published"
        )
    if expected_base_version_no is not None and expected_base_version_no != d.base_version_no:
        raise VersionError(
            f"请求声明的基线 v{expected_base_version_no} 与草案基线 v{d.base_version_no} 不一致",
            code="stale_draft",
        )

    snapshot = draft_content_to_snapshot(json.loads(d.content))
    errors = validate_topology(snapshot)
    if errors:
        # 整体拒绝：不写入任何版本/片段
        raise VersionError(
            f"草案校验失败，发布被整体拒绝（{len(errors)} 项）",
            code="validation_failed",
            status=422,
            extra={"errors": errors},
        )

    latest = latest_version_no(db) or 1
    if d.base_version_no != latest:
        raise VersionError(
            f"修订冲突：草案基线为 v{d.base_version_no}，当前最新版本已为 v{latest}；"
            "请基于最新版本重新复制草案后再发布。",
            code="revision_conflict",
            status=409,
            extra={"base_version_no": d.base_version_no, "latest_version_no": latest},
        )

    try:
        new_no = latest + 1
        version = TopologyVersion(
            version_no=new_no,
            name=d.name,
            base_version_no=d.base_version_no,
            created_by=created_by,
            note=note[:256],
            snapshot=json.dumps(snapshot, ensure_ascii=False),
            created_at=_now(),
            immutable=True,
        )
        db.add(version)
        db.flush()  # 先拿到新行但尚未提交；任何异常都会整体回滚
        d.published_version_no = new_no
        d.updated_at = _now()
        db.commit()
    except Exception:
        db.rollback()
        # 高并发下两个发布可能同时读到同一 latest：其中一个撞版本号主键，
        # 语义上同样是修订冲突（后到者不能覆盖），而不是留下 500/半张图。
        if isinstance(sys.exc_info()[1], IntegrityError):
            now_latest = latest_version_no(db) or 1
            raise VersionError(
                f"修订冲突：并发发布时最新版本已推进到 v{now_latest}，"
                "请基于最新版本重新复制草案后再发布。",
                code="revision_conflict",
                status=409,
                extra={"base_version_no": d.base_version_no, "latest_version_no": now_latest},
            )
        raise

    return get_version(db, new_no)


# ---------------------------------------------------------------- 按版本锁定

def get_locks(db: Session, version_no: int) -> dict[str, bool]:
    get_version_obj(db, version_no)
    rows = db.scalars(select(VersionLock).where(VersionLock.version_no == version_no)).all()
    return {r.valve_id: r.locked for r in rows if r.locked}


def replace_locks(db: Session, version_no: int, locks: dict[str, bool], valid_valve_ids: set[str]) -> dict[str, bool]:
    get_version_obj(db, version_no)
    unknown = sorted(vid for vid in locks if vid not in valid_valve_ids)
    if unknown:
        raise VersionError(f"未知阀门: {', '.join(unknown)}", code="unknown_valve")

    db.query(VersionLock).filter(VersionLock.version_no == version_no).delete()
    for vid, locked in locks.items():
        if locked:
            db.add(VersionLock(version_no=version_no, valve_id=vid, locked=True))
    db.commit()
    return {vid: True for vid, locked in locks.items() if locked}


def reset_locks(db: Session, version_no: int) -> None:
    get_version_obj(db, version_no)
    db.query(VersionLock).filter(VersionLock.version_no == version_no).delete()
    db.commit()


# ---------------------------------------------------------------- 历史记录

def add_record(
    db: Session,
    version: TopologyVersion,
    target_id: str,
    locks: dict[str, bool],
    result: dict[str, Any],
    created_by: str = "trainer",
) -> int:
    rec = IsolationRecord(
        version_no=version.version_no,
        version_name=version.name,
        target_id=target_id,
        locks_json=json.dumps(locks, ensure_ascii=False),
        result_json=json.dumps(result, ensure_ascii=False),
        created_at=_now(),
        created_by=created_by,
    )
    db.add(rec)
    db.commit()
    return rec.id


def _record_dict(r: IsolationRecord) -> dict[str, Any]:
    return {
        "id": r.id,
        "version_no": r.version_no,
        "version_name": r.version_name,
        "target_id": r.target_id,
        "locks": json.loads(r.locks_json or "{}"),
        "result": json.loads(r.result_json),
        "created_at": r.created_at.isoformat(),
        "created_by": r.created_by,
    }


def list_records(db: Session, version_no: int | None = None) -> list[dict[str, Any]]:
    stmt = select(IsolationRecord).order_by(IsolationRecord.id.desc()).limit(100)
    rows = db.scalars(stmt).all()
    out = [_record_dict(r) for r in rows]
    if version_no is not None:
        out = [r for r in out if r["version_no"] == version_no]
    return out


def get_record(db: Session, record_id: int) -> dict[str, Any]:
    r = db.get(IsolationRecord, record_id)
    if r is None:
        raise VersionError(f"计算记录不存在: #{record_id}", code="not_found", status=404)
    rec = _record_dict(r)
    # 历史重绘始终使用记录绑定版本的不可变快照
    rec["topology"] = topology_payload_for_version(db, r.version_no)
    rec["topo_version"] = get_version(db, r.version_no)
    return rec


def topology_payload_for_version(db: Session, version_no: int) -> dict[str, Any]:
    from .isolation import topology_payload_snapshot

    snap = load_snapshot(db, version_no)
    payload = topology_payload_snapshot(snap)
    payload["topo_version"] = get_version(db, version_no)
    return payload


# ---------------------------------------------------------------- 导入导出

def export_bundle(db: Session) -> dict[str, Any]:
    versions = db.scalars(select(TopologyVersion).order_by(TopologyVersion.version_no)).all()
    records = db.scalars(select(IsolationRecord).order_by(IsolationRecord.id)).all()
    return {
        "format": "isolation-bundle/1",
        "exported_at": _now().isoformat(),
        "versions": [
            {
                "version_no": v.version_no,
                "name": v.name,
                "base_version_no": v.base_version_no,
                "created_by": v.created_by,
                "note": v.note,
                "immutable": v.immutable,
                "created_at": v.created_at.isoformat(),
                "content_hash": canonical_hash(json.loads(v.snapshot)),
                "snapshot": json.loads(v.snapshot),
            }
            for v in versions
        ],
        "records": [
            {
                "version_no": r.version_no,
                "target_id": r.target_id,
                "locks": json.loads(r.locks_json or "{}"),
                "result": json.loads(r.result_json),
                "created_at": r.created_at.isoformat(),
                "created_by": r.created_by,
            }
            for r in records
        ],
    }


def import_bundle(db: Session, bundle: dict[str, Any]) -> dict[str, Any]:
    """导入版本与计算记录，保留版本间基线关系与记录-版本绑定。

    快照校验失败的版本整体拒绝；同号且内容哈希一致的版本视为同一版本
    直接复用，其余版本分配新的版本号并重新映射 base_version_no 与记录引用，
    因此导入的旧计算仍按其原有快照绘制，不会被新版本重解释。
    """
    if not isinstance(bundle, dict) or bundle.get("format") != "isolation-bundle/1":
        raise VersionError("导入文件格式不是 isolation-bundle/1", code="bad_format")
    in_versions = bundle.get("versions")
    if not isinstance(in_versions, list) or not in_versions:
        raise VersionError("导入文件不包含任何拓扑版本", code="bad_format")

    ensure_versioned_seed(db)

    no_map: dict[int, int] = {}  # 导入文件版本号 -> 本地版本号
    added = 0
    reused = 0

    try:
        for iv in sorted(in_versions, key=lambda x: x.get("version_no", 0)):
            snap = iv.get("snapshot")
            if not isinstance(snap, dict):
                raise VersionError(f"版本 v{iv.get('version_no')} 缺少快照", code="bad_format")
            errors = validate_topology(snap)
            if errors:
                raise VersionError(
                    f"导入版本 v{iv.get('version_no')} 校验失败，导入被整体拒绝",
                    code="validation_failed",
                    status=422,
                    extra={"errors": errors},
                )
            old_no = iv.get("version_no")
            existing = db.get(TopologyVersion, old_no) if isinstance(old_no, int) else None
            if existing is not None and canonical_hash(json.loads(existing.snapshot)) == canonical_hash(snap):
                no_map[old_no] = existing.version_no
                reused += 1
                continue

            base_old = iv.get("base_version_no")
            base_new = no_map.get(base_old) if isinstance(base_old, int) else None
            new_no = (latest_version_no(db) or 1) + 1
            created_at = _parse_dt(iv.get("created_at")) or _now()
            db.add(
                TopologyVersion(
                    version_no=new_no,
                    name=str(iv.get("name", f"导入版本 v{old_no}"))[:128],
                    base_version_no=base_new,
                    created_by=str(iv.get("created_by", "import"))[:64],
                    note=str(iv.get("note", f"自 v{old_no} 导入"))[:256],
                    snapshot=json.dumps(snap, ensure_ascii=False),
                    created_at=created_at,
                    immutable=True,
                )
            )
            db.flush()
            if isinstance(old_no, int):
                no_map[old_no] = new_no
            added += 1

        records_imported = 0
        for ir in bundle.get("records", []) or []:
            old_vno = ir.get("version_no")
            new_vno = no_map.get(old_vno) if isinstance(old_vno, int) else None
            if new_vno is None:
                # 记录引用的版本未包含在包内：拒绝整次导入，避免悬挂绑定
                raise VersionError(
                    f"计算记录引用的版本 v{old_vno} 不在导入包中，导入被整体拒绝",
                    code="bad_format",
                )
            result = ir.get("result")
            if not isinstance(result, dict):
                raise VersionError("存在缺少结果的计算记录，导入被整体拒绝", code="bad_format")
            version = db.get(TopologyVersion, new_vno)
            # 重写结果中的版本绑定，旧计算继续指向它原来那张图（内容哈希不变）
            result["topo_version"] = {
                "version_no": new_vno,
                "name": version.name,
                "immutable": version.immutable,
                "base_version_no": version.base_version_no,
                "imported_from_version_no": old_vno,
            }
            db.add(
                IsolationRecord(
                    version_no=new_vno,
                    version_name=version.name,
                    target_id=str(ir.get("target_id", "")),
                    locks_json=json.dumps(ir.get("locks") or {}, ensure_ascii=False),
                    result_json=json.dumps(result, ensure_ascii=False),
                    created_at=_parse_dt(ir.get("created_at")) or _now(),
                    created_by=str(ir.get("created_by", "import"))[:64],
                )
            )
            records_imported += 1

        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "versions_added": added,
        "versions_reused": reused,
        "records_imported": records_imported,
        "version_no_map": {str(k): v for k, v in sorted(no_map.items())},
    }


def _parse_dt(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None) if dt.tzinfo else dt
    except ValueError:
        return None
