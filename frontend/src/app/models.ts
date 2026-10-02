export interface TopoNode {
  id: string;
  name: string;
  kind: 'source' | 'equipment' | 'consumer' | 'junction';
  x: number;
  y: number;
  essential: boolean;
}

export interface Segment {
  id: string;
  source: string;
  target: string;
  direction: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve_id: string | null;
}

export interface Valve {
  id: string;
  name: string;
  segment_id: string;
  endpoints: string[];
  is_open: boolean;
  locked: boolean;
  operable: boolean;
  is_bypass: boolean;
}

export interface VersionInfo {
  id: string;
  name: string;
  note?: string | null;
  base_version_id?: string | null;
  immutable: boolean;
  created_at?: string | null;
  node_count: number;
  segment_count: number;
  valve_count: number;
}

export interface VersionList {
  current_version: string;
  versions: VersionInfo[];
}

/** 版本/草案使用的可编辑快照格式（valve 内嵌于 segment）。 */
export interface DraftValve {
  id: string;
  name: string;
  is_open: boolean;
  operable: boolean;
}

export interface DraftSegment {
  id: string;
  upstream_id: string;
  downstream_id: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve: DraftValve | null;
}

export interface DraftContent {
  nodes: TopoNode[];
  segments: DraftSegment[];
}

export interface ValidationErrorItem {
  code: string;
  message: string;
  ref?: string | null;
}

export interface Draft {
  id: string;
  name: string;
  base_version_id: string;
  content: DraftContent;
  validation_errors: ValidationErrorItem[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface PublishResult {
  published: boolean;
  errors: ValidationErrorItem[];
  version_id?: string | null;
  version?: VersionInfo | null;
}

export interface Topology {
  topology_version: string;
  version_name?: string | null;
  base_version_id?: string | null;
  immutable?: boolean;
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
}

export interface Solution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface CalcHistoryItem {
  id: string;
  topology_version: string;
  version_name?: string | null;
  target_id: string;
  feasible: boolean;
  best_solution: string[];
  locks_snapshot: Record<string, boolean>;
  created_at?: string | null;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  topology_version: string;
  version_name?: string | null;
  calculation_id?: string | null;
  locks_snapshot?: Record<string, boolean>;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
  examined_combinations: number;
  solutions: Solution[];
  best_solution: string[];
  residual_path: ResidualPath | null;
  locked_witness_path: ResidualPath | null;
  infeasible_reason: string | null;
  unconstrained_best: {
    close_valves: string[];
    size: number;
    disconnects_essentials: string[];
    unavoidable_essentials: string[];
  } | null;
}

/** 导入导出包（关系：versions.base_version_id、calculations.topology_version_id）。 */
export interface Bundle {
  format: 'isolation-topology-bundle/1';
  exported_at?: string;
  versions: Array<{
    id: string;
    name: string;
    note?: string | null;
    base_version_id?: string | null;
    content: DraftContent;
  }>;
  calculations?: Array<{
    id: string;
    topology_version_id: string;
    target_id: string;
    locks_snapshot: Record<string, boolean>;
    result: IsolationResult;
    feasible: boolean;
  }>;
}
