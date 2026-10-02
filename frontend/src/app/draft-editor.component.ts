import { Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import { DraftContent, DraftDetail, DraftSegment, TopoNode } from './models';

const KIND_LABELS: Record<string, string> = {
  source: '来源',
  equipment: '目标设备',
  consumer: '用户',
  junction: '桥点',
};

/**
 * 拓扑草案编辑器：复制某版本后，可编辑节点（含必要供给点标记）、
 * 管段方向（一键反转 source/target）、旁路标记、来源/目标节点以及阀门。
 * 保存是“草稿态”允许有错；发布前由后端整体校验，错误清单在此展示。
 */
@Component({
  selector: 'app-draft-editor',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './draft-editor.component.html',
})
export class DraftEditorComponent {
  @Input({ required: true }) draft!: DraftDetail;
  @Output() draftChange = new EventEmitter<DraftDetail>();
  @Output() published = new EventEmitter<number>();
  @Output() closed = new EventEmitter<void>();

  private api = inject(ApiService);

  saving = false;
  publishing = false;
  error = '';
  info = '';
  kindLabels = KIND_LABELS;
  nodeKinds = ['source', 'equipment', 'consumer', 'junction'];
  segKinds = ['main', 'bypass', 'branch'];

  get content(): DraftContent {
    return this.draft.content;
  }

  get nodes(): TopoNode[] {
    return this.content.nodes;
  }

  get segments(): DraftSegment[] {
    return this.content.segments;
  }

  nodeName(id: string | null | undefined): string {
    return this.nodes.find((n) => n.id === id)?.name ?? id ?? '—';
  }

  private touch() {
    this.info = '';
    this.error = '';
  }

  // ---------- 节点 ----------

  addNode() {
    this.touch();
    const n = this.nodes.length + 1;
    let id = `N_NEW${n}`;
    let i = n;
    while (this.nodes.some((x) => x.id === id)) {
      i += 1;
      id = `N_NEW${i}`;
    }
    this.nodes.push({
      id,
      name: `新节点${i}`,
      kind: 'junction',
      x: 120 + ((i * 90) % 700),
      y: 120 + ((i * 70) % 360),
      essential: false,
    });
  }

  removeNode(id: string) {
    this.touch();
    this.content.nodes = this.nodes.filter((n) => n.id !== id);
    // 同时删除悬挂管段（交由后端校验也可，但前端即时反馈更清晰）
    this.content.segments = this.segments.filter((s) => s.source !== id && s.target !== id);
  }

  // ---------- 管段 / 阀门 ----------

  addSegment() {
    this.touch();
    const n = this.segments.length + 1;
    let id = `E_NEW${n}`;
    let i = n;
    while (this.segments.some((x) => x.id === id)) {
      i += 1;
      id = `E_NEW${i}`;
    }
    const first = this.nodes[0]?.id ?? '';
    const second = this.nodes[1]?.id ?? first;
    this.segments.push({
      id,
      source: first,
      target: second,
      kind: 'main',
      is_bypass: false,
      valve: {
        id: `V_NEW${i}`,
        name: `新阀${i}`,
        is_open: true,
        locked: false,
        operable: true,
      },
    });
  }

  removeSegment(s: DraftSegment) {
    this.touch();
    this.content.segments = this.segments.filter((x) => x !== s);
  }

  reverseDirection(s: DraftSegment) {
    this.touch();
    const u = s.source;
    s.source = s.target;
    s.target = u;
  }

  toggleValve(s: DraftSegment) {
    this.touch();
    if (s.valve) {
      s.valve = null;
    } else {
      const n = this.segments.filter((x) => x.valve).length + 1;
      let vid = `V_NEW${n}`;
      let i = n;
      const existing = new Set(this.segments.map((x) => x.valve?.id));
      while (existing.has(vid)) {
        i += 1;
        vid = `V_NEW${i}`;
      }
      s.valve = { id: vid, name: `新阀${i}`, is_open: true, locked: false, operable: true };
    }
  }

  // ---------- 保存 / 校验 / 发布 ----------

  save() {
    this.saving = true;
    this.error = '';
    this.api.saveDraft(this.draft.id, { name: this.draft.name, content: this.content }).subscribe({
      next: (d) => {
        this.draft = d;
        this.draftChange.emit(d);
        this.saving = false;
        this.info = d.validation.valid ? '已保存，当前校验通过。' : `已保存为草稿（${d.validation.errors.length} 项校验问题）。`;
      },
      error: (e) => {
        this.error = `保存失败：${e.error?.detail?.message ?? e.error?.detail ?? e.message}`;
        this.saving = false;
      },
    });
  }

  publish() {
    this.publishing = true;
    this.error = '';
    this.info = '';
    // 先保存再发布，避免发布的是旧内容
    this.api.saveDraft(this.draft.id, { name: this.draft.name, content: this.content }).subscribe({
      next: (d) => {
        this.draft = d;
        this.draftChange.emit(d);
        this.api.publishDraft(d.id, d.base_version_no).subscribe({
          next: (res) => {
            this.publishing = false;
            this.info = `已发布为 v${res.published.version_no}：${res.published.name}`;
            this.published.emit(res.published.version_no);
          },
          error: (e) => {
            this.publishing = false;
            const detail = e.error?.detail;
            if (detail?.code === 'revision_conflict') {
              this.error =
                `修订冲突：该草案基线为 v${detail.base_version_no}，` +
                `当前最新版本已是 v${detail.latest_version_no}。请基于最新版本重新复制草案，本次发布未覆盖任何版本。`;
            } else if (detail?.code === 'validation_failed') {
              this.error = `发布被整体拒绝（${detail.errors?.length ?? 0} 项校验问题，已在下方列出），没有产生新版本或半张图。`;
              this.draft = { ...this.draft, validation: { valid: false, errors: detail.errors } };
              this.draftChange.emit(this.draft);
            } else {
              this.error = `发布失败：${detail?.message ?? detail ?? e.message}`;
            }
          },
        });
      },
      error: (e) => {
        this.publishing = false;
        this.error = `保存失败：${e.error?.detail?.message ?? e.message}`;
      },
    });
  }

  close() {
    this.closed.emit();
  }
}
