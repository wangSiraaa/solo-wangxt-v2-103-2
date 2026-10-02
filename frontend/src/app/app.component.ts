import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import {
  DraftDetail,
  DraftSummary,
  HistoryRecord,
  IsolationResult,
  TopoVersionMeta,
  Topology,
} from './models';
import { NetworkGraphComponent } from './network-graph.component';
import { DraftEditorComponent } from './draft-editor.component';

const VIEW = {
  CURRENT: 'current',
  DRAFT: 'draft',
  HISTORY: 'history',
} as const;

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent, DraftEditorComponent, FormsModule],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);

  // 已发布版本与当前选择
  versions = signal<TopoVersionMeta[]>([]);
  currentVersionNo = signal<number>(1);
  currentVersion = computed(
    () => this.versions().find((v) => v.version_no === this.currentVersionNo()) ?? null,
  );

  topology = signal<Topology | null>(null);
  result = signal<IsolationResult | null>(null);
  targetId = signal<string>('T');
  loading = signal(false);
  error = signal<string | null>(null);
  info = signal<string | null>(null);

  // 当前版本下的阀门锁定
  locks = signal<Record<string, boolean>>({});

  // 草案
  drafts = signal<DraftSummary[]>([]);
  editingDraft = signal<DraftDetail | null>(null);

  // 历史回看：查看旧记录时使用其绑定版本的拓扑绘图
  history = signal<HistoryRecord[]>([]);
  viewingRecord = signal<HistoryRecord | null>(null);

  view = signal<string>(VIEW.CURRENT);

  private static readonly VERSION_KEY = 'iso.selectedTopoVersion';

  ngOnInit(): void {
    const saved = Number(localStorage.getItem(AppComponent.VERSION_KEY));
    this.loadVersionsAndTopology(Number.isInteger(saved) && saved > 0 ? saved : 1);
    this.refreshDrafts();
    this.loadHistory();
  }

  // ------------------------------------------------ 版本 / 拓扑

  loadVersionsAndTopology(versionNo: number) {
    this.loading.set(true);
    this.error.set(null);
    this.api.versions().subscribe({
      next: (vs) => {
        this.versions.set(vs);
        if (!vs.some((v) => v.version_no === versionNo)) {
          versionNo = vs[0]?.version_no ?? 1;
        }
        this.switchVersion(versionNo);
      },
      error: (e) => this.failLoad(e),
    });
  }

  switchVersion(versionNo: number) {
    this.viewingRecord.set(null);
    this.view.set(VIEW.CURRENT);
    this.currentVersionNo.set(versionNo);
    localStorage.setItem(AppComponent.VERSION_KEY, String(versionNo));
    this.loading.set(true);
    this.error.set(null);
    this.api.topology(versionNo).subscribe({
      next: (t) => {
        this.topology.set(t);
        this.targetId.set(this.defaultTarget(t));
        this.loadLocks(versionNo);
        this.result.set(null);
        this.loading.set(false);
      },
      error: (e) => this.failLoad(e),
    });
  }

  private defaultTarget(t: Topology): string {
    return (
      t.nodes.find((n) => n.kind === 'equipment')?.id ??
      t.nodes.find((n) => n.id === 'T')?.id ??
      t.nodes[0]?.id ??
      'T'
    );
  }

  private loadLocks(versionNo: number) {
    this.api.versionLocks(versionNo).subscribe({
      next: (r) => {
        const l: Record<string, boolean> = {};
        for (const v of this.topology()?.valves ?? []) {
          l[v.id] = !!r.locks[v.id];
        }
        this.locks.set(l);
      },
      error: () => {
        const l: Record<string, boolean> = {};
        for (const v of this.topology()?.valves ?? []) {
          l[v.id] = false;
        }
        this.locks.set(l);
      },
    });
  }

  private failLoad(e: unknown) {
    this.error.set(`无法加载：${(e as { message?: string })?.message ?? e}`);
    this.loading.set(false);
  }

  // ------------------------------------------------ 锁定与计算

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    const vno = this.currentVersionNo();
    this.loading.set(true);
    this.error.set(null);
    this.api.isolate(this.targetId(), this.locks(), vno).subscribe({
      next: (r) => {
        this.result.set(r);
        this.locks.update((l) => {
          const next = { ...l };
          for (const v of r.candidate_valves) {
            next[v.id] = v.locked;
          }
          return next;
        });
        this.loading.set(false);
        this.loadHistory();
      },
      error: (e) => {
        this.error.set(`计算失败：${e.error?.detail?.message ?? e.error?.detail ?? e.message ?? e}`);
        this.loading.set(false);
      },
    });
  }

  resetLocks(): void {
    const vno = this.currentVersionNo();
    this.api.resetVersion(vno).subscribe(() => {
      const l: Record<string, boolean> = {};
      for (const v of this.topology()?.valves ?? []) {
        l[v.id] = false;
      }
      this.locks.set(l);
      this.result.set(null);
      this.info.set(`v${vno} 锁定已重置。`);
    });
  }

  /** 三个固定培训样例（基于当前所选版本的同名校验；样例文字描述针对 v1）。 */
  loadSample(sample: 1 | 2 | 3): void {
    const l: Record<string, boolean> = {};
    for (const v of this.topology()?.valves ?? []) {
      l[v.id] = false;
    }
    if (sample === 2 && 'V_TIN' in l) {
      l['V_TIN'] = true;
    } else if (sample === 3 && 'V_TOUT' in l) {
      l['V_TOUT'] = true;
    }
    this.locks.set(l);
    this.compute();
  }

  // ------------------------------------------------ 草案

  refreshDrafts() {
    this.api.drafts().subscribe({ next: (d) => this.drafts.set(d), error: () => {} });
  }

  showDraftsPanel() {
    this.viewingRecord.set(null);
    this.view.set(VIEW.CURRENT);
    this.result.set(null);
    this.refreshDrafts();
  }

  copyDraft() {
    const vno = this.currentVersionNo();
    this.loading.set(true);
    this.api.createDraft(vno, `草案（基于 v${vno}）`).subscribe({
      next: (d) => this.openDraft(d),
      error: (e) => {
        this.error.set(`创建草案失败：${e.error?.detail?.message ?? e.message}`);
        this.loading.set(false);
      },
    });
  }

  openExistingDraft(id: string) {
    this.loading.set(true);
    this.api.draft(id).subscribe({
      next: (d) => this.openDraft(d),
      error: (e) => {
        this.error.set(`打开草案失败：${e.message}`);
        this.loading.set(false);
      },
    });
  }

  private openDraft(d: DraftDetail) {
    this.editingDraft.set(d);
    this.view.set(VIEW.DRAFT);
    this.refreshDrafts();
    this.loading.set(false);
  }

  onDraftChange(d: DraftDetail) {
    this.editingDraft.set(d);
    this.refreshDrafts();
  }

  onDraftPublished(newVersionNo: number) {
    this.editingDraft.set(null);
    this.view.set(VIEW.CURRENT);
    this.info.set(null);
    this.api.versions().subscribe({
      next: (vs) => {
        this.versions.set(vs);
        this.switchVersion(newVersionNo);
        this.refreshDrafts();
      },
    });
  }

  closeEditor() {
    this.editingDraft.set(null);
    this.view.set(VIEW.CURRENT);
    this.refreshDrafts();
  }

  deleteDraft(id: string) {
    this.api.deleteDraft(id).subscribe(() => {
      if (this.editingDraft()?.id === id) {
        this.editingDraft.set(null);
        this.view.set(VIEW.CURRENT);
      }
      this.refreshDrafts();
    });
  }

  // ------------------------------------------------ 历史

  loadHistory() {
    this.api.history().subscribe({ next: (h) => this.history.set(h), error: () => {} });
  }

  showHistory() {
    this.view.set(VIEW.HISTORY);
    this.loadHistory();
  }

  viewRecord(id: number) {
    this.loading.set(true);
    this.api.historyRecord(id).subscribe({
      next: (rec) => {
        // 旧方案始终用它绑定版本的旧拓扑绘制；不重解释，也不改用户的版本选择
        this.viewingRecord.set(rec);
        if (rec.topology) {
          this.topology.set(rec.topology);
        }
        this.result.set(rec.result);
        this.view.set(VIEW.HISTORY);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`读取历史记录失败：${e.message}`);
        this.loading.set(false);
      },
    });
  }

  exitHistory() {
    this.viewingRecord.set(null);
    this.view.set(VIEW.CURRENT);
    this.result.set(null);
    this.switchVersion(this.currentVersionNo());
  }

  // ------------------------------------------------ 导入导出

  exportBundle() {
    this.api.exportBundle().subscribe({
      next: (bundle) => {
        const blob = new Blob([JSON.stringify(bundle, null, 2)], {
          type: 'application/json',
        });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `isolation-topologies-${new Date().toISOString().slice(0, 10)}.json`;
        a.click();
        URL.revokeObjectURL(url);
      },
      error: (e) => this.error.set(`导出失败：${e.message}`),
    });
  }

  importBundle(inputEl: EventTarget | null) {
    const input = inputEl as HTMLInputElement | null;
    const file = input?.files?.[0];
    if (input) {
      input.value = '';
    }
    if (!file) {
      return;
    }
    file.text().then((text) => {
      let bundle: unknown;
      try {
        bundle = JSON.parse(text);
      } catch {
        this.error.set('导入文件不是合法 JSON。');
        return;
      }
      this.loading.set(true);
      this.api.importBundle(bundle).subscribe({
        next: (r) => {
          this.loading.set(false);
          this.info.set(
            `导入完成：新增版本 ${r.versions_added} 个，复用同内容版本 ${r.versions_reused} 个，` +
              `导入计算记录 ${r.records_imported} 条。旧计算仍按其原版本拓扑绘制。`,
          );
          this.loadVersionsAndTopology(1);
          this.loadHistory();
          this.refreshDrafts();
        },
        error: (e) => {
          this.loading.set(false);
          const d = e.error?.detail;
          const errs = d?.errors?.length ? `\n${d.errors.slice(0, 5).join('\n')}` : '';
          this.error.set(`导入被整体拒绝：${d?.message ?? d ?? e.message}${errs}`);
        },
      });
    });
  }

  // ------------------------------------------------ 展示辅助

  valveName(id: string): string {
    return this.topology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }

  lockedValveIds(locks: Record<string, boolean> | null | undefined): string[] {
    return Object.keys(locks ?? {}).filter((k) => locks?.[k]);
  }

  versionBadge(v: TopoVersionMeta | null | undefined): string {
    if (!v) {
      return '';
    }
    return `v${v.version_no}${v.immutable ? ' 🔒' : ''}`;
  }
}
