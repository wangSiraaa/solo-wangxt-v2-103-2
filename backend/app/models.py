"""数据库模型：版本化拓扑、草案、锁阀记录、计算记录。

版本模型说明：
- v1 演示样例的节点/管段/阀门仍使用 Node/Segment/Valve 三张原表，
  其结构内容**永久不可变**（现有培训样例与旧测试继续依赖它们）；
  Valve.locked/is_open 仍是 v1 的可变锁阀状态。
- v2+ 版本的完整拓扑以 JSON 快照存于 TopologyVersion.content，
  发布即不可变；草案 TopologyDraft.content 可任意编辑（允许处于
  校验不通过的中间状态），只有通过校验才能发布。
- v2+ 的锁阀状态存于 LockRecord（与版本绑定）；v1 仍写 Valve 原表。
- 每次隔离计算生成 CalculationRecord，绑定 topology_version 与当时
  使用的锁阀快照，保证旧结果永远按旧拓扑解释。
"""
from __future__ import annotations

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# 固定演示样例的版本标识：作为版本链根节点，永不重新发布。
SEED_VERSION_ID = "v1"
SEED_VERSION_NAME = "演示样例（固定不可变）"


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source/equipment/consumer/junction
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Valve(Base):
    """v1 演示样例阀门。阀门与其所在管段一一对应（demo 模型）。

    is_open:       阀门当前实际开闭状态；锁定后用户不可再改变其操作状态。
    locked:        用户锁定标记，锁定阀门不参与候选关闭集合。
                   初始状态全部为打开、未锁定；当前演示中可操作的动作是“关闭”，
                   因此锁定等价于“禁止关闭”（保持打开）。
    operable:      工艺上是否允许操作（个别阀门检修/铅封，恒不可用）。
    """

    __tablename__ = "valves"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    segment_id: Mapped[str] = mapped_column(ForeignKey("segments.id"), nullable=False)
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    operable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    segment: Mapped["Segment"] = relationship(back_populates="valve")


class Segment(Base):
    """v1 演示样例有向管段：upstream -> downstream 表示介质名义流向。

    隔离计算按“物理连通”使用无向图（介质可被两侧隔离边界切断，
    且检修隔离关注连通性而非流向）；direction 用于前端箭头标注、
    来源/下游识别与结果解释。旁路是与主管并联的一对管段。
    """

    __tablename__ = "segments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    upstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    downstream_id: Mapped[str] = mapped_column(ForeignKey("nodes.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="main")  # main/bypass/branch
    is_bypass: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    valve: Mapped["Valve | None"] = relationship(back_populates="segment", uselist=False)

    __table_args__ = (UniqueConstraint("upstream_id", "downstream_id", name="uq_segment_endpoints"),)


class TopologyVersion(Base):
    """已发布、不可变的拓扑版本快照。

    content 结构:
      {"nodes":    [{"id","name","kind","x","y","essential"}],
       "segments": [{"id","upstream_id","downstream_id","kind","is_bypass",
                      "valve": {"id","name","is_open","locked","operable"} | null}]}
    base_version_id 为链状修订来源；线性链（一个版本至多一个已发布后继）
    由唯一约束 uq_published_base 保证（v1 的 base 为 NULL，
    SQLite/PG 中 NULL 不参与唯一性比较，故根版本可共存），
    冲突时产生修订冲突 409。
    v1 行的 base_version_id 为 NULL，content 为发布时的样例快照（不可变）。
    """

    __tablename__ = "topology_versions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_version_id: Mapped[str | None] = mapped_column(
        ForeignKey("topology_versions.id"), nullable=True
    )
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("base_version_id", name="uq_published_base"),
    )


class TopologyDraft(Base):
    """可编辑拓扑草案：从某版本复制而来，发布前不影响任何计算。"""

    __tablename__ = "topology_drafts"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_version_id: Mapped[str] = mapped_column(
        ForeignKey("topology_versions.id"), nullable=False
    )
    content: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class LockRecord(Base):
    """v2+ 版本下的锁阀记录（version_id, valve_id）唯一。

    仅记录“锁定/保持现状”的阀门；未出现在表中的阀门视为未锁定。
    v1 版本不使用本表（继续写 Valve.locked），避免改动既有样例行为。
    """

    __tablename__ = "lock_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(
        ForeignKey("topology_versions.id"), nullable=False
    )
    valve_id: Mapped[str] = mapped_column(String(32), nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("version_id", "valve_id", name="uq_lock_version_valve"),
    )


class CalculationRecord(Base):
    """一次隔离计算的不可变归档：结果、锁阀快照都绑定拓扑版本。

    locks_snapshot 保存本次计算实际生效的 {valve_id: locked}，
    即使之后有人改了该版本的锁阀状态，旧记录仍按旧锁解释。
    """

    __tablename__ = "calculation_records"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    topology_version_id: Mapped[str] = mapped_column(
        ForeignKey("topology_versions.id"), nullable=False
    )
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    locks_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    result: Mapped[dict] = mapped_column(JSON, nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[str] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AppState(Base):
    """单行键值表：当前界面选用的拓扑版本等全局状态。"""

    __tablename__ = "app_state"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
