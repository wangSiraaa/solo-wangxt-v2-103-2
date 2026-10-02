"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from .models import SEED_VERSION_ID


class ValveLockIn(BaseModel):
    locked: bool = Field(..., description="True=锁定该阀门（保持现状不可操作），False=解锁")
    topology_version: str | None = Field(
        None, description="在指定拓扑版本上加锁；缺省为当前选中版本（v1 写原表）"
    )


class IsolationIn(BaseModel):
    target_id: str = Field("T", description="待隔离目标设备节点 id")
    locks: dict[str, bool] | None = Field(
        None, description="可选：一次性提交的阀门锁定状态 {valve_id: locked}"
    )
    topology_version: str | None = Field(
        None, description="在哪个拓扑版本上计算；缺省为当前选中版本"
    )


class NodeOut(BaseModel):
    id: str
    name: str
    kind: str
    x: float
    y: float
    essential: bool


class ValveOut(BaseModel):
    id: str
    name: str
    segment_id: str
    endpoints: list[str]
    is_open: bool
    locked: bool
    operable: bool
    is_bypass: bool


class SegmentOut(BaseModel):
    id: str
    source: str
    target: str
    direction: str
    kind: str
    is_bypass: bool
    valve_id: str | None


class TopologyOut(BaseModel):
    topology_version: str = SEED_VERSION_ID
    version_name: str | None = None
    base_version_id: str | None = None
    immutable: bool = False
    nodes: list[NodeOut]
    segments: list[SegmentOut]
    valves: list[ValveOut]


class IsolationSolution(BaseModel):
    close_valves: list[str]
    size: int
    alternative_rank: int
    closes_bypass_valves: list[str] = []
    supply_paths: dict[str, list[str] | None] = {}


class ResidualPath(BaseModel):
    nodes: list[str]
    valves: list[str | None]
    locked_valves_on_path: list[str]
    uses_bypass: bool


class UnconstrainedBest(BaseModel):
    close_valves: list[str]
    size: int
    disconnects_essentials: list[str]
    unavoidable_essentials: list[str] = []


class IsolationOut(BaseModel):
    feasible: bool
    target_id: str
    topology_version: str = SEED_VERSION_ID
    version_name: str | None = None
    calculation_id: str | None = None
    sources: list[str]
    essentials: list[str]
    candidate_valves: list[ValveOut]
    examined_combinations: int = 0
    solutions: list[dict[str, Any]] = []
    best_solution: list[str] = []
    residual_path: dict[str, Any] | None = None
    locked_witness_path: dict[str, Any] | None = None
    infeasible_reason: str | None = None
    unconstrained_best: dict[str, Any] | None = None


# ---------------- 版本化拓扑 ----------------

class VersionSummaryOut(BaseModel):
    id: str
    name: str
    note: str | None = None
    base_version_id: str | None = None
    immutable: bool = False
    created_at: str | None = None
    node_count: int
    segment_count: int
    valve_count: int


class VersionListOut(BaseModel):
    current_version: str
    versions: list[VersionSummaryOut]


class DraftCreateIn(BaseModel):
    base_version_id: str = Field(..., description="从哪个已发布版本复制")
    name: str | None = None


class DraftContentIn(BaseModel):
    content: dict[str, Any] = Field(..., description="草案完整拓扑快照（允许暂不合法）")


class DraftPublishIn(BaseModel):
    name: str | None = None


class ValidationErrorItem(BaseModel):
    code: str
    message: str
    ref: str | None = None


class DraftValidationOut(BaseModel):
    valid: bool
    errors: list[ValidationErrorItem]


class DraftOut(BaseModel):
    id: str
    name: str
    base_version_id: str
    content: dict[str, Any]
    validation_errors: list[ValidationErrorItem]
    created_at: str | None = None
    updated_at: str | None = None


class PublishOut(BaseModel):
    published: bool
    errors: list[ValidationErrorItem] = []
    version_id: str | None = None
    version: VersionSummaryOut | None = None


class SwitchVersionIn(BaseModel):
    version_id: str


class ImportBundleIn(BaseModel):
    bundle: dict[str, Any]
