export interface TopoNode {
  id: string;
  name: string;
  kind: 'source' | 'equipment' | 'consumer' | 'junction';
  x: number;
  y: number;
  essential: boolean;
}

export interface SegmentValve {
  id: string;
  name: string;
  is_open: boolean;
  locked: boolean;
  operable: boolean;
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

/** 草案可编辑内容中的管段：端点/方向/旁路标记/阀门都可改。 */
export interface DraftSegment {
  id: string;
  source: string;
  target: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve: SegmentValve | null;
}

export interface DraftContent {
  nodes: TopoNode[];
  segments: DraftSegment[];
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

export interface TopoVersionMeta {
  version_no: number;
  name: string;
  base_version_no: number | null;
  created_by: string;
  note: string;
  immutable: boolean;
  created_at: string;
  content_hash: string;
}

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
  topo_version?: TopoVersionMeta | null;
}

export interface DraftSummary {
  id: string;
  name: string;
  base_version_no: number;
  created_by: string;
  created_at: string;
  updated_at: string;
  published_version_no: number | null;
}

export interface DraftDetail extends DraftSummary {
  content: DraftContent;
  validation: { valid: boolean; errors: string[] };
}

export interface HistoryRecord {
  id: number;
  version_no: number;
  version_name: string;
  target_id: string;
  locks: Record<string, boolean>;
  result: IsolationResult;
  created_at: string;
  created_by: string;
  topology?: Topology;
  topo_version?: TopoVersionMeta;
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

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  topo_version?: TopoVersionMeta | null;
  record_id?: number | null;
  locked_valves?: string[];
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
