import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { AlertTriangle, RefreshCw, Users } from 'lucide-react';
import { Button, Card, EmptyState, EngineBadge, SkeletonRows } from '@/components/ui';
import { AreaChart, BarChart, Donut, Sparkline } from '@/components/charts';
import { useLang } from '@/lib/i18n';
import { ENGINE_LABELS, ENGINES, engineLogs, engineRecentUsage, engineUsage, fanout, hubCooldowns, hubHealth, type Engine, type LogRow, type RecentRequestRow } from '@/lib/api';
import { bucketSeries, ratio } from '@/lib/series';
import { fmtCompact, fmtClock, fmtDuration, fmtInt, fmtMoney, fmtTokens } from '@/lib/format';

/** 紧凑计数（K/M/B）：图表轴与提示用 */
const fmt = fmtCompact;
/** 精确千分位：需要逐位读准的地方（请求数、账号数）仍用这个 */
const fmtExact = fmtInt;
const money = fmtMoney;

/** 一格 KPI：标签 / 主数字 / 备注，克制到只有这三层信息。
 *
 * 加了两个「看得见」的东西（对齐 workbuddy2api-hub 面板的做法）：
 *   · trend：底部迷你趋势线 —— 数字在涨还是在跌，一眼就有判断；
 *   · title：悬停给精确值 —— 紧凑显示（1.23M）会丢低位，逐位核对时不必换页。
 * 两者都可选：没有时序数据时不画线，不留空白占位。
 */
function Kpi(props: {
  label: string;
  value: string;
  hint?: string;
  tone?: 'err';
  title?: string;
  trend?: number[];
  trendTone?: string;
}) {
  return (
    <div className="rounded-lg border border-line-faint bg-surface px-3.5 py-3">
      <p className="text-[11px] text-fg-mute">{props.label}</p>
      <p
        title={props.title}
        className={'mt-1 text-[21px] font-semibold leading-7 tnum ' + (props.tone === 'err' ? 'text-err' : 'text-fg')}
      >
        {props.value}
      </p>
      {props.hint ? <p className="mt-0.5 truncate text-[11px] text-fg-mute">{props.hint}</p> : null}
      {props.trend && props.trend.length > 1 ? (
        <div className="mt-1.5 -mx-0.5">
          <Sparkline
            data={props.trend}
            height={22}
            tone={props.trendTone ?? (props.tone === 'err' ? 'err' : 'accent')}
            ariaLabel={props.label}
          />
        </div>
      ) : null}
    </div>
  );
}

export default function OverviewView() {
  const { t } = useLang();
  const navigate = useNavigate();

  const health = useQuery({ queryKey: ['hub-health'], queryFn: hubHealth });
  /** 模型冷却：引擎记着的上游限流窗口。放在总览最上面 ——
   *  它解释了两件用户最容易困惑的事：为什么某个模型现在不通、什么时候会好。 */
  const cooldowns = useQuery({ queryKey: ['hub-cooldowns'], queryFn: () => hubCooldowns(), refetchInterval: 60000 });
  // 总览统计必须跨区域：引擎默认只报「当前区域」，账号在 cn、引擎跑在 intl 时
  // 成功请求会被过滤成 0，只剩失败数，看起来像统计坏了。
  const usage = useQuery({ queryKey: ['overview-usage'], queryFn: () => fanout(async (eng: Engine) => engineUsage(eng, 'all')) });
  const logs = useQuery({
    queryKey: ['overview-logs'],
    queryFn: async () => {
      const rows: { eng: Engine; row: LogRow }[] = [];
      const failed: Engine[] = [];
      for (const eng of ENGINES) {
        try {
          const data = await engineLogs(eng, 200);
          for (const row of (data.logs ?? []) as LogRow[]) rows.push({ eng: eng, row: row });
        } catch { failed.push(eng); }
      }
      return { rows: rows, failed: failed };
    },
  });

  /** 失败请求：读**账本**（/usage/recent），不读运行日志。
   *
   * 运行日志是每引擎 200 行的环形缓冲，一忙就滚没；而上面的「失败请求」KPI 数的是
   * 账本里今天累计的行。两处窗口不同就会出现「上面 318 条失败、下面 0 条记录」
   * （2026-11-17 实测：22:37 后的日志窗口里一条 ERROR 都没有，失败全在 21 点那波
   * 之前的账本里，或者压根不是 ERROR 级别）。
   */
  const recent = useQuery({
    queryKey: ['overview-recent'],
    queryFn: () => fanout((eng: Engine) => engineRecentUsage(eng, 200)),
  });

  const totals = useMemo(() => {
    const values = Object.values(usage.data?.values ?? {}) as Array<Record<string, number> | undefined>;
    const sum = (k: string) => values.reduce((a, r) => a + (Number(r?.[k]) || 0), 0);
    return {
      requests: sum('requests'),
      errors: sum('errors'),
      aborted: sum('client_aborted'),
      throttled: sum('throttled'),
      tokens: sum('total_tokens'), credit: sum('credit'), cost: sum('cost_cny'),
    };
  }, [usage.data]);

  const engines = health.data?.engines ?? {};
  const accountTotals = useMemo(() => {
    let total = 0; let ready = 0;
    for (const eng of ENGINES) {
      total += Number(engines[eng]?.accounts) || 0;
      ready += Number(engines[eng]?.accounts_ready) || 0;
    }
    return { total: total, ready: ready };
  }, [engines]);

  const series = useMemo(() => bucketSeries((logs.data?.rows ?? []).map(function (r) { return r.row; }), 24), [logs.data]);

  /** 失败请求的分桶序列：给「失败」KPI 一条自己的趋势线。
   *
   * 用和主图同一个分桶器（24 桶），所以两条线的横轴口径一致 —— 用户能直接
   * 对照「总流量涨了」和「失败也涨了」是不是同一段时间。 */
  const errorSeries = useMemo(function () {
    const rows = (logs.data?.rows ?? [])
      .filter(function (x) { return Number(x.row.status ?? 0) >= 400 || x.row.error === true; })
      .map(function (x) { return x.row; });
    return bucketSeries(rows, 24).counts;
  }, [logs.data]);

  /** 每引擎一条迷你曲线：同一时间窗分桶，方便横向比较谁在忙 */
  const perEngineSeries = useMemo(function () {
    const out: Record<string, number[]> = {};
    for (const eng of ENGINES) {
      const rows = (logs.data?.rows ?? []).filter(function (x) { return x.eng === eng; }).map(function (x) { return x.row; });
      out[eng] = bucketSeries(rows, 24).counts;
    }
    return out;
  }, [logs.data]);

  const byModel = useMemo(() => {
    const map = new Map<string, number>();
    for (const eng of ENGINES) {
      const bm = (usage.data?.values?.[eng]?.by_model ?? {}) as Record<string, unknown>;
      for (const key of Object.keys(bm)) {
        const raw = bm[key];
        const n = typeof raw === 'number' ? raw : Number((raw as { requests?: number })?.requests) || 0;
        map.set(key, (map.get(key) ?? 0) + n);
      }
    }
    return Array.from(map, function (e) { return { label: e[0], value: e[1] }; }).sort(function (a, b) { return b.value - a.value; }).slice(0, 6);
  }, [usage.data]);

  /** 最近的**失败请求**（账本口径）：运维每天第一眼要看的东西，直接摆在概览上。
   *
   * 只算 outcome=failed；client_aborted（客户端主动断开）单独记数 —— 它不是网关
   * 故障，混进来会把成功率和错误卡都染红（实测 148 条中断顶起了「失败」的三分之一）。
   */
  const recentFailures = useMemo(function () {
    const rows: { eng: Engine; row: RecentRequestRow }[] = [];
    for (const eng of ENGINES) {
      for (const row of (recent.data?.values?.[eng]?.rows ?? [])) {
        const o = String(row.outcome || '');
        // throttled 是网关自己判的「现在别来」，不是一次失败；它单独计数，
        // 否则一个 4 小时冷却窗口就能把「最近失败」刷成上百条一样的 429。
        if (o === 'failed' || o === 'upstream_aborted') rows.push({ eng: eng, row: row });
      }
    }
    rows.sort(function (a, b) { return Number(b.row.at || 0) - Number(a.row.at || 0); });
    return rows;
  }, [recent.data]);

  /** 窗口内实际看了多少条请求（副标题要说清「在这 M 条里」） */
  const recentScanned = useMemo(function () {
    let n = 0;
    for (const eng of ENGINES) n += (recent.data?.values?.[eng]?.rows ?? []).length;
    return n;
  }, [recent.data]);

  const throttledCount = useMemo(function () {
    let n = 0;
    for (const eng of ENGINES) {
      for (const row of (recent.data?.values?.[eng]?.rows ?? [])) {
        if (String(row.outcome || '') === 'throttled') n += 1;
      }
    }
    return n;
  }, [recent.data]);

  const abortedCount = useMemo(function () {
    let n = 0;
    for (const eng of ENGINES) {
      for (const row of (recent.data?.values?.[eng]?.rows ?? [])) {
        if (String(row.outcome || '') === 'client_aborted') n += 1;
      }
    }
    return n;
  }, [recent.data]);

  const failed = Array.from(new Set([...(usage.data?.failed ?? []), ...(logs.data?.failed ?? [])]));
  /** 冷却列表：按剩余时间升序（快好的排前面，最值得等） */
  const coolList = useMemo(function () {
    const rows = cooldowns.data?.cooldowns ?? [];
    return rows.slice().sort(function (a, b) { return a.seconds_left - b.seconds_left; });
  }, [cooldowns.data]);  const usageAllFailed = (usage.data?.failed?.length ?? 0) === ENGINES.length;
  const allFailed = health.isError || usageAllFailed;
  const loading = usage.isPending || health.isPending;
  // requests = 成功，errors = 真失败。中断（客户端自己断开）与 throttled
  // （网关本地判的 429，没打上游）都不是网关的处理结果，不进成功率分母 ——
  // 否则一个客户端反复重试就能把成功率压到 50%。
  const attempts = totals.requests + totals.errors;
  const empty = !loading && !allFailed && attempts === 0 && accountTotals.total === 0;
  const successRate = ratio(totals.requests, attempts);

  function retryAll() {
    void health.refetch(); void usage.refetch(); void logs.refetch(); void recent.refetch();
  }
  const busy = health.isFetching || usage.isFetching || logs.isFetching || recent.isFetching;

  if (loading) {
    return (
      <div className="flex flex-col gap-3">
        <div className="grid gap-3 @lg:grid-cols-2 @3xl:grid-cols-4">
          {[0, 1, 2, 3].map(function (i) {
            return <div key={i} className="rounded-lg border border-line-faint bg-surface px-3.5 py-3"><SkeletonRows rows={2} cols={1} /></div>;
          })}
        </div>
        <div className="rounded-lg border border-line-faint bg-surface p-4"><SkeletonRows rows={5} cols={3} /></div>
      </div>
    );
  }

  if (allFailed) {
    return (
      <Card title={t('state.errTitle')}>
        <div className="flex flex-col items-start gap-2">
          <p className="text-[12px] text-fg-dim">{t('state.errDesc')}</p>
          <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={retryAll} loading={busy}>{t('common.retry')}</Button>
        </div>
      </Card>
    );
  }

  if (empty) {
    return (
      <EmptyState
        icon={<Users size={18} />}
        title={t('state.emptyTitle')}
        description={t('state.emptyDesc')}
        action={<Button variant="primary" size="sm" onClick={function () { navigate('/accounts'); }} icon={<Users size={14} />}>{t('nav.accounts')}</Button>}
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-[11.5px] text-fg-mute">{t('overview.desc')}</p>
        <Button variant="secondary" size="sm" icon={<RefreshCw size={13} />} onClick={retryAll} loading={busy}>
          {busy ? t('app.loading') : t('common.refresh')}
        </Button>
      </div>

      {failed.length ? (
        <div role="status" className="flex items-start gap-2 rounded-md border border-warn/35 bg-warn-soft px-2.5 py-1.5 text-[11.5px] text-warn">
          <AlertTriangle size={13} className="mt-0.5 shrink-0" aria-hidden="true" />
          <span className="mix-space">{t('overview.pool.partial', { engines: failed.map(function (e) { return ENGINE_LABELS[e]; }).join(' / ') })}</span>
        </div>
      ) : null}

      {/* KPI 条 */}
      <div className="grid gap-3 @lg:grid-cols-2 @3xl:grid-cols-4">
        <Kpi
          label={t('overview.kpi.requests')}
          value={fmtExact(attempts)}
          trend={series.counts}
          trendTone="accent"
          hint={t('engine.online', { up: String(ENGINES.filter(function (e) { return engines[e] && engines[e].ok !== false; }).length), total: String(ENGINES.length) })
            + (totals.aborted || totals.throttled
              ? ' · ' + t('overview.kpi.excluded', { aborted: fmtExact(totals.aborted), throttled: fmtExact(totals.throttled) })
              : '')}
        />
        <Kpi
          label={t('overview.kpi.errors')}
          value={fmtExact(totals.errors)}
          trend={errorSeries}
          trendTone="err"
          hint={successRate === undefined
            ? t('overview.kpi.errorsHint')
            : t('metrics.successRate') + ' ' + successRate.toFixed(1) + '% · ' + t('overview.kpi.errorsHint')}
          tone={totals.errors > 0 ? 'err' : undefined}
        />
        <Kpi
          label={t('overview.kpi.tokens')}
          value={fmtTokens(totals.tokens)}
          /* 紧凑显示会丢低位（1,234,567 → 1.23M），所以精确值挂在 title 上：
             要逐位核对时悬停即可，不用再去指标页翻。 */
          title={fmtExact(totals.tokens) + ' tokens'}
          hint={t('overview.pool.ready') + ' ' + fmtExact(accountTotals.ready) + ' / ' + fmtExact(accountTotals.total)}
        />
        <Kpi label={t('overview.kpi.cost')} value={totals.cost > 0 ? money(totals.cost) : t('overview.kpi.noPricing')} hint={totals.credit > 0 ? t('overview.kpi.credits') + ' ' + fmtCompact(totals.credit) : undefined} />
      </div>

      {/* 模型冷却：有就显示，没有就整块不出现（不留空占位）。
          这块的价值是「解释」而不是「报警」：它告诉用户某个模型现在为什么不通、
          什么时候会好 —— 而这两件事以前只能靠翻引擎日志才能知道。 */}
      {coolList.length > 0 ? (
        <div className="rounded-lg border border-warn/35 bg-warn-soft px-3 py-2.5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="flex items-center gap-1.5 text-[11.5px] font-medium text-warn">
              <AlertTriangle size={13} className="shrink-0" aria-hidden="true" />
              {t('overview.cooldown.title')} · {coolList.length}
            </p>
            <p className="text-[10.5px] text-fg-mute">{t('overview.cooldown.hint')}</p>
          </div>
          <ul className="mt-1.5 flex flex-col gap-1">
            {coolList.slice(0, 5).map(function (c, i) {
              // 进度条按「已等待 / 总窗口」画：窗口总长用 until - (until - left) 推不出来，
              // 所以只用剩余量做相对刻度（最长的那个占满），比较的是「谁快好了」。
              const longest = Math.max.apply(null, coolList.map(function (x) { return x.seconds_left; })) || 1;
              const width = Math.max(4, Math.round((c.seconds_left / longest) * 100));
              return (
                <li key={i} className="flex items-center gap-2 text-[11px]">
                  <EngineBadge engine={c.engine} className="shrink-0" />
                  <span className="min-w-0 flex-1 truncate font-mono text-fg-dim" title={c.model}>{c.model}</span>
                  <span className="shrink-0 text-fg-mute">{c.realm}</span>
                  {/* 剩余时长的横向刻度：长度=相对谁更久，数字=还要等多久 */}
                  <span className="hidden h-1 w-16 shrink-0 overflow-hidden rounded-full bg-line-faint @lg:block">
                    <span className="block h-full rounded-full bg-warn" style={{ width: width + '%' }} />
                  </span>
                  <span className="w-[72px] shrink-0 text-right tnum text-warn" title={fmtClock(c.until)}>
                    {fmtDuration(c.seconds_left)}
                  </span>
                  <span className="w-[52px] shrink-0 text-right tnum text-fg-mute">{fmtClock(c.until)}</span>
                </li>
              );
            })}
          </ul>
          {coolList.length > 5 ? (
            <p className="mt-1 text-[10.5px] text-fg-mute">+{coolList.length - 5}</p>
          ) : null}
        </div>
      ) : null}

      {/* 图表区 */}
      <div className="grid gap-3 @3xl:grid-cols-[minmax(0,1.9fr)_minmax(0,1fr)]">
        <Card title={t('overview.chart.traffic')} subtitle={t('overview.chart.window', { n: String((logs.data?.rows ?? []).length), from: series.labels[0] ?? '—' })}>
          <AreaChart
            data={series.counts}
            labels={series.labels}
            height={150}
            formatValue={fmt}
            ariaLabel={t('metrics.title')}
          />
          <div className="mt-2 flex items-center justify-between text-[10.5px] text-fg-mute">
            <span>{series.labels[0] ?? '—'}</span>
            <span>{series.labels[series.labels.length - 1] ?? '—'}</span>
          </div>
        </Card>

        <Card title={t('metrics.successRate')} subtitle={t('overview.successRate.sub')}>
          <div className="flex items-center gap-4">
            <Donut
              segments={[{ value: totals.requests, tone: 'accent-2' }, { value: totals.errors, tone: 'err' }]}
              centerLabel={successRate === undefined ? '—' : successRate.toFixed(1) + '%'}
              centerHint={t('metrics.successRate')}
              ariaLabel={t('metrics.successRate')}
            />
            <ul className="flex flex-col gap-1.5 text-[11.5px]">
              <li className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: 'var(--accent-2)' }} />{t('common.ok')} <span className="tnum text-fg-mute">{fmtExact(totals.requests)}</span></li>
              <li className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: 'var(--err)' }} />{t('common.error')} <span className="tnum text-fg-mute">{fmtExact(totals.errors)}</span></li>
              {totals.aborted ? (
                <li className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: 'var(--line-strong)' }} />{t('overview.legend.aborted')} <span className="tnum text-fg-mute">{fmtExact(totals.aborted)}</span></li>
              ) : null}
              {totals.throttled ? (
                <li className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full" style={{ background: 'var(--warn)' }} />{t('overview.legend.throttled')} <span className="tnum text-fg-mute">{fmtExact(totals.throttled)}</span></li>
              ) : null}
            </ul>
          </div>
        </Card>
      </div>

      <div className="grid gap-3 @2xl:grid-cols-2">
        <Card title={t('metrics.byModel')}>
          <BarChart data={byModel} maxRows={6} formatValue={fmt} ariaLabel={t('metrics.byModel')} />
        </Card>

        <Card
          title={t('logs.tab.errors')}
          subtitle={t('overview.errors.subtitle', {
            m: recentScanned, n: recentFailures.length, a: abortedCount, th: throttledCount,
          })}
        >
          {recentFailures.length === 0 ? (
            <div className="py-6 text-center">
              <p className="text-[11.5px] text-fg-mute">{t('overview.errors.empty', { m: recentScanned })}</p>
              <button
                onClick={function () { navigate('/logs'); }}
                className="mt-2 cursor-pointer text-[11px] text-accent hover:underline"
              >{t('overview.errors.open')}</button>
            </div>
          ) : (
            <>
              <ul className="flex flex-col">
                {recentFailures.slice(0, 6).map(function (x, i) {
                  const iso = String(x.row.iso ?? '');
                  const ms = Number(x.row.elapsed_ms) || 0;
                  const detail = [
                    String(x.row.model ?? '—'),
                    ms ? ms + 'ms' : '',
                  ].filter(Boolean).join(' · ');
                  return (
                    <li key={i} className="flex items-center gap-2 border-b border-line-faint py-1.5 last:border-0">
                      <span className="w-[52px] shrink-0 font-mono text-[10.5px] tnum text-fg-mute">{iso.slice(11, 19) || '—'}</span>
                      <EngineBadge engine={x.eng} className="shrink-0" />
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-fg-dim" title={detail}>{detail}</span>
                      <span className="shrink-0 rounded bg-err-soft px-1.5 py-0.5 text-[10px] text-err">{t('overview.errors.failed')}</span>
                    </li>
                  );
                })}
              </ul>
              <button
                onClick={function () { navigate('/logs'); }}
                className="mt-2 cursor-pointer text-[11px] text-accent hover:underline"
              >{t('overview.errors.open')}</button>
            </>
          )}
        </Card>
      </div>

      <div className="grid gap-3 @2xl:grid-cols-3">

          {ENGINES.map(function (eng) {
            const info = engines[eng];
            const down = !info || info.ok === false;
            const total = Number(info?.accounts) || 0;
            const ready = Number(info?.accounts_ready) || 0;
            const tone = eng === 'wb' ? 'accent' : eng === 'qd' ? 'accent-2' : 'accent-3';
            return (
              <div key={eng} className="flex flex-col gap-2 rounded-lg border border-line-faint bg-surface px-3.5 py-3">
                <div className="flex items-center justify-between gap-2">
                  <EngineBadge engine={eng} label={ENGINE_LABELS[eng]} />
                  <span className="flex items-center gap-1.5 text-[10.5px] text-fg-mute">
                    <span className="h-1.5 w-1.5 rounded-full" style={{ background: down ? 'var(--err)' : 'var(--ok)' }} />
                    {down ? t('common.error') : t('common.ok')}
                  </span>
                </div>
                <div className="flex items-baseline gap-1.5">
                  <span className="text-[19px] font-semibold leading-6 tnum">{ready}</span>
                  <span className="text-[11px] text-fg-mute tnum">/ {total} {t('overview.pool.ready')}</span>
                </div>
                <Sparkline data={perEngineSeries[eng] ?? []} height={26} ariaLabel={ENGINE_LABELS[eng] + ' 24h'} tone={tone} />
                <div className="flex items-center justify-between text-[10px] text-fg-mute">
                  <span className="font-mono">{String(info?.version || '—')}</span>
                  <span
                    className="tnum"
                    title={fmtExact(Number((usage.data?.values?.[eng] as Record<string, number> | undefined)?.total_tokens) || 0) + ' tokens'}
                  >{t('logs.col.tokens')} {fmtTokens(Number((usage.data?.values?.[eng] as Record<string, number> | undefined)?.total_tokens) || 0)}</span>
                </div>
              </div>
            );
          })}
      </div>
    </div>
  );
}
