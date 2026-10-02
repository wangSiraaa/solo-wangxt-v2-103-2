import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  Bundle,
  CalcHistoryItem,
  Draft,
  IsolationResult,
  PublishResult,
  Topology,
  VersionList,
} from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(versionId?: string): Observable<Topology> {
    const q = versionId ? `?topology_version=${encodeURIComponent(versionId)}` : '';
    return this.http.get<Topology>(`/api/topology${q}`);
  }

  versions(): Observable<VersionList> {
    return this.http.get<VersionList>('/api/versions');
  }

  switchVersion(versionId: string): Observable<{ current_version: string }> {
    return this.http.post<{ current_version: string }>('/api/current-version', {
      version_id: versionId,
    });
  }

  setLock(valveId: string, locked: boolean, versionId?: string): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, {
      locked,
      topology_version: versionId ?? null,
    });
  }

  reset(versionId?: string): Observable<unknown> {
    const q = versionId ? `?topology_version=${encodeURIComponent(versionId)}` : '';
    return this.http.post(`/api/reset${q}`, {});
  }

  isolate(
    targetId: string,
    locks?: Record<string, boolean>,
    versionId?: string,
  ): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
      topology_version: versionId ?? null,
    });
  }

  // ---------------- 草案 ----------------

  drafts(): Observable<{ drafts: Draft[] }> {
    return this.http.get<{ drafts: Draft[] }>('/api/drafts');
  }

  createDraft(baseVersionId: string, name?: string): Observable<Draft> {
    return this.http.post<Draft>('/api/drafts', {
      base_version_id: baseVersionId,
      name: name ?? null,
    });
  }

  getDraft(draftId: string): Observable<Draft> {
    return this.http.get<Draft>(`/api/drafts/${draftId}`);
  }

  updateDraft(draftId: string, content: Draft['content']): Observable<Draft> {
    return this.http.put<Draft>(`/api/drafts/${draftId}`, { content });
  }

  deleteDraft(draftId: string): Observable<unknown> {
    return this.http.delete(`/api/drafts/${draftId}`);
  }

  validateDraft(draftId: string): Observable<{ valid: boolean; errors: Draft['validation_errors'] }> {
    return this.http.post<{ valid: boolean; errors: Draft['validation_errors'] }>(
      `/api/drafts/${draftId}/validate`,
      {},
    );
  }

  publishDraft(draftId: string, name?: string): Observable<PublishResult> {
    return this.http.post<PublishResult>(`/api/drafts/${draftId}/publish`, {
      name: name ?? null,
    });
  }

  // ---------------- 历史与导入导出 ----------------

  history(): Observable<{ calculations: CalcHistoryItem[] }> {
    return this.http.get<{ calculations: CalcHistoryItem[] }>('/api/calculations');
  }

  getCalculation(calcId: string): Observable<IsolationResult> {
    return this.http.get<IsolationResult>(`/api/calculations/${calcId}`);
  }

  exportVersion(versionId: string, includeCalculations = true): Observable<Bundle> {
    return this.http.get<Bundle>(
      `/api/versions/${encodeURIComponent(versionId)}/export?include_calculations=${includeCalculations}`,
    );
  }

  importBundle(bundle: Bundle): Observable<{ imported_versions: string[]; imported_calculations: string[] }> {
    return this.http.post<{ imported_versions: string[]; imported_calculations: string[] }>(
      '/api/import',
      bundle,
    );
  }
}
