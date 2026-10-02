"""版本化拓扑：快照内容格式、草案校验、v1 表结构与快照互转。

快照 content 是版本与草案共同使用的唯一可编辑/可交换格式：

    {
      "nodes": [
        {"id", "name", "kind", "x", "y", "essential"}
      ],
      "segments": [
        {"id", "upstream_id", "downstream_id", "kind", "is_bypass",
         "valve": {"id", "name", "is_open", "operable"} | null}
      ]
    }

说明：
- 管段方向 = upstream_id -> downstream_id（介质名义流向）；隔离仍按无向物理连通计算。
- 阀门与管段 1:1（或管段无阀）。锁阀状态不属于拓扑结构：发布快照不保存 locked，
  运行时锁定分别落在 v1 的 Valve 表与 v2+ 的 LockRecord 表。
- 校验只在“发布”时强制通过；草案允许保存半成品，便于演示错误被整体拒绝。
"""
from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Node, Segment, Valve

VALID_NODE_KINDS = {"source", "equipment", "consumer", "junction"}
VALID_SEGMENT_KINDS = {"main", "bypass", "branch"}

# 仅允许常规标识字符，避免出现空 id / 纯空白 / 注入样式标识
_ID_CHARS_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def _valid_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 32
        and all(c in _ID_CHARS_OK for c in value)
    )


def _err(errors: list[dict[str, Any]], code: str, message: str, ref: str | None = None) -> None:
    item: dict[str, Any] = {"code": code, "message": message}
    if ref is not None:
        item["ref"] = ref
    errors.append(item)


# ---------------------------------------------------------------- 校验

def validate_content(content: Any) -> list[dict[str, Any]]:
    """对草案快照做发布前校验，返回错误列表；空列表表示通过。

    覆盖（对应需求的硬性校验）：
    - duplicate_id            重复节点/管段/阀门标识
    - valve_bound_twice       同一阀门绑定多条管段（阀门 id 重复）
    - orphan_node             孤立的来源/目标设备/必要供给点（无任何管段相连）
    - missing_source          缺失介质来源
    - incomplete_edge         不完整边定义（缺端点/端点不存在/自环/平行边/字段非法）
    - bad_field               字段缺失或类型非法
    """
    errors: list[dict[str, Any]] = []

    if not isinstance(content, dict):
        _err(errors, "bad_field", "拓扑内容必须是包含 nodes 与 segments 的对象")
        return errors

    raw_nodes = content.get("nodes")
    raw_segments = content.get("segments")
    if not isinstance(raw_nodes, list):
        _err(errors, "bad_field", "nodes 必须是数组")
        raw_nodes = []
    if not isinstance(raw_segments, list):
        _err(errors, "bad_field", "segments 必须是数组")
        raw_segments = []

    # ---- 节点 ----
    node_ids: set[str] = set()
    for i, n in enumerate(raw_nodes):
        ref = f"nodes[{i}]"
        if not isinstance(n, dict):
            _err(errors, "bad_field", "节点必须是对象", ref)
            continue
        nid = n.get("id")
        if not _valid_id(nid):
            _err(errors, "bad_field", "节点 id 缺失或非法（1-32 位字母/数字/_-）", ref)
        elif nid in node_ids:
            _err(errors, "duplicate_id", f"节点标识重复: {nid}", nid)
        else:
            node_ids.add(nid)
        if not isinstance(n.get("name"), str) or not n["name"].strip():
            _err(errors, "bad_field", "节点名称缺失", ref + (f"({nid})" if nid else ""))
        kind = n.get("kind")
        if kind not in VALID_NODE_KINDS:
            _err(errors, "bad_field", f"节点类型非法: {kind!r}（允许 {sorted(VALID_NODE_KINDS)}）",
                 ref + (f"({nid})" if nid else ""))
        if not isinstance(n.get("essential"), bool):
            _err(errors, "bad_field", "essential 必须是布尔值", ref + (f"({nid})" if nid else ""))
        for coord in ("x", "y"):
            if not isinstance(n.get(coord), (int, float)) or isinstance(n.get(coord), bool):
                _err(errors, "bad_field", f"坐标 {coord} 必须是数值", ref + (f"({nid})" if nid else ""))

    # ---- 管段 ----
    segment_ids: set[str] = set()
    valve_owner: dict[str, str] = {}  # valve_id -> 第一段管段 id
    endpoint_pairs: set[tuple[str, str]] = set()
    degree: dict[str, int] = {nid: 0 for nid in node_ids}

    for i, s in enumerate(raw_segments):
        ref = f"segments[{i}]"
        if not isinstance(s, dict):
            _err(errors, "bad_field", "管段必须是对象", ref)
            continue
        sid = s.get("id")
        if not _valid_id(sid):
            _err(errors, "bad_field", "管段 id 缺失或非法（1-32 位字母/数字/_-）", ref)
        elif sid in segment_ids:
            _err(errors, "duplicate_id", f"管段标识重复: {sid}", sid)
        else:
            segment_ids.add(sid)

        up = s.get("upstream_id")
        down = s.get("downstream_id")
        if not isinstance(up, str) or not up:
            _err(errors, "incomplete_edge", "管段缺少 upstream_id", ref + (f"({sid})" if sid else ""))
        elif up not in node_ids:
            _err(errors, "incomplete_edge", f"管段起点节点不存在: {up}", ref + (f"({sid})" if sid else ""))
        if not isinstance(down, str) or not down:
            _err(errors, "incomplete_edge", "管段缺少 downstream_id", ref + (f"({sid})" if sid else ""))
        elif down not in node_ids:
            _err(errors, "incomplete_edge", f"管段终点节点不存在: {down}",
                 ref + (f"({sid})" if sid else ""))
        if isinstance(up, str) and isinstance(down, str) and up == down and up in node_ids:
            _err(errors, "incomplete_edge", f"管段自环（起终点相同）: {up}",
                 ref + (f"({sid})" if sid else ""))

        kind = s.get("kind")
        if kind not in VALID_SEGMENT_KINDS:
            _err(errors, "bad_field", f"管段类型非法: {kind!r}（允许 {sorted(VALID_SEGMENT_KINDS)}）",
                 ref + (f"({sid})" if sid else ""))
        if not isinstance(s.get("is_bypass"), bool):
            _err(errors, "bad_field", "is_bypass 必须是布尔值", ref + (f"({sid})" if sid else ""))

        if isinstance(up, str) and isinstance(down, str) and up in node_ids and down in node_ids:
            pair = tuple(sorted((up, down)))
            if pair in endpoint_pairs:
                _err(
                    errors,
                    "incomplete_edge",
                    f"节点 {up} 与 {down} 之间已存在管段：演示模型中平行管段无法区分，"
                    "请改用中间桥点",
                    ref + (f"({sid})" if sid else ""),
                )
            else:
                endpoint_pairs.add(pair)
            degree[up] = degree.get(up, 0) + 1
            degree[down] = degree.get(down, 0) + 1

        valve = s.get("valve")
        if valve is not None:
            vref = ref + ".valve"
            if not isinstance(valve, dict):
                _err(errors, "bad_field", "valve 必须是对象或 null", vref)
            else:
                vid = valve.get("id")
                if not _valid_id(vid):
                    _err(errors, "bad_field", "阀门 id 缺失或非法", vref)
                else:
                    owner = valve_owner.get(vid)
                    if owner is not None:
                        _err(
                            errors,
                            "valve_bound_twice",
                            f"阀门 {vid} 同时绑定管段 {owner} 与 {sid}，"
                            "演示模型中一只阀门只能属于一条管段",
                            vid,
                        )
                    else:
                        valve_owner[vid] = sid
                if not isinstance(valve.get("name"), str) or not valve["name"].strip():
                    _err(errors, "bad_field", "阀门名称缺失", vref + (f"({vid})" if vid else ""))
                for flag in ("is_open", "operable"):
                    if not isinstance(valve.get(flag), bool):
                        _err(errors, "bad_field", f"阀门 {flag} 必须是布尔值",
                             vref + (f"({vid})" if vid else ""))

    if not node_ids:
        _err(errors, "bad_field", "拓扑至少需要一个节点")
    if not segment_ids:
        _err(errors, "incomplete_edge", "拓扑至少需要一条管段")

    # ---- 整体语义 ----
    nodes_by_id = {n["id"]: n for n in raw_nodes if isinstance(n, dict) and _valid_id(n.get("id"))}
    sources = [nid for nid, n in nodes_by_id.items() if n.get("kind") == "source"]
    if not sources:
        _err(errors, "missing_source", "拓扑缺少介质来源节点（kind=source）")

    # 来源 / 目标设备 / 必要供给点不允许孤立：它们必须出现在至少一条完整管段上。
    for nid, n in sorted(nodes_by_id.items()):
        must_connect = n.get("kind") == "source" or n.get("kind") == "equipment" or n.get("essential")
        if must_connect and degree.get(nid, 0) == 0:
            if n.get("essential"):
                _err(errors, "orphan_node", f"必要供给点 {nid} 孤立：没有任何管段与之相连", nid)
            elif n.get("kind") == "equipment":
                _err(errors, "orphan_node", f"目标设备 {nid} 孤立：没有任何管段与之相连", nid)
            else:
                _err(errors, "orphan_node", f"介质来源 {nid} 孤立：没有任何管段与之相连", nid)

    return errors


# ---------------------------------------------------------------- 转换

def content_from_v1_tables(db: Session) -> dict[str, Any]:
    """从 v1 三张原表读出结构快照（锁定状态不进快照）。"""
    nodes = list(db.scalars(select(Node)).all())
    segments = list(db.scalars(select(Segment)).all())
    valves = {v.segment_id: v for v in db.scalars(select(Valve)).all()}
    return {
        "nodes": [
            {
                "id": n.id,
                "name": n.name,
                "kind": n.kind,
                "x": n.x,
                "y": n.y,
                "essential": n.essential,
            }
            for n in nodes
        ],
        "segments": [
            {
                "id": s.id,
                "upstream_id": s.upstream_id,
                "downstream_id": s.downstream_id,
                "kind": s.kind,
                "is_bypass": s.is_bypass,
                "valve": _valve_snapshot(valves.get(s.id)),
            }
            for s in sorted(segments, key=lambda x: x.id)
        ],
    }


def _valve_snapshot(v: Valve | None) -> dict[str, Any] | None:
    if v is None:
        return None
    return {"id": v.id, "name": v.name, "is_open": v.is_open, "operable": v.operable}


def normalize_for_publish(content: dict[str, Any]) -> dict[str, Any]:
    """发布前复制并规范化：去掉多余字段、排序，确保快照形状稳定。"""
    nodes = [
        {
            "id": n["id"],
            "name": str(n["name"]).strip(),
            "kind": n["kind"],
            "x": float(n["x"]),
            "y": float(n["y"]),
            "essential": bool(n["essential"]),
        }
        for n in content["nodes"]
    ]
    nodes.sort(key=lambda n: n["id"])
    segments = []
    for s in content["segments"]:
        v = s.get("valve")
        segments.append(
            {
                "id": s["id"],
                "upstream_id": s["upstream_id"],
                "downstream_id": s["downstream_id"],
                "kind": s["kind"],
                "is_bypass": bool(s["is_bypass"]),
                "valve": (
                    None
                    if v is None
                    else {
                        "id": v["id"],
                        "name": str(v["name"]).strip(),
                        "is_open": bool(v["is_open"]),
                        "operable": bool(v["operable"]),
                    }
                ),
            }
        )
    segments.sort(key=lambda s: s["id"])
    return {"nodes": nodes, "segments": segments}


def nodes_edges_from_content(
    content: dict[str, Any], locks: dict[str, bool] | None = None
) -> tuple[list[SimpleNamespace], dict[str, dict[str, Any]]]:
    """把快照转换成隔离引擎使用的 (nodes, edges) 结构。

    locks 只覆盖运行时锁定态，不修改快照。调用方应先通过 validate_content。
    """
    locks = locks or {}
    nodes = [
        SimpleNamespace(
            id=n["id"], name=n["name"], kind=n["kind"],
            x=float(n["x"]), y=float(n["y"]), essential=bool(n["essential"]),
        )
        for n in content["nodes"]
    ]
    edges: dict[str, dict[str, Any]] = {}
    for s in content["segments"]:
        v = s.get("valve")
        vid = v["id"] if v else None
        edges[s["id"]] = {
            "id": s["id"],
            "u": s["upstream_id"],
            "v": s["downstream_id"],
            "direction": f"{s['upstream_id']}->{s['downstream_id']}",
            "kind": s["kind"],
            "is_bypass": bool(s["is_bypass"]),
            "valve_id": vid,
            "valve_name": v["name"] if v else None,
            "is_open": bool(v["is_open"]) if v else True,
            "locked": bool(locks.get(vid, False)) if vid else False,
            "operable": bool(v["operable"]) if v else True,
        }
    return nodes, edges


def clone_content(content: dict[str, Any]) -> dict[str, Any]:
    """深拷贝快照，避免草案编辑意外污染发布版本。"""
    return copy.deepcopy(content)
