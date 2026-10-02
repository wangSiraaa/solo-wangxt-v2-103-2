# 管网隔离方案培训演示（Isolation Training Demo）

面向工艺培训的**纯演示系统**：在一张固定的模拟管网上，计算隔离目标设备所需关闭的
阀门候选集合。**不连接任何真实控制系统，计算结果不代表真实检修已满足安全隔离条件。**

- 前端：Angular 19 + Cytoscape.js（拓扑、管段方向、旁路、阀门锁定、方案与残余路径高亮）
- 后端：FastAPI + NetworkX（无向物理连通图上的候选阀门集合枚举与约束校验）
- 存储：PostgreSQL（节点连接、阀门开闭/锁定、必要供给点；本地无 PG 时自动回退 SQLite）

## 版本化拓扑草案

固定样例以不可变版本 **v1** 永久保留；培训时可从任意已发布版本**复制出草案**，
编辑节点、管段方向、阀门（与管段 1:1）、旁路标记、节点种类（来源/目标设备/
必要供给点）与坐标，再**校验发布**为新版本（v2、v3……线性修订链）。

- **不可变已发布版本**：发布即生成 JSON 快照，之后不再修改；v1 的三张原表也
  仅保留原有锁定语义，结构永不改变。
- **发布前强校验**（失败整体拒绝、不留半张图，草案保留以便修改）：
  同一阀门绑定多条管段（`valve_bound_twice`）、孤立目标设备/必要供给点/来源
  （`orphan_node`）、缺失来源（`missing_source`）、重复标识（`duplicate_id`）、
  不完整边定义（`incomplete_edge`：缺端点/端点不存在/自环/平行边）及字段非法。
- **修订冲突**：同一基线版本只能发布出一个后继；两个编辑者基于同一版本发布时，
  后一份得到 **409 修订冲突**，不会覆盖先发布者，需基于最新版本重开草案。
- **版本绑定**：每次计算、锁阀记录（`LockRecord`）、无解见证路径都归档为
  `CalculationRecord`，带 `topology_version` 与当时的 `locks_snapshot`。
  历史/刷新/导入旧记录时按其绑定版本取回拓扑绘制与解释，新版本不重解释旧结果。
- **导入导出**：导出 bundle 保留版本链（`base_version_id`）与计算→版本关系；
  导入为原子事务，内容一致的不可变版本（v1）可共享，任何差异或分叉整体拒绝。

## 演示拓扑

```
SRC ─V0─ N1 ─V1─ N2 ─V_TIN─ [T] ─V_TOUT─ N3
          │            ╲  旁路 N2─V_BP_IN─BP─V_BP_OUT─N3 ╱
         V_P1           环网联络 N1─V_LK1─N5─V_LK2─N3
          ↓                          N3 ─V_P2─ P2
          P1
```

- 每条管段显式保存名义方向（upstream→downstream，图上箭头标注）；隔离按**无向物理连通**计算。
- 旁路（紫虚线）与目标设备 T 并联；P1、P2 为**必要供给点**（任何方案不得断供）。
- 阀门与管段 1:1；初始全部打开。锁定 = 禁止关闭（保持现状），可在页面勾选后重新计算。

## 三个培训样例

| 样例 | 锁定 | 结果 |
| --- | --- | --- |
| 1 旁路绕回 | 无 | 关 `V_TIN`+`V_TOUT` 即隔离 T；旁路保持打开，P2 经 `N2→BP→N3` 绕回不断供 |
| 2 锁定入口阀 | `V_TIN` | 最小集合升为 3 阀：`V_TOUT`+`V1`+旁路一只（`V_BP_IN`/`V_BP_OUT` 两个等价方案）；旁路被封，P2 改由环网 `N1→N5→N3` 供料 |
| 3 不应断供的支路 | `V_TOUT` | **无可行方案**：任何切法都不可避免断供 P2（P1 可保住）；页面显示仍连通的残余路径 `SRC→N1→N2→T`，以及经锁定阀的见证路径 `…→N3→T`（含 `V_TOUT`） |

> 仅关 T 一侧阀门时隔离不成立：例如只关 `V_TIN`，介质仍可经旁路绕回 N3 再回到 T
> （`N2→BP→N3→T`），或经环网绕回。这正是样例 1 必须两侧同关的原因。

## 本地运行

### 后端（无 PostgreSQL 时自动用 SQLite 文件）

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
# API: http://127.0.0.1:8000/api/topology, /api/isolation, /api/valves/{id}/lock, /api/reset
# 测试: python3 -m pytest tests/ -q
```

指定 PostgreSQL：

```bash
export DATABASE_URL=postgresql+psycopg://isolation:isolation@localhost:5432/isolation_demo
docker compose up -d db        # 或使用任意已有 PG 实例
```

### 前端

```bash
cd frontend
npm install
npm start                      # http://localhost:4200 （/api 代理到 8000）
# 或产物构建后由后端直接托管: npx ng build  → http://127.0.0.1:8000/
```

### 一键（含 PG）

```bash
cd frontend && npm ci && npx ng build && cd ..
docker compose up --build
```

## API 摘要

- `GET /api/topology`：节点 / 有向管段 / 阀门（含 `is_open`、`locked`、`is_bypass`、
  `topology_version`）；可带 `?topology_version=v2` 取指定版本
- `POST /api/isolation`：body
  `{ "target_id": "T", "locks": {"V_TIN": true}, "topology_version": "v2" }`
  - 可行：`best_solution`（最少阀门）、等价方案、方案后每个必要供给点的来源路径
  - 不可行：`residual_path`（仍连通的一条残余路径）、`locked_witness_path`（经锁定阀的见证路径）、
    `unconstrained_best.unavoidable_essentials`（任何切法都无法保住的供给点）
  - 返回与归档均带 `topology_version`、`calculation_id`
- `POST /api/valves/{id}/lock`：持久化单只阀门锁定（可带 `topology_version`，按版本隔离）
- `POST /api/reset`：恢复指定版本阀门打开、未锁定（v1 为原样例语义）
- `GET /api/versions` / `POST /api/current-version`：版本列表与当前版本切换
- `POST /api/drafts`、`GET|PUT|DELETE /api/drafts/{id}`、
  `POST /api/drafts/{id}/validate`、`POST /api/drafts/{id}/publish`
  （校验失败返回 `published:false` + 错误列表；修订冲突返回 409）
- `GET /api/calculations` / `GET /api/calculations/{id}`：计算历史与旧记录（按绑定版本解释）
- `GET /api/versions/{id}/export` / `POST /api/import`：版本+计算关系的导入导出

## 安全边界声明

系统仅对**给定拓扑与阀门模型**做枚举演示：假设阀门与管段 1:1、关阀即断边、
无背压/泄漏/盲板/双阀双断等真实工况要素。任何输出**不得**作为真实检修隔离
（LOTO）的安全依据。
