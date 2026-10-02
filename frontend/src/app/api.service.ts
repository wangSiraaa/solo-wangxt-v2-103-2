import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  DraftDetail,
  DraftSummary,
  HistoryRecord,
  IsolationResult,
  TopoVersionMeta,
  Topology,
} from './models';

export interface PublishResult {
  published: TopoVersionMeta;
  draft_id: string;
}

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  // -------- 拓扑（版本化） --------

  topology(versionNo?: number): Observable<Topology> {
    if (versionNo !== undefined) {
      return this.http.get<Topology>(`/api/versions/${versionNo}/topology`);
    }
    return this.http.get<Topology>('/api/topology');
  }

  versions(): Observable<TopoVersionMeta[]> {
    return this.http.get<TopoVersionMeta[]>('/api/versions');
  }

  versionLocks(versionNo: number): Observable<{ version_no: number; locks: Record<string, boolean> }> {
    return this.http.get<{ version_no: number; locks: Record<string, boolean> }>(
      `/api/versions/${versionNo}/locks`,
    );
  }

  putVersionLocks(versionNo: number, locks: Record<string, boolean>) {
    return this.http.put<{ version_no: number; locks: Record<string, boolean> }>(
      `/api/versions/${versionNo}/locks`,
      { locks },
    );
  }

  resetVersion(versionNo: number) {
    return this.http.post<{ status: string }>(`/api/versions/${versionNo}/reset`, {});
  }

  // -------- 草案 --------

  drafts(): Observable<DraftSummary[]> {
    return this.http.get<DraftSummary[]>('/api/drafts');
  }

  createDraft(baseVersionNo: number, name?: string): Observable<DraftDetail> {
    return this.http.post<DraftDetail>(`/api/versions/${baseVersionNo}/drafts`, {
      name: name ?? '',
    });
  }

  draft(id: string): Observable<DraftDetail> {
    return this.http.get<DraftDetail>(`/api/drafts/${id}`);
  }

  saveDraft(id: string, body: { name?: string; content?: unknown }): Observable<DraftDetail> {
    return this.http.put<DraftDetail>(`/api/drafts/${id}`, body);
  }

  deleteDraft(id: string) {
    return this.http.delete<{ status: string }>(`/api/drafts/${id}`);
  }

  validateDraft(id: string): Observable<{ valid: boolean; errors: string[] }> {
    return this.http.get<{ valid: boolean; errors: string[] }>(`/api/drafts/${id}/validate`);
  }

  publishDraft(
    id: string,
    expectedBaseVersionNo: number,
    note?: string,
  ): Observable<PublishResult> {
    return this.http.post<PublishResult>(`/api/drafts/${id}/publish`, {
      expected_base_version_no: expectedBaseVersionNo,
      note: note ?? '',
    });
  }

  // -------- 计算 / 历史 / 导入导出 --------

  isolate(
    targetId: string,
    locks?: Record<string, boolean>,
    topoVersion?: number,
  ): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
      topo_version: topoVersion ?? null,
    });
  }

  history(versionNo?: number): Observable<HistoryRecord[]> {
    const qs = versionNo !== undefined ? `?version_no=${versionNo}` : '';
    return this.http.get<HistoryRecord[]>(`/api/history${qs}`);
  }

  historyRecord(id: number): Observable<HistoryRecord> {
    return this.http.get<HistoryRecord>(`/api/history/${id}`);
  }

  exportBundle(): Observable<unknown> {
    return this.http.get('/api/export');
  }

  importBundle(bundle: unknown) {
    return this.http.post<{
      versions_added: number;
      versions_reused: number;
      records_imported: number;
      version_no_map: Record<string, number>;
    }>('/api/import', bundle);
  }

  // -------- v1 兼容旧接口（样例按钮仍可用） --------

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  reset(): Observable<unknown> {
    return this.http.post('/api/reset', {});
  }
}
