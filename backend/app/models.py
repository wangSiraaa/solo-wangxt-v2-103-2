"""数据库模型：节点（设备/供给点）、管段、阀门、版本化拓扑草案。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # source/equipment/consumer/junction
    x: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    y: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    essential: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Valve(Base):
    """阀门与其所在管段一一对应（demo 模型）。

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
    """有向管段：upstream -> downstream 表示介质名义流向。

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
    """已发布、不可变的拓扑版本。

    version_no=1 为固定演示样例（从种子数据发布），任何接口都不允许修改其
    snapshot；后续版本只能由通过校验的草案发布产生。snapshot 保存发布时刻
    的完整拓扑快照（节点/管段/阀门），旧版本内容不随后续编辑改变，因此旧的
    隔离计算永远按旧版本重绘与解释。
    """

    __tablename__ = "topo_versions"

    version_no: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_version_no: Mapped[int | None] = mapped_column(
        ForeignKey("topo_versions.version_no"), nullable=True
    )
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="trainer")
    note: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    immutable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class TopologyDraft(Base):
    """可编辑的版本化拓扑草案：从某已发布版本复制快照后自由编辑。

    只有 publish 时通过全部校验才会生成新版本；发布失败不会改动任何已发布
    版本。base_version_no 为草案基线；expected_base_version_no 为发布时
    客户端持有的修订基准，与当前最新版本不一致时判定为修订冲突。
    """

    __tablename__ = "topo_drafts"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    base_version_no: Mapped[int] = mapped_column(
        ForeignKey("topo_versions.version_no"), nullable=False
    )
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="trainer")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_version_no: Mapped[int | None] = mapped_column(
        ForeignKey("topo_versions.version_no"), nullable=True
    )


class VersionLock(Base):
    """按拓扑版本隔离的阀门锁定（禁止关闭）记录。

    version 1 的锁定同时镜像到旧 valves 表以兼容既有接口；其余版本只使用
    本表。隔离计算按请求所带版本从本表取锁定，绝不跨版本混用。
    """

    __tablename__ = "topo_locks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_no: Mapped[int] = mapped_column(
        ForeignKey("topo_versions.version_no"), nullable=False
    )
    valve_id: Mapped[str] = mapped_column(String(32), nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("version_no", "valve_id", name="uq_topo_lock_version_valve"),
    )


class IsolationRecord(Base):
    """每次隔离计算的历史记录，强绑定计算时使用的拓扑版本。

    记录保存版本号（以及版本名/不可变标记的冗余，便于历史展示）、计算输入、
    完整结果与锁定阀门；历史重绘使用 result.topo_version 指向的版本快照，
    新版本发布不会重解释旧记录。
    """

    __tablename__ = "isolation_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_no: Mapped[int] = mapped_column(
        ForeignKey("topo_versions.version_no"), nullable=False
    )
    version_name: Mapped[str] = mapped_column(String(128), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    locks_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="trainer")
