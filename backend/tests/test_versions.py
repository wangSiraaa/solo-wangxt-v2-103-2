"""版本化拓扑草案 / 发布 / 冲突 / 版本绑定结果的验收测试。"""
from __future__ import annotations

import copy

from fastapi.testclient import TestClient

from app.database import Base, SessionLocal, engine
from app.main import app
from app.seed import seed_database


def _reset_seed():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    db = SessionLocal()
    seed_database(db)
    db.close()


def _add_second_bypass(content: dict) -> dict:
    """从 v1 复制并加入：T-B3-N3 第二条旁路 + 必要供给点 P3（挂 N3）。"""
    content = copy.deepcopy(content)
    content["nodes"].extend(
        [
            {"id": "B3", "name": "新旁路桥点", "kind": "junction", "x": 680, "y": 340,
             "essential": False},
            {"id": "P3", "name": "新增必要用户P3", "kind": "consumer", "x": 1040, "y": 340,
             "essential": True},
        ]
    )
    content["segments"].extend(
        [
            {"id": "EC1", "upstream_id": "T", "downstream_id": "B3", "kind": "bypass",
             "is_bypass": True,
             "valve": {"id": "V_BP3_IN", "name": "新旁路入口阀", "is_open": True,
                       "operable": True}},
            {"id": "EC2", "upstream_id": "B3", "downstream_id": "N3", "kind": "bypass",
             "is_bypass": True,
             "valve": {"id": "V_BP3_OUT", "name": "新旁路出口阀", "is_open": True,
                       "operable": True}},
            {"id": "EP3", "upstream_id": "N3", "downstream_id": "P3", "kind": "branch",
             "is_bypass": False,
             "valve": {"id": "V_P3", "name": "P3支路阀", "is_open": True, "operable": True}},
        ]
    )
    return content


# ---------------- 验收 1：复制样例 + 旁路 -> 新版本结果变化，旧样例不变 ----------------

def test_publish_bypass_draft_changes_result_but_v1_stays():
    _reset_seed()
    with TestClient(app) as client:
        # v1 基线结果
        v1 = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert v1["topology_version"] == "v1"
        assert v1["best_solution"] == ["V_TIN", "V_TOUT"]

        # 从 v1 复制草案
        draft = client.post("/api/drafts", json={"base_version_id": "v1", "name": "加旁路"}).json()
        topo = client.get("/api/topology", params={"topology_version": "v1"}).json()
        # 草案复制出的内容与 v1 结构一致（不包含锁）
        draft_detail = client.get(f"/api/drafts/{draft['id']}").json()
        assert len(draft_detail["content"]["nodes"]) == len(topo["nodes"]) == 9

        new_content = _add_second_bypass(draft_detail["content"])
        r = client.put(f"/api/drafts/{draft['id']}", json={"content": new_content})
        assert r.status_code == 200
        val = client.post(f"/api/drafts/{draft['id']}/validate").json()
        assert val["valid"] is True

        pub = client.post(f"/api/drafts/{draft['id']}/publish", json={}).json()
        assert pub["published"] is True
        v2 = pub["version_id"]
        assert v2 == "v2"

        # 新版本：最小隔离集从 2 阀变为 3 阀（必须额外切断新旁路的一侧）
        r2 = client.post(
            "/api/isolation",
            json={"target_id": "T", "topology_version": v2},
        ).json()
        assert r2["feasible"] is True
        assert r2["topology_version"] == v2
        assert len(r2["best_solution"]) == 3
        assert "V_TIN" in r2["best_solution"] and "V_TOUT" in r2["best_solution"]
        bp3 = {"V_BP3_IN", "V_BP3_OUT"}
        assert set(r2["best_solution"]) & bp3
        # 新增必要供给点 P3 在结果中出现且有保供路径
        assert "P3" in r2["essentials"]
        p3_path = r2["solutions"][0]["supply_paths"]["P3"]
        assert p3_path and p3_path[0] == "SRC" and p3_path[-1] == "P3"

        # 旧样例 v1 完全不变
        v1_again = client.post(
            "/api/isolation", json={"target_id": "T", "topology_version": "v1"}
        ).json()
        assert v1_again["best_solution"] == ["V_TIN", "V_TOUT"]
        assert "P3" not in v1_again["essentials"]
        topo_v1 = client.get("/api/topology", params={"topology_version": "v1"}).json()
        assert len(topo_v1["nodes"]) == 9
        assert all(vid not in {v["id"] for v in topo_v1["valves"]}
                   for vid in ("V_BP3_IN", "V_BP3_OUT", "V_P3"))

        # v1 在版本列表中标记为不可变
        versions = client.get("/api/versions").json()["versions"]
        by_id = {v["id"]: v for v in versions}
        assert by_id["v1"]["immutable"] is True
        assert by_id["v2"]["base_version_id"] == "v1"
        assert by_id["v1"]["base_version_id"] is None


# ---------------- 验收 2：重复阀门 / 孤立必要点草案被整体拒绝 ----------------

def test_duplicate_valve_draft_rejected_entirely():
    _reset_seed()
    with TestClient(app) as client:
        draft = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        content = client.get(f"/api/drafts/{draft['id']}").json()["content"]
        # 把 V1 同时绑到两条管段（E1 与 EL1）
        for s in content["segments"]:
            if s["id"] == "EL1" and s["valve"]:
                s["valve"]["id"] = "V1"
        client.put(f"/api/drafts/{draft['id']}", json={"content": content})

        before = {v["id"] for v in client.get("/api/versions").json()["versions"]}
        pub = client.post(f"/api/drafts/{draft['id']}/publish", json={}).json()
        assert pub["published"] is False
        codes = {e["code"] for e in pub["errors"]}
        assert "valve_bound_twice" in codes
        # 没有留下任何新版本（无半张图），草案仍保留
        after = {v["id"] for v in client.get("/api/versions").json()["versions"]}
        assert before == after
        assert client.get(f"/api/drafts/{draft['id']}").status_code == 200


def test_orphan_essential_draft_rejected_entirely():
    _reset_seed()
    with TestClient(app) as client:
        draft = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        content = client.get(f"/api/drafts/{draft['id']}").json()["content"]
        # 新增一个孤立的必要供给点
        content["nodes"].append(
            {"id": "PX", "name": "孤立必要点", "kind": "consumer", "x": 0, "y": 0,
             "essential": True}
        )
        client.put(f"/api/drafts/{draft['id']}", json={"content": content})

        before = {v["id"] for v in client.get("/api/versions").json()["versions"]}
        pub = client.post(f"/api/drafts/{draft['id']}/publish", json={}).json()
        assert pub["published"] is False
        assert "orphan_node" in {e["code"] for e in pub["errors"]}
        assert any(e["ref"] == "PX" for e in pub["errors"])
        after = {v["id"] for v in client.get("/api/versions").json()["versions"]}
        assert before == after


def test_missing_source_and_incomplete_edge_rejected():
    _reset_seed()
    with TestClient(app) as client:
        draft = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        content = client.get(f"/api/drafts/{draft['id']}").json()["content"]
        # 删掉全部来源节点 -> 缺失来源；同时残留引用 SRC 的边 -> 不完整边
        content["nodes"] = [n for n in content["nodes"] if n["id"] != "SRC"]
        client.put(f"/api/drafts/{draft['id']}", json={"content": content})
        pub = client.post(f"/api/drafts/{draft['id']}/publish", json={}).json()
        assert pub["published"] is False
        codes = {e["code"] for e in pub["errors"]}
        assert "missing_source" in codes
        assert "incomplete_edge" in codes


def test_duplicate_node_id_rejected():
    _reset_seed()
    with TestClient(app) as client:
        draft = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        content = client.get(f"/api/drafts/{draft['id']}").json()["content"]
        content["nodes"][1]["id"] = "SRC"  # 与来源重名
        client.put(f"/api/drafts/{draft['id']}", json={"content": content})
        pub = client.post(f"/api/drafts/{draft['id']}/publish", json={}).json()
        assert pub["published"] is False
        assert "duplicate_id" in {e["code"] for e in pub["errors"]}


# ---------------- 验收 3：同基线两份草案，后发布者得到版本冲突 ----------------

def test_two_drafts_same_baseline_second_gets_conflict():
    _reset_seed()
    with TestClient(app) as client:
        d1 = client.post("/api/drafts", json={"base_version_id": "v1", "name": "编辑者A"}).json()
        d2 = client.post("/api/drafts", json={"base_version_id": "v1", "name": "编辑者B"}).json()

        c1 = client.get(f"/api/drafts/{d1['id']}").json()["content"]
        c1 = _add_second_bypass(c1)
        client.put(f"/api/drafts/{d1['id']}", json={"content": c1})
        pub1 = client.post(f"/api/drafts/{d1['id']}/publish", json={"name": "A的版本"}).json()
        assert pub1["published"] is True
        assert pub1["version_id"] == "v2"

        # B 仍基于 v1（没有重新基线），即使内容合法也必须冲突，而不是覆盖 v2
        c2 = client.get(f"/api/drafts/{d2['id']}").json()["content"]
        c2["nodes"].append(
            {"id": "Q1", "name": "B的节点", "kind": "junction", "x": 0, "y": 0,
             "essential": False}
        )
        c2["segments"].append(
            {"id": "EQ1", "upstream_id": "N1", "downstream_id": "Q1", "kind": "branch",
             "is_bypass": False, "valve": None}
        )
        client.put(f"/api/drafts/{d2['id']}", json={"content": c2})
        resp = client.post(f"/api/drafts/{d2['id']}/publish", json={})
        assert resp.status_code == 409
        assert "修订冲突" in resp.json()["detail"]

        # v2 仍是 A 的内容，未被覆盖；v1 也未变
        versions = {v["id"]: v for v in client.get("/api/versions").json()["versions"]}
        assert set(versions) == {"v1", "v2"}
        assert versions["v2"]["node_count"] == 11  # A 的 9+2
        assert versions["v2"]["name"] == "A的版本"
        # B 的草案保留，可基于 v2 重新开
        assert client.get(f"/api/drafts/{d2['id']}").status_code == 200

        # B 重新基于最新版本后可以继续发布 v3
        d3 = client.post("/api/drafts", json={"base_version_id": "v2", "name": "编辑者A2"}).json()
        pub3 = client.post(f"/api/drafts/{d3['id']}/publish", json={}).json()
        assert pub3["published"] is True
        assert pub3["version_id"] == "v3"
        assert pub3["version"]["base_version_id"] == "v2"


# ---------------- 验收 4：切换/刷新/导入旧计算后，旧方案仍按旧拓扑解释 ----------------

def test_results_are_bound_to_version_and_survive_switch():
    _reset_seed()
    with TestClient(app) as client:
        # v1 计算并拿到记录 id
        old = client.post("/api/isolation", json={"target_id": "T"}).json()
        old_calc_id = old["calculation_id"]
        assert old["topology_version"] == "v1"

        # 发布 v2（加旁路）
        d = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        c = _add_second_bypass(client.get(f"/api/drafts/{d['id']}").json()["content"])
        client.put(f"/api/drafts/{d['id']}", json={"content": c})
        client.post(f"/api/drafts/{d['id']}/publish", json={})

        # 切换到 v2 后再计算：当前拓扑是新约束
        client.post("/api/current-version", json={"version_id": "v2"})
        cur = client.get("/api/topology").json()
        assert cur["topology_version"] == "v2"
        new = client.post("/api/isolation", json={"target_id": "T"}).json()
        assert new["topology_version"] == "v2"
        assert len(new["best_solution"]) == 3

        # 刷新后当前版本仍为 v2（服务端持久化）
        cur2 = client.get("/api/topology").json()
        assert cur2["topology_version"] == "v2"

        # 打开旧计算记录：仍标记 v1，结果是旧的 2 阀，绝不按 v2 重解释
        history = client.get("/api/calculations").json()["calculations"]
        versions_seen = {h["topology_version"] for h in history}
        assert versions_seen == {"v1", "v2"}
        old_rec = client.get(f"/api/calculations/{old_calc_id}").json()
        assert old_rec["topology_version"] == "v1"
        assert old_rec["version_name"]
        assert old_rec["best_solution"] == ["V_TIN", "V_TOUT"]
        assert "P3" not in old_rec["essentials"]


def test_locks_are_scoped_per_version():
    _reset_seed()
    with TestClient(app) as client:
        d = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        c = _add_second_bypass(client.get(f"/api/drafts/{d['id']}").json()["content"])
        client.put(f"/api/drafts/{d['id']}", json={"content": c})
        client.post(f"/api/drafts/{d['id']}/publish", json={})

        # 在 v2 锁定 V_TIN 不影响 v1
        client.post("/api/isolation", json={"target_id": "T", "topology_version": "v2",
                                            "locks": {"V_TIN": True}})
        v1_locks = {v["id"]: v["locked"]
                    for v in client.get("/api/topology",
                                        params={"topology_version": "v1"}).json()["valves"]}
        assert v1_locks["V_TIN"] is False
        v2_locks = {v["id"]: v["locked"]
                    for v in client.get("/api/topology",
                                        params={"topology_version": "v2"}).json()["valves"]}
        assert v2_locks["V_TIN"] is True

        # 计算记录里保存锁快照，且锁快照绑定版本
        rec = client.get("/api/calculations").json()["calculations"][0]
        assert rec["locks_snapshot"].get("V_TIN") is True
        assert rec["topology_version"] == "v2"

        # 重置 v1 不动 v2
        client.post("/api/reset", params={"topology_version": "v1"})
        v2_locks2 = {v["id"]: v["locked"]
                     for v in client.get("/api/topology",
                                         params={"topology_version": "v2"}).json()["valves"]}
        assert v2_locks2["V_TIN"] is True


def test_export_then_import_old_calculation_replays_with_old_topology():
    _reset_seed()
    with TestClient(app) as client:
        # v1：锁定出口阀 -> 无解 + 见证路径（样例 3），然后导出
        r = client.post(
            "/api/isolation",
            json={"target_id": "T", "locks": {"V_TOUT": True}},
        ).json()
        assert r["feasible"] is False
        assert "V_TOUT" in r["locked_witness_path"]["valves"]
        bundle = client.get("/api/versions/v1/export").json()
        calc = bundle["calculations"][0]
        assert calc["topology_version_id"] == "v1"
        assert calc["result"]["topology_version"] == "v1"

    # 全新库：先播种不可变 v1，再导入旧计算包（模拟“导入旧计算”）
    _reset_seed()
    with TestClient(app) as client:
        imp = client.post("/api/import", json=bundle).json()
        # v1 内容一致 -> 共享不可变版本；旧计算记录被导入
        assert calc["id"] in imp["imported_calculations"]

        rec = client.get(f"/api/calculations/{calc['id']}").json()
        assert rec["topology_version"] == "v1"
        assert rec["feasible"] is False
        assert "V_TOUT" in rec["locked_witness_path"]["valves"]
        # 旧方案用旧拓扑绘制：v1 仍无 P3/V_BP3
        topo = client.get("/api/versions/v1").json()
        assert len(topo["nodes"]) == 9


def test_import_rejects_clobbering_existing_version():
    _reset_seed()
    with TestClient(app) as client:
        # 发布 v2
        d = client.post("/api/drafts", json={"base_version_id": "v1"}).json()
        c = _add_second_bypass(client.get(f"/api/drafts/{d['id']}").json()["content"])
        client.put(f"/api/drafts/{d['id']}", json={"content": c})
        client.post(f"/api/drafts/{d['id']}/publish", json={})
        bundle = client.get("/api/versions/v2/export").json()

        # 篡改包中 v2 内容后再次导入 -> 拒绝，不覆盖
        bundle["versions"][0]["name"] = "被篡改的v2"
        bundle["versions"][0]["content"]["nodes"][0]["name"] = "恶意改名"
        resp = client.post("/api/import", json={"bundle": bundle})
        assert resp.status_code == 400
        name = {v["id"]: v["name"] for v in client.get("/api/versions").json()["versions"]}
        assert name["v2"] != "被篡改的v2"
