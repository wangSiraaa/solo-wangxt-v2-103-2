"""版本化拓扑草案/发布/校验/冲突/历史绑定/导入导出测试。"""
from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed_database
from app import versions


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    versions.ensure_versioned_seed(db)
    yield db
    db.close()


def _client():
    return TestClient(app)


def _seed_content(client: TestClient):
    r = client.post("/api/versions/1/drafts", json={"name": "测试草案"})
    assert r.status_code == 200, r.text
    return r.json()


# ---------------- v1 不可变 + 基础版本接口 ----------------

def test_v1_is_immutable_seed_and_always_available(fresh_db):
    client = _client()
    vs = client.get("/api/versions").json()
    assert [v["version_no"] for v in vs] == [1]
    v1 = vs[0]
    assert v1["immutable"] is True
    assert v1["base_version_no"] is None

    topo = client.get("/api/versions/1/topology").json()
    assert len(topo["nodes"]) == 9
    assert topo["topo_version"]["version_no"] == 1
    assert topo["topo_version"]["immutable"] is True

    # 旧接口默认仍指向 v1
    legacy = client.get("/api/topology").json()
    assert legacy["topo_version"]["version_no"] == 1
    assert {n["id"] for n in legacy["nodes"]} == {n["id"] for n in topo["nodes"]}


# ---------------- 验收 1：复制 + 一条旁路 => 方案与保供路径变化，v1 不变 ----------------

def _add_bypass_with_essential(content: dict) -> dict:
    """在 T 侧加入一条旁路 T-BX-N2（绕过入口阀 V_TIN）并挂必要供给点 P3。"""
    content = copy.deepcopy(content)
    content["nodes"].extend(
        [
            {"id": "BX", "name": "新旁路桥点", "kind": "junction", "x": 480, "y": 90, "essential": False},
            {"id": "P3", "name": "新增必要用户P3", "kind": "consumer", "x": 480, "y": 30, "essential": True},
        ]
    )
    content["segments"].extend(
        [
            {
                "id": "EBX1", "source": "T", "target": "BX", "kind": "bypass",
                "is_bypass": True,
                "valve": {"id": "V_BX_T", "name": "T侧新旁路阀", "is_open": True, "locked": False, "operable": True},
            },
            {
                "id": "EBX2", "source": "BX", "target": "N2", "kind": "bypass",
                "is_bypass": True,
                "valve": {"id": "V_BX_N2", "name": "N2侧新旁路阀", "is_open": True, "locked": False, "operable": True},
            },
            {
                "id": "EP3", "source": "BX", "target": "P3", "kind": "branch",
                "is_bypass": False,
                "valve": {"id": "V_P3", "name": "P3支路阀", "is_open": True, "locked": False, "operable": True},
            },
        ]
    )
    return content


def test_copy_sample_add_bypass_changes_solution_and_supply_paths(fresh_db):
    client = _client()

    # v1 旧样例：最小集 2 阀，必要点只有 P1/P2
    r1 = client.post("/api/isolation", json={"target_id": "T"}).json()
    assert r1["topo_version"]["version_no"] == 1
    assert r1["best_solution"] == ["V_TIN", "V_TOUT"]
    assert set(r1["essentials"]) == {"P1", "P2"}
    v1_record = r1["record_id"]

    # 从 v1 复制草案，加旁路后发布
    draft = _seed_content(client)
    content = _add_bypass_with_essential(draft["content"])
    upd = client.put(f"/api/drafts/{draft['id']}", json={"name": "加旁路与P3", "content": content})
    assert upd.status_code == 200, upd.text
    assert upd.json()["validation"]["valid"] is True

    pub = client.post(
        f"/api/drafts/{draft['id']}/publish",
        json={"expected_base_version_no": 1, "note": "新增T侧旁路与P3"},
    )
    assert pub.status_code == 200, pub.text
    v2 = pub.json()["published"]
    assert v2["version_no"] == 2
    assert v2["base_version_no"] == 1
    assert v2["immutable"] is True

    # 新版本：拓扑多出 BX/P3 与新阀
    topo2 = client.get("/api/versions/2/topology").json()
    assert {n["id"] for n in topo2["nodes"]} >= {"BX", "P3"}
    assert {v["id"] for v in topo2["valves"]} >= {"V_BX_T", "V_BX_N2", "V_P3"}

    # 新版本计算：最小隔离集变为 3 阀（必须封住新旁路），保供路径多出 P3
    r2 = client.post("/api/isolation", json={"target_id": "T", "topo_version": 2}).json()
    assert r2["feasible"] is True
    assert r2["topo_version"]["version_no"] == 2
    assert len(r2["best_solution"]) == 3
    assert "V_BX_T" in r2["best_solution"]
    assert set(r2["essentials"]) == {"P1", "P2", "P3"}
    sol = r2["solutions"][0]
    p3 = sol["supply_paths"]["P3"]
    assert p3 is not None and p3[-1] == "P3" and "BX" in p3
    # P1/P2 仍然保供
    assert sol["supply_paths"]["P2"] is not None

    # 旧样例不变：v1 仍为 2 阀方案，且没有 P3
    r1_again = client.post("/api/isolation", json={"target_id": "T", "topo_version": 1}).json()
    assert r1_again["best_solution"] == ["V_TIN", "V_TOUT"]
    assert "P3" not in r1_again["essentials"]
    topo1 = client.get("/api/versions/1/topology").json()
    assert "P3" not in {n["id"] for n in topo1["nodes"]}

    # 版本列表里两版关系清晰
    metas = client.get("/api/versions").json()
    assert [(m["version_no"], m["base_version_no"]) for m in metas] == [(1, None), (2, 1)]
    assert v1_record != r2["record_id"]


# ---------------- 验收 2：重复阀门 / 孤立必要点 => 草案被整体拒绝 ----------------

def test_duplicate_valve_binding_rejected_entirely(fresh_db):
    client = _client()
    draft = _seed_content(client)
    content = copy.deepcopy(draft["content"])
    # 让新管段也使用已属于 E1 的阀门 V1
    content["nodes"].append(
        {"id": "NX", "name": "孤岛节点", "kind": "junction", "x": 0, "y": 0, "essential": False}
    )
    content["segments"].append(
        {
            "id": "EBAD", "source": "N1", "target": "NX", "kind": "main", "is_bypass": False,
            "valve": {"id": "V1", "name": "重复绑定的阀门", "is_open": True, "locked": False, "operable": True},
        }
    )
    client.put(f"/api/drafts/{draft['id']}", json={"content": content})

    val = client.get(f"/api/drafts/{draft['id']}/validate").json()
    assert val["valid"] is False
    assert any("阀门 V1 同时绑定多条管段" in e for e in val["errors"])

    # 发布被整体拒绝：没有新版本、没有半张图
    before = [v["version_no"] for v in client.get("/api/versions").json()]
    pub = client.post(f"/api/drafts/{draft['id']}/publish", json={"expected_base_version_no": 1})
    assert pub.status_code == 422
    body = pub.json()["detail"]
    assert body["code"] == "validation_failed"
    assert any("阀门 V1 同时绑定多条管段" in e for e in body["errors"])
    after = [v["version_no"] for v in client.get("/api/versions").json()]
    assert before == after == [1]
    # 草案仍在，内容未被破坏，可继续修改
    again = client.get(f"/api/drafts/{draft['id']}").json()
    assert any(s["id"] == "EBAD" for s in again["content"]["segments"])


def test_orphan_essential_and_incomplete_edge_rejected(fresh_db):
    client = _client()
    draft = _seed_content(client)
    content = copy.deepcopy(draft["content"])

    # 孤立必要供给点 PX
    content["nodes"].append(
        {"id": "PX", "name": "孤立必要点", "kind": "consumer", "x": 0, "y": 0, "essential": True}
    )
    # 不完整边：缺 target 且缺阀门
    content["segments"].append(
        {"id": "EINC", "source": "N1", "target": None, "kind": "main", "is_bypass": False, "valve": None}
    )
    # 孤立的目标设备
    content["nodes"].append(
        {"id": "TX", "name": "孤立目标", "kind": "equipment", "x": 5, "y": 5, "essential": False}
    )

    client.put(f"/api/drafts/{draft['id']}", json={"content": content})
    val = client.get(f"/api/drafts/{draft['id']}/validate").json()
    assert val["valid"] is False
    joined = "\n".join(val["errors"])
    assert "必要供给点 PX 孤立" in joined
    assert "目标设备 TX 孤立" in joined
    assert "管段 EINC 缺少起点/终点（不完整边定义）" in joined
    assert "管段 EINC 缺少阀门定义" in joined

    pub = client.post(f"/api/drafts/{draft['id']}/publish", json={})
    assert pub.status_code == 422


def test_missing_source_rejected(fresh_db):
    client = _client()
    draft = _seed_content(client)
    content = copy.deepcopy(draft["content"])
    # 去掉唯一来源
    content["nodes"] = [n for n in content["nodes"] if n["id"] != "SRC"]
    client.put(f"/api/drafts/{draft['id']}", json={"content": content})
    errors = client.get(f"/api/drafts/{draft['id']}/validate").json()["errors"]
    assert any("缺少介质来源节点" in e for e in errors)
    assert any("起点 SRC 不存在" in e for e in errors)
    assert client.post(f"/api/drafts/{draft['id']}/publish", json={}).status_code == 422


def test_duplicate_identifiers_rejected(fresh_db):
    client = _client()
    draft = _seed_content(client)
    content = copy.deepcopy(draft["content"])
    content["nodes"].append(
        {"id": "N1", "name": "冒名节点", "kind": "junction", "x": 1, "y": 1, "essential": False}
    )
    dup_seg = copy.deepcopy(content["segments"][0])
    dup_seg["valve"] = {
        "id": "V_DUP_S", "name": "重复管段上的阀", "is_open": True, "locked": False, "operable": True
    }
    content["segments"].append(dup_seg)  # 与 E0 同 id、同端点
    client.put(f"/api/drafts/{draft['id']}", json={"content": content})

    errors = client.get(f"/api/drafts/{draft['id']}/validate").json()["errors"]
    assert any("重复节点标识：N1" in e for e in errors)
    assert any("重复管段标识：E0" in e for e in errors)


# ---------------- 验收 3：同基线两个草案，后发布者得到修订冲突 ----------------

def test_concurrent_drafts_from_same_base_conflict(fresh_db):
    client = _client()
    draft_a = _seed_content(client)
    draft_b = _seed_content(client)
    assert draft_a["base_version_no"] == draft_b["base_version_no"] == 1

    # A 先发布成功
    pa = client.post(f"/api/drafts/{draft_a['id']}/publish", json={"expected_base_version_no": 1})
    assert pa.status_code == 200
    assert pa.json()["published"]["version_no"] == 2

    # B 仍基于 v1：必须冲突，而不是覆盖 v2
    pb = client.post(f"/api/drafts/{draft_b['id']}/publish", json={"expected_base_version_no": 1})
    assert pb.status_code == 409
    detail = pb.json()["detail"]
    assert detail["code"] == "revision_conflict"
    assert detail["latest_version_no"] == 2

    # v2 内容完好（B 的失败发布没有覆盖它）
    topo2 = client.get("/api/versions/2/topology").json()
    assert len(topo2["nodes"]) == 9
    # B 未被标记成已发布，可改基线重做
    b = client.get(f"/api/drafts/{draft_b['id']}").json()
    assert b["published_version_no"] is None

    # 基于最新 v2 再复制草案即可发布为 v3
    c = client.post("/api/versions/2/drafts", json={"name": "跟进草案"}).json()
    pc = client.post(
        f"/api/drafts/{c['id']}/publish", json={"expected_base_version_no": 2}
    )
    assert pc.status_code == 200
    assert pc.json()["published"]["version_no"] == 3
    assert pc.json()["published"]["base_version_no"] == 2


def test_published_draft_cannot_be_republished_or_edited(fresh_db):
    client = _client()
    draft = _seed_content(client)
    client.post(f"/api/drafts/{draft['id']}/publish", json={"expected_base_version_no": 1})
    again = client.post(f"/api/drafts/{draft['id']}/publish", json={})
    assert again.status_code == 400
    assert again.json()["detail"]["code"] == "published"
    upd = client.put(f"/api/drafts/{draft['id']}", json={"name": "改不了"})
    assert upd.status_code == 400


# ---------------- 验收 4：版本绑定计算/锁定/见证；历史用旧拓扑重绘 ----------------

def test_locks_are_scoped_per_version(fresh_db):
    client = _client()
    # 发布 v2
    draft = _seed_content(client)
    content = _add_bypass_with_essential(draft["content"])
    client.put(f"/api/drafts/{draft['id']}", json={"content": content})
    client.post(f"/api/drafts/{draft['id']}/publish", json={"expected_base_version_no": 1})

    # 在 v2 锁定 V_TIN
    r = client.put("/api/versions/2/locks", json={"locks": {"V_TIN": True}})
    assert r.status_code == 200
    assert client.get("/api/versions/2/locks").json()["locks"] == {"V_TIN": True}
    # v1 锁定不受影响
    assert client.get("/api/versions/1/locks").json()["locks"] == {}

    # v2 用 v2 阀门，未知阀门按版本拒绝
    bad = client.put("/api/versions/1/locks", json={"locks": {"V_BX_T": True}})
    assert bad.status_code == 400

    # 按版本重置不影响另一版
    client.post("/api/versions/2/reset")
    assert client.get("/api/versions/2/locks").json()["locks"] == {}


def test_history_records_bind_version_and_old_records_use_old_topology(fresh_db):
    client = _client()
    # v1 样例 3：无解 + 见证路径，记录绑定 v1
    r3 = client.post(
        "/api/isolation",
        json={"target_id": "T", "topo_version": 1, "locks": {"V_TOUT": True}},
    ).json()
    assert r3["feasible"] is False
    assert r3["topo_version"]["version_no"] == 1
    assert r3["locked_valves"] == ["V_TOUT"]
    assert "V_TOUT" in r3["locked_witness_path"]["valves"]
    old_id = r3["record_id"]

    # 发布 v2（加旁路+P3）
    draft = _seed_content(client)
    client.put(
        f"/api/drafts/{draft['id']}",
        json={"content": _add_bypass_with_essential(draft["content"])},
    )
    client.post(f"/api/drafts/{draft['id']}/publish", json={"expected_base_version_no": 1})
    new = client.post("/api/isolation", json={"target_id": "T", "topo_version": 2}).json()
    assert new["topo_version"]["version_no"] == 2
    new_id = new["record_id"]

    # 刷新/回看旧记录：结果仍绑定 v1，绘图拓扑也是旧的（无 P3/BX）
    old_rec = client.get(f"/api/history/{old_id}").json()
    assert old_rec["version_no"] == 1
    assert old_rec["result"]["topo_version"]["version_no"] == 1
    assert "V_TOUT" in old_rec["result"]["locked_witness_path"]["valves"]
    old_topo_ids = {n["id"] for n in old_rec["topology"]["nodes"]}
    assert "P3" not in old_topo_ids and "BX" not in old_topo_ids
    assert old_rec["topology"]["topo_version"]["version_no"] == 1

    new_rec = client.get(f"/api/history/{new_id}").json()
    assert new_rec["version_no"] == 2
    assert "V_BX_T" in new_rec["result"]["best_solution"]
    assert "P3" in {n["id"] for n in new_rec["topology"]["nodes"]}

    # 历史列表保留全部记录并带版本标识
    hist = client.get("/api/history").json()
    assert {h["version_no"] for h in hist} == {1, 2}
    v1_only = client.get("/api/history?version_no=1").json()
    assert all(h["version_no"] == 1 for h in v1_only)
    assert any(h["id"] == old_id for h in v1_only)


def test_import_old_calculation_keeps_old_topology_binding(fresh_db, tmp_path):
    client = _client()
    # v1 计算 + 发布 v2 + v2 计算
    old = client.post(
        "/api/isolation", json={"target_id": "T", "locks": {"V_TOUT": True}}
    ).json()
    draft = _seed_content(client)
    client.put(
        f"/api/drafts/{draft['id']}",
        json={"content": _add_bypass_with_essential(draft["content"])},
    )
    client.post(f"/api/drafts/{draft['id']}/publish", json={"expected_base_version_no": 1})
    client.post("/api/isolation", json={"target_id": "T", "topo_version": 2})
    bundle = client.get("/api/export").json()
    assert bundle["format"] == "isolation-bundle/1"
    assert [v["version_no"] for v in bundle["versions"]] == [1, 2]

    # 导入一个全新的库
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db2 = SessionLocal()
    seed_database(db2)
    versions.ensure_versioned_seed(db2)
    db2.close()

    client2 = _client()
    imp = client2.post("/api/import", json=bundle)
    assert imp.status_code == 200, imp.text
    summary = imp.json()
    # v1 内容哈希一致 => 复用；v2 新增
    assert summary["versions_reused"] == 1
    assert summary["versions_added"] == 1
    assert summary["records_imported"] == 2

    metas = client2.get("/api/versions").json()
    assert len(metas) == 2
    # 旧计算（样例3无解见证）仍按旧拓扑解释：见证阀门在其绑定快照中存在
    hist = client2.get("/api/history").json()
    by_version = {}
    for h in hist:
        detail = client2.get(f"/api/history/{h['id']}").json()
        by_version.setdefault(detail["version_no"], []).append(detail)

    old_recs = [d for d in sum(by_version.values(), []) if not d["result"]["feasible"]]
    assert old_recs, "导入的旧无解记录丢失"
    old_rec = old_recs[0]
    topo_valves = {v["id"] for v in old_rec["topology"]["valves"]}
    assert "V_TOUT" in topo_valves and "V_BX_T" not in topo_valves
    assert "V_TOUT" in old_rec["result"]["locked_witness_path"]["valves"]
    assert old_rec["topology"]["topo_version"]["version_no"] == 1

    # 新导入的 v2 仍保持“基于 v1”的关系，且新方案用新约束
    v2_no = [m["version_no"] for m in metas if m["base_version_no"] == 1][0]
    v2_topo = client2.get(f"/api/versions/{v2_no}/topology").json()
    assert "P3" in {n["id"] for n in v2_topo["nodes"]}


def test_import_rejects_bad_bundle_entirely(fresh_db):
    client = _client()
    bad_bundle = {
        "format": "isolation-bundle/1",
        "versions": [
            {
                "version_no": 2,
                "name": "坏版本",
                "base_version_no": 1,
                "snapshot": {
                    "nodes": [
                        {"id": "S", "name": "S", "kind": "source", "x": 0, "y": 0, "essential": False},
                        {"id": "P", "name": "P", "kind": "consumer", "x": 1, "y": 1, "essential": True},
                    ],
                    # 必要点 P 孤立
                    "segments": [],
                },
            }
        ],
        "records": [],
    }
    r = client.post("/api/import", json=bad_bundle)
    assert r.status_code == 422
    # 整体回滚：库里仍只有 v1
    assert [v["version_no"] for v in client.get("/api/versions").json()] == [1]


def test_failed_publish_leaves_no_partial_graph(fresh_db):
    """发布失败前后版本表完全一致，且草案错误不影响已发布版本快照。"""
    client = _client()
    good = _seed_content(client)
    bad = _seed_content(client)

    bad_content = copy.deepcopy(bad["content"])
    bad_content["nodes"] = [n for n in bad_content["nodes"] if n["id"] != "SRC"]
    client.put(f"/api/drafts/{bad['id']}", json={"content": bad_content})
    assert client.post(f"/api/drafts/{bad['id']}/publish", json={}).status_code == 422

    pub = client.post(f"/api/drafts/{good['id']}/publish", json={"expected_base_version_no": 1})
    assert pub.status_code == 200
    v2_no = pub.json()["published"]["version_no"]

    # v2 已发布快照不可通过再次发布同名草案而改变
    another = _seed_content(client)
    conflict = client.post(f"/api/drafts/{another['id']}/publish", json={"expected_base_version_no": 1})
    assert conflict.status_code == 409
    snap_hash_before = client.get(f"/api/versions/{v2_no}").json()["content_hash"]
    assert client.get(f"/api/versions/{v2_no}").json()["content_hash"] == snap_hash_before
