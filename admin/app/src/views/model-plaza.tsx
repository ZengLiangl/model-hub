import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { RefreshCw, Search } from 'lucide-react';
import {
  Button, EngineBadge, EmptyState, Input, MetricStrip, Panel, SkeletonRows,
  StatusTag, TableWrap, Td, Th, Tr, Verdict, type MetricItem,
} from '@/components/ui';
import { useLang } from '@/lib/i18n';
import { fmtCompact } from '@/lib/format';
import {
  ENGINE_LABELS, ENGINES, engineModels, hubCooldowns, type Engine, type ModelRow, type Realm,
} from '@/lib/api';

/**
 * 模型广场：回答一个问题 —— 现在有哪些模型能用、哪几家同时有、哪个正在冷却。
 *
 * 与「模型清单」的分工：清单是完整目录（价格、能力、上限），广场只保留
 * 排障与选型需要的三件事：可用性、上下文、冷却。所以不画图表，只给判定条 + 一张表。
 */

type Row = {
  id: string;
  engines: Engine[];
  ctx?: number;
  out?: number;
  vision: boolean;
  tools: boolean;
  reasoning: boolean;
  cooling: boolean;
  coolUntil?: string;
};

function positive(v: unknown): number | undefined {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : undefined;
}

function mergeModels(byEngine: Partial<Record<Engine, ModelRow[]>>): Row[] {
  const map = new Map<string, Row>();
  for (const eng of ENGINES) {
    for (const r of byEngine[eng] ?? []) {
      const id = String(r.id || '').trim();
      if (!id) continue;
      const caps = (r.capabilities || {}) as Record<string, boolean>;
      const cur = map.get(id);
      if (cur) {
        cur.engines.push(eng);
        cur.ctx = cur.ctx ?? positive(r.context_length) ?? positive(r.context_window);
        cur.out = cur.out ?? positive(r.max_output_tokens) ?? positive(r.max_completion_tokens);
        cur.vision = cur.vision || !!(caps.vision ?? r.supports_vision);
        cur.tools = cur.tools || !!(caps.tool_calls ?? r.supports_tool_calls);
        cur.reasoning = cur.reasoning || !!(caps.reasoning ?? r.supports_reasoning);
      } else {
        map.set(id, {
          id,
          engines: [eng],
          ctx: positive(r.context_length) ?? positive(r.context_window),
          out: positive(r.max_output_tokens) ?? positive(r.max_completion_tokens),
          vision: !!(caps.vision ?? r.supports_vision),
          tools: !!(caps.tool_calls ?? r.supports_tool_calls),
          reasoning: !!(caps.reasoning ?? r.supports_reasoning),
          cooling: false,
        });
      }
    }
  }
  return Array.from(map.values());
}

export default function ModelPlazaView() {
  const { t } = useLang();
  const [q, setQ] = useState('');
  const [realm, setRealm] = useState<Realm>('cn');

  const models = useQuery({
    queryKey: ['plaza-models', realm],
    queryFn: async () => {
      const byEngine: Partial<Record<Engine, ModelRow[]>> = {};
      const failed: Engine[] = [];
      for (const eng of ENGINES) {
        try {
          const data = await engineModels(eng, realm);
          byEngine[eng] = (data.data ?? []) as ModelRow[];
        } catch {
          failed.push(eng);
        }
      }
      return { byEngine, failed };
    },
  });

  const cooldowns = useQuery({
    queryKey: ['hub-cooldowns'],
    queryFn: () => hubCooldowns(),
    refetchInterval: 60000,
  });

  const rows = useMemo(() => {
    const merged = mergeModels(models.data?.byEngine ?? {});
    const coolMap = new Map<string, string>();
    const list = (cooldowns.data?.cooldowns ?? []) as { model?: string; until?: number; seconds_left?: number }[];
    for (const c of list) {
      const key = String(c.model || '').trim();
      if (!key) continue;
      // 服务端给了剩余秒数就用它，避免前端时钟偏差把「刚恢复」算成还在冷却。
      const left = Number(c.seconds_left);
      coolMap.set(key, Number.isFinite(left) ? String(Math.max(0, left)) : '');
    }
    for (const r of merged) {
      const left = coolMap.get(r.id);
      if (left !== undefined) {
        r.cooling = true;
        r.coolUntil = left;
      }
    }
    const kw = q.trim().toLowerCase();
    return kw ? merged.filter((r) => r.id.toLowerCase().includes(kw)) : merged;
  }, [models.data, cooldowns.data, q]);

  const cooling = rows.filter((r) => r.cooling);
  const multi = rows.filter((r) => r.engines.length > 1);
  const withTools = rows.filter((r) => r.tools);

  const metrics: MetricItem[] = [
    {
      label: t('plaza.metric.total'),
      value: String(rows.length),
      hint: t('plaza.metric.totalHint'),
    },
    {
      label: t('plaza.metric.multi'),
      value: String(multi.length),
      hint: t('plaza.metric.multiHint'),
      tone: multi.length > 0 ? 'accent' : undefined,
    },
    {
      label: t('plaza.metric.cooling'),
      value: String(cooling.length),
      hint: cooling.length ? t('plaza.metric.coolingHint') : t('plaza.metric.coolingNone'),
      tone: cooling.length > 0 ? 'warn' : 'ok',
    },
    {
      label: t('plaza.metric.tools'),
      value: String(withTools.length),
      hint: t('plaza.metric.toolsHint'),
    },
  ];

  const verdict = models.isPending
    ? { tone: 'info' as const, text: t('state.loading') }
    : cooling.length
      ? {
        tone: 'warn' as const,
        text: t('plaza.verdict.cooling', { n: cooling.length }),
        detail: cooling.slice(0, 3).map((r) => r.id).join(' · '),
      }
      : models.data?.failed.length
        ? {
          tone: 'warn' as const,
          text: t('plaza.verdict.partial', { engines: models.data.failed.map((e) => ENGINE_LABELS[e]).join(' / ') }),
        }
        : { tone: 'ok' as const, text: t('plaza.verdict.ok', { n: rows.length }) };

  return (
    <div className="flex flex-col gap-3">
      <MetricStrip items={metrics} />
      <Verdict tone={verdict.tone} text={verdict.text} detail={verdict.detail} />

      <Panel
        title={t('plaza.title')}
        hint={t('plaza.desc')}
        action={(
          <div className="flex items-center gap-1.5">
            <div role="tablist" aria-label={t('models.col.realm')} className="flex gap-0.5 rounded-md border border-line p-0.5">
              {(['cn', 'intl'] as Realm[]).map((r) => (
                <button
                  key={r}
                  role="tab"
                  aria-selected={realm === r}
                  onClick={() => setRealm(r)}
                  className={
                    'min-h-8 cursor-pointer rounded px-2 text-[11px] transition-colors ' +
                    (realm === r ? 'bg-accent-soft font-medium text-accent' : 'text-fg-dim hover:bg-surface-2')
                  }
                >
                  {t(r === 'cn' ? 'realm.cn.short' : 'realm.intl.short')}
                </button>
              ))}
            </div>
            <Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} loading={models.isFetching}
              onClick={() => void models.refetch()}>
              {t('common.refresh')}
            </Button>
          </div>
        )}
      >
        <div className="border-b border-line-faint px-3.5 py-2">
          <div className="relative w-full sm:w-64">
            <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-fg-mute" aria-hidden="true" />
            <Input value={q} onChange={(e) => setQ(e.target.value)} className="h-8 pl-8 text-[12px]"
              placeholder={t('plaza.searchPlaceholder')} aria-label={t('common.search')} />
          </div>
        </div>

        {models.isPending ? <SkeletonRows rows={6} cols={5} /> : null}

        {!models.isPending && rows.length === 0 ? (
          <EmptyState icon={<Search size={18} />} title={t('plaza.empty')} description={t('plaza.emptyDesc')} />
        ) : null}

        {!models.isPending && rows.length > 0 ? (
          <TableWrap className="rounded-none border-0">
            <thead>
              <tr>
                <Th>{t('plaza.col.model')}</Th>
                <Th>{t('plaza.col.engines')}</Th>
                <Th className="text-right">{t('plaza.col.ctx')}</Th>
                <Th className="text-right">{t('plaza.col.out')}</Th>
                <Th>{t('plaza.col.caps')}</Th>
                <Th>{t('plaza.col.state')}</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <Tr key={r.id}>
                  <Td className="max-w-[280px] truncate font-medium text-fg" title={r.id}>{r.id}</Td>
                  <Td>
                    <span className="flex flex-wrap gap-1">
                      {r.engines.map((e) => (
                        <EngineBadge key={e} engine={e} label={ENGINE_LABELS[e]} />
                      ))}
                    </span>
                  </Td>
                  <Td className="text-right tnum">{r.ctx ? fmtCompact(r.ctx) : '—'}</Td>
                  <Td className="text-right tnum">{r.out ? fmtCompact(r.out) : '—'}</Td>
                  <Td>
                    <span className="flex flex-wrap gap-1">
                      {r.vision ? <StatusTag tone="neutral">{t('models.cap.vision')}</StatusTag> : null}
                      {r.tools ? <StatusTag tone="neutral">{t('models.cap.tools')}</StatusTag> : null}
                      {r.reasoning ? <StatusTag tone="neutral">{t('models.cap.reasoning')}</StatusTag> : null}
                      {!r.vision && !r.tools && !r.reasoning ? <span className="text-fg-mute">—</span> : null}
                    </span>
                  </Td>
                  <Td>
                    {r.cooling
                      ? <StatusTag tone="warn">{t('plaza.state.cooling')}</StatusTag>
                      : <StatusTag tone="ok">{t('plaza.state.ready')}</StatusTag>}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </TableWrap>
        ) : null}
      </Panel>
    </div>
  );
}
