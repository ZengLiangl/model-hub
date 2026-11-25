import { cn } from '@/lib/cn';

/**
 * 面板骨架：所有控制台页面共用同一套「指标条 → 判定 → 表格」结构。
 * 目标是让运营扫一眼就知道「现在有没有事、有事是哪几条」。
 */

/** 指标条：一行 3–5 格，每格只有标签 + 主数字 + 一句人话。 */
export function MetricStrip(props: { items: MetricItem[]; className?: string }) {
  return (
    <div
      className={cn(
        'grid gap-px overflow-hidden rounded-lg border border-line-faint bg-line-faint',
        'grid-cols-2 sm:grid-cols-3 lg:grid-cols-' + Math.min(props.items.length, 5),
        props.className,
      )}
      style={{ gridTemplateColumns: 'repeat(' + Math.min(props.items.length, 5) + ', minmax(0, 1fr))' }}
    >
      {props.items.map(function (it) {
        return <MetricCell key={it.label} item={it} />;
      })}
    </div>
  );
}

export type MetricItem = {
  label: string;
  value: string;
  /** 一句话解释这个数字意味着什么，不写单位缩写 */
  hint?: string;
  tone?: 'ok' | 'warn' | 'err' | 'accent';
};

function MetricCell({ item }: { item: MetricItem }) {
  const toneClass =
    item.tone === 'err' ? 'text-err'
      : item.tone === 'warn' ? 'text-warn'
        : item.tone === 'ok' ? 'text-ok'
          : item.tone === 'accent' ? 'text-accent'
            : 'text-fg';
  return (
    <div className="bg-surface px-3.5 py-3">
      <p className="text-[11px] leading-4 text-fg-mute">{item.label}</p>
      <p className={cn('mt-1 text-[21px] font-semibold leading-7 tnum', toneClass)}>{item.value}</p>
      {item.hint ? <p className="mt-0.5 text-[11px] leading-4 text-fg-mute">{item.hint}</p> : null}
    </div>
  );
}

/** 判定条：面板顶部唯一的结论行。ok 时一句话收工，异常时直接说哪几条要处理。 */
export function Verdict(props: { tone: 'ok' | 'warn' | 'err' | 'info'; text: string; detail?: string }) {
  const map = {
    ok: 'border-ok/35 bg-ok-soft text-ok',
    warn: 'border-warn/35 bg-warn-soft text-warn',
    err: 'border-err/35 bg-err-soft text-err',
    info: 'border-line bg-surface-2 text-fg-dim',
  } as const;
  return (
    <div className={cn('flex flex-wrap items-baseline gap-x-2 gap-y-1 rounded-lg border px-3 py-2 text-[12px]', map[props.tone])}>
      <span className="font-medium">{props.text}</span>
      {props.detail ? <span className="text-[11px] opacity-80">{props.detail}</span> : null}
    </div>
  );
}

/** 判定标签：表格里的状态列统一用它，不写「成功/失败」这种含糊词。 */
export function StatusTag(props: { tone: 'ok' | 'warn' | 'err' | 'neutral' | 'info'; children: React.ReactNode }) {
  const map = {
    ok: 'bg-ok-soft text-ok',
    warn: 'bg-warn-soft text-warn',
    err: 'bg-err-soft text-err',
    info: 'bg-info-soft text-info',
    neutral: 'bg-surface-2 text-fg-dim',
  } as const;
  return (
    <span className={cn('inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap', map[props.tone])}>
      {props.children}
    </span>
  );
}

/** 区块：标题 + 可选操作 + 内容，控制台里重复最多的单元。 */
export function Panel(props: {
  title: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={cn('rounded-lg border border-line-faint bg-surface', props.className)}>
      <header className="flex items-center gap-2 border-b border-line-faint px-3.5 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate text-[12.5px] font-semibold leading-5">{props.title}</h2>
          {props.hint ? <p className="truncate text-[11px] leading-4 text-fg-mute">{props.hint}</p> : null}
        </div>
        {props.action ? <div className="ml-auto shrink-0">{props.action}</div> : null}
      </header>
      {props.children}
    </section>
  );
}
