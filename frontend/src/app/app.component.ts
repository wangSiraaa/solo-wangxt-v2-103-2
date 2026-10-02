import { Component, OnInit, inject, signal } from '@angular/core';
import { JsonPipe } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import {
  Bundle,
  CalcHistoryItem,
  Draft,
  IsolationResult,
  Topology,
  VersionInfo,
} from './models';
import { NetworkGraphComponent } from './network-graph.component';
import { VersionBadgeComponent } from './version-badge.component';
import { DraftEditorComponent } from './draft-editor.component';

type ViewMode = 'live' | 'history';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [
    NetworkGraphComponent,
    VersionBadgeComponent,
    FormsModule,
    JsonPipe,
    DraftEditorComponent,
  ],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);

  topology = signal<Topology | null>(null);
  result = signal<IsolationResult | null>(null);
  targetId = signal<string>('T');
  loading = signal(false);
  error = signal<string | null>(null);

  // 阀门锁定状态（仅前端选择，计算时随请求提交并由后端按版本持久化）
  locks = signal<Record<string, boolean>>({});

  // 版本与草案
  versions = signal<VersionInfo[]>([]);
  currentVersionId = signal<string>('v1');
  drafts = signal<Draft[]>([]);
  editingDraft = signal<Draft | null>(null);
  history = signal<CalcHistoryItem[]>([]);

  // live=当前版本交互视图；history=查看旧计算（按其绑定版本绘制）
  viewMode = signal<ViewMode>('live');
  viewingResult = signal<IsolationResult | null>(null);
  viewingTopology = signal<Topology | null>(null);

  ngOnInit(): void {
    this.refreshAll();
  }

  // ---------------- 数据装载 ----------------

  refreshAll(): void {
    this.loading.set(true);
    this.api.versions().subscribe({
      next: (vl) => {
        this.versions.set(vl.versions);
        this.currentVersionId.set(vl.current_version);
        this.loadTopologyAndHistory(vl.current_version);
      },
      error: (e) => this.fail(e),
    });
  }

  private loadTopologyAndHistory(versionId: string): void {
    this.api.topology(versionId).subscribe({
      next: (t) => {
        this.topology.set(t);
        const l: Record<string, boolean> = {};
        for (const v of t.valves) {
          l[v.id] = v.locked;
        }
        this.locks.set(l);
        this.loading.set(false);
      },
      error: (e) => this.fail(e),
    });
    this.api.history().subscribe({ next: (h) => this.history.set(h.calculations) });
    this.api.drafts().subscribe({ next: (d) => this.drafts.set(d.drafts) });
  }

  private fail(e: unknown): void {
    const msg = (e as { error?: { detail?: string }; message?: string }).error?.detail
      ?? (e as { message?: string }).message
      ?? String(e);
    this.error.set(`操作失败：${msg}`);
    this.loading.set(false);
  }

  currentVersion(): VersionInfo | undefined {
    return this.versions().find((v) => v.id === this.currentVersionId());
  }

  versionName(id: string): string {
    return this.versions().find((v) => v.id === id)?.name ?? id;
  }

  // ---------------- 版本切换 ----------------

  switchVersion(versionId: string): void {
    this.error.set(null);
    this.loading.set(true);
    this.api.switchVersion(versionId).subscribe({
      next: () => {
        this.currentVersionId.set(versionId);
        this.viewMode.set('live');
        this.result.set(null);
        this.loadTopologyAndHistory(versionId);
      },
      error: (e) => this.fail(e),
    });
  }

  // ---------------- 草案 ----------------

  createDraft(): void {
    this.error.set(null);
    this.api.createDraft(this.currentVersionId()).subscribe({
      next: (d) => {
        this.drafts.update((list) => [d, ...list]);
        this.editingDraft.set(d);
      },
      error: (e) => this.fail(e),
    });
  }

  editDraft(draftId: string): void {
    this.api.getDraft(draftId).subscribe({
      next: (d) => this.editingDraft.set(d),
      error: (e) => this.fail(e),
    });
  }

  deleteDraft(draftId: string): void {
    this.api.deleteDraft(draftId).subscribe({
      next: () => {
        this.drafts.update((list) => list.filter((d) => d.id !== draftId));
        if (this.editingDraft()?.id === draftId) {
          this.editingDraft.set(null);
        }
      },
      error: (e) => this.fail(e),
    });
  }

  onDraftPublished(versionId: string): void {
    this.editingDraft.set(null);
    this.error.set(null);
    this.refreshAll();
    // 自动切到新版本并用新约束计算
    this.api.switchVersion(versionId).subscribe({
      next: () => {
        this.currentVersionId.set(versionId);
        this.loadTopologyAndHistory(versionId);
      },
      error: (e) => this.fail(e),
    });
  }

  // ---------------- 锁阀与计算（绑定当前版本） ----------------

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    this.viewMode.set('live');
    this.loading.set(true);
    this.error.set(null);
    this.api.isolate(this.targetId(), this.locks(), this.currentVersionId()).subscribe({
      next: (r) => {
        this.result.set(r);
        this.viewingResult.set(r);
        this.viewingTopology.set(this.topology());
        // 同步锁定勾选（后端为权威来源，且按版本持久化）
        this.locks.update((l) => {
          const next = { ...l };
          for (const v of r.candidate_valves) {
            next[v.id] = v.locked;
          }
          return next;
        });
        this.api.history().subscribe({ next: (h) => this.history.set(h.calculations) });
        this.loading.set(false);
      },
      error: (e) => this.fail(e),
    });
  }

  loadSample(sample: 1 | 2 | 3): void {
    const buildLocks = (): Record<string, boolean> => {
      const l: Record<string, boolean> = {};
      for (const v of this.topology()?.valves ?? []) {
        l[v.id] = false;
      }
      // 三个固定样例针对 v1 的阀门命名；其他版本仅清空锁定
      if (this.currentVersionId() === 'v1') {
        if (sample === 2) {
          l['V_TIN'] = true;
        } else if (sample === 3) {
          l['V_TOUT'] = true;
        }
      }
      return l;
    };

    // 样例 1 需要先重置 v1 锁阀，再按无锁计算；其余样例直接带锁计算。
    if (this.currentVersionId() === 'v1' && sample === 1) {
      this.loading.set(true);
      this.api.reset('v1').subscribe({
        next: () => {
          this.api.topology('v1').subscribe({
            next: (t) => {
              this.topology.set(t);
              this.locks.set(buildLocks());
              this.compute();
            },
            error: (e) => this.fail(e),
          });
        },
        error: (e) => this.fail(e),
      });
    } else {
      this.locks.set(buildLocks());
      this.compute();
    }
  }

  resetAll(): void {
    this.api.reset(this.currentVersionId()).subscribe({
      next: () => {
        this.result.set(null);
        this.viewMode.set('live');
        this.loadTopologyAndHistory(this.currentVersionId());
      },
      error: (e) => this.fail(e),
    });
  }

  // ---------------- 历史（旧结果按旧版本绘制） ----------------

  openHistoryItem(item: CalcHistoryItem): void {
    this.loading.set(true);
    this.api.getCalculation(item.id).subscribe({
      next: (r) => {
        // 关键：按记录绑定的拓扑版本取图，而不是当前版本
        this.api.topology(r.topology_version).subscribe({
          next: (t) => {
            this.viewingResult.set(r);
            this.viewingTopology.set(t);
            this.result.set(r);
            this.viewMode.set('history');
            this.loading.set(false);
          },
          error: (e) => this.fail(e),
        });
      },
      error: (e) => this.fail(e),
    });
  }

  backToLive(): void {
    this.viewMode.set('live');
    this.viewingResult.set(this.result());
    this.viewingTopology.set(this.topology());
  }

  // ---------------- 导入导出（保留版本关系） ----------------

  exportVersion(versionId: string): void {
    this.api.exportVersion(versionId).subscribe({
      next: (bundle) => {
        const blob = new Blob([JSON.stringify(bundle, null, 2)], {
          type: 'application/json',
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `topology-${versionId}.bundle.json`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: (e) => this.fail(e),
    });
  }

  onImportFile(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const bundle = JSON.parse(String(reader.result)) as Bundle;
        this.api.importBundle(bundle).subscribe({
          next: () => {
            input.value = '';
            this.refreshAll();
          },
          error: (e) => this.fail(e),
        });
      } catch {
        this.error.set('导入失败：文件不是合法 JSON');
      }
    };
    reader.readAsText(file);
  }

  // ---------------- 视图辅助 ----------------

  valveName(id: string): string {
    return this.displayTopology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  displayTopology(): Topology | null {
    return this.viewMode() === 'history' ? this.viewingTopology() : this.topology();
  }

  displayResult(): IsolationResult | null {
    return this.viewMode() === 'history' ? this.viewingResult() : this.result();
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }
}
