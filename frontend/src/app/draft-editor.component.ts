import { Component, EventEmitter, Input, Output, inject } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from './api.service';
import { Draft, DraftContent, DraftSegment, TopoNode, ValidationErrorItem } from './models';

type NodeKind = TopoNode['kind'];
type SegKind = DraftSegment['kind'];

/**
 * 拓扑草案编辑器：从某版本复制出草案后，可编辑
 * 节点、管段方向、阀门、旁路标记、来源/目标种类、必要供给点。
 * 可随时校验；发布由父组件触发（后端再次强校验，失败不留半张图）。
 */
@Component({
  selector: 'app-draft-editor',
  standalone: true,
  imports: [FormsModule],
  templateUrl: './draft-editor.component.html',
})
export class DraftEditorComponent {
  private api = inject(ApiService);

  @Input({ required: true }) draft!: Draft;
  @Output() draftChange = new EventEmitter<Draft>();
  @Output() published = new EventEmitter<string>();
  @Output() closed = new EventEmitter<void>();

  errors: ValidationErrorItem[] = [];
  busy = false;
  publishName = '';
  serverError = '';

  nodeKinds: NodeKind[] = ['source', 'equipment', 'consumer', 'junction'];
  segKinds: SegKind[] = ['main', 'bypass', 'branch'];

  get content(): DraftContent {
    return this.draft.content;
  }

  get nodeIds(): string[] {
    return this.content.nodes.map((n) => n.id);
  }

  // ---------------- 节点 ----------------

  addNode(): void {
    const used = new Set(this.nodeIds);
    let base = 'N';
    let i = this.content.nodes.length + 1;
    let id = `${base}${i}`;
    while (used.has(id)) {
      i += 1;
      id = `${base}${i}`;
    }
    this.content.nodes.push({
      id,
      name: `新节点${i}`,
      kind: 'junction',
      x: 120 + Math.round(Math.random() * 760),
      y: 80 + Math.round(Math.random() * 360),
      essential: false,
    });
    this.markDirty();
  }

  removeNode(id: string): void {
    this.content.nodes = this.content.nodes.filter((n) => n.id !== id);
    // 连带删除引用该节点的管段，避免留下不完整边
    this.content.segments = this.content.segments.filter(
      (s) => s.upstream_id !== id && s.downstream_id !== id,
    );
    this.markDirty();
  }

  // ---------------- 管段 / 阀门 / 方向 / 旁路 ----------------

  addSegment(): void {
    const used = new Set(this.content.segments.map((s) => s.id));
    let i = this.content.segments.length + 1;
    let id = `EN${i}`;
    while (used.has(id)) {
      i += 1;
      id = `EN${i}`;
    }
    const first = this.content.nodes[0]?.id ?? '';
    this.content.segments.push({
      id,
      upstream_id: first,
      downstream_id: first,
      kind: 'main',
      is_bypass: false,
      valve: { id: `V_NEW${i}`, name: `新阀门${i}`, is_open: true, operable: true },
    });
    this.markDirty();
  }

  reverse(s: DraftSegment): void {
    const t = s.upstream_id;
    s.upstream_id = s.downstream_id;
    s.downstream_id = t;
    this.markDirty();
  }

  toggleValve(s: DraftSegment): void {
    if (s.valve) {
      s.valve = null;
    } else {
      const used = new Set(
        this.content.segments.filter((x) => x.valve).map((x) => x.valve!.id),
      );
      let i = this.content.segments.length + 1;
      let vid = `V_NEW${i}`;
      while (used.has(vid)) {
        i += 1;
        vid = `V_NEW${i}`;
      }
      s.valve = { id: vid, name: `新阀门${i}`, is_open: true, operable: true };
    }
    this.markDirty();
  }

  removeSegment(id: string): void {
    this.content.segments = this.content.segments.filter((s) => s.id !== id);
    this.markDirty();
  }

  // ---------------- 保存 / 校验 / 发布 ----------------

  private markDirty(): void {
    this.errors = [];
    this.serverError = '';
  }

  save(): void {
    this.busy = true;
    this.api.updateDraft(this.draft.id, this.content).subscribe({
      next: (d) => {
        this.draft = d;
        this.errors = d.validation_errors;
        this.draftChange.emit(d);
        this.busy = false;
      },
      error: (e) => {
        this.serverError = e.error?.detail ?? e.message;
        this.busy = false;
      },
    });
  }

  validate(): void {
    this.busy = true;
    this.api.updateDraft(this.draft.id, this.content).subscribe({
      next: (d) => {
        this.draft = d;
        this.api.validateDraft(this.draft.id).subscribe({
          next: (r) => {
            this.errors = r.errors;
            this.busy = false;
          },
          error: () => (this.busy = false),
        });
      },
      error: (e) => {
        this.serverError = e.error?.detail ?? e.message;
        this.busy = false;
      },
    });
  }

  publish(): void {
    this.busy = true;
    this.serverError = '';
    this.api.updateDraft(this.draft.id, this.content).subscribe({
      next: (d) => {
        this.draft = d;
        this.api.publishDraft(this.draft.id, this.publishName.trim() || undefined).subscribe({
          next: (r) => {
            this.busy = false;
            if (r.published && r.version_id) {
              this.published.emit(r.version_id);
            } else {
              this.errors = r.errors;
            }
          },
          error: (e) => {
            // 409 修订冲突等由服务端返回；整体拒绝，不留半张图
            this.serverError = e.error?.detail ?? e.message;
            this.busy = false;
          },
        });
      },
      error: (e) => {
        this.serverError = e.error?.detail ?? e.message;
        this.busy = false;
      },
    });
  }

  close(): void {
    this.closed.emit();
  }
}
