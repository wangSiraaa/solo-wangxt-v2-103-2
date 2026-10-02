import { Component, Input } from '@angular/core';

/** 版本徽章：在图、方案、历史等各处一致标识“该内容属于哪个拓扑版本”。 */
@Component({
  selector: 'app-version-badge',
  standalone: true,
  template: `
    <span class="vbadge" [class.immutable]="immutable" [class.small]="small">
      <span class="vid">{{ version }}</span>
      @if (name) {
        <span class="vname">{{ name }}</span>
      }
      @if (immutable) {
        <em class="lock">🔒 不可变</em>
      }
    </span>
  `,
  styles: [
    `
      .vbadge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #e0e7ff;
        border: 1px solid #a5b4fc;
        color: #312e81;
        border-radius: 999px;
        padding: 2px 10px;
        font-size: 12px;
        font-weight: 600;
      }
      .vbadge.immutable {
        background: #fef3c7;
        border-color: #f59e0b;
        color: #92400e;
      }
      .vbadge.small {
        font-size: 11px;
        padding: 1px 8px;
      }
      .vbadge .vid {
        font-family: monospace;
      }
      .vbadge .vname {
        font-weight: 500;
        opacity: 0.85;
      }
      .vbadge .lock {
        font-style: normal;
        font-size: 11px;
      }
    `,
  ],
})
export class VersionBadgeComponent {
  @Input() version = 'v1';
  @Input() name: string | null | undefined = null;
  @Input() immutable = false;
  @Input() small = false;
}
