import { useEffect, useState } from 'react';
import { Outlet, useLocation, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';

const LOGO = (import.meta.env.BASE_URL || './') + 'logo.jpg';
import { Menu, Monitor, Moon, Sun, X } from 'lucide-react';
import { motion } from 'framer-motion';
import { useLang } from '@/lib/i18n';
import { hubHealth } from '@/lib/api';
import { UpdateBanner } from '@/components/app/update';
import { cn } from '@/lib/cn';
import { GROUP_LABEL, ROUTES, type RouteMeta } from '@/app/routes';
import { NavItem } from './nav-item';
import { RouteTransition } from './route-transition';
import { TabBar } from './tab-bar';

const THEME_KEY = 'hub-theme';
/** 三态：明 / 暗 / 跟随系统（规范第八条） */
type ThemeMode = 'light' | 'dark' | 'system';

function readThemeMode(): ThemeMode {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'light' || saved === 'dark' || saved === 'system') return saved;
  } catch { /* ignore */ }
  return 'system';
}

function applyTheme(mode: ThemeMode) {
  const resolved = mode === 'system'
    ? (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')
    : mode;
  document.documentElement.setAttribute('data-theme', resolved);
}

/** 一级模块：左轨的一个图标 */
function RailItem(props: {
  group: RouteMeta['group'];
  label: string;
  icon: typeof ROUTES[number]['icon'];
  active: boolean;
  onClick: () => void;
  expanded: boolean;
}) {
  const Icon = props.icon;
  return (
    <button
      type="button"
      onClick={props.onClick}
      aria-current={props.active ? 'true' : undefined}
      title={props.label}
      className={cn(
        'rail-item relative flex h-10 w-full cursor-pointer items-center rounded-[8px] text-nav-fg',
        'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
        props.expanded ? 'gap-2.5 px-3' : 'justify-center',
        props.active ? 'text-nav-fg-active' : 'hover:text-nav-fg-active',
      )}
      style={props.active ? { background: 'var(--nav-active-bg)' } : undefined}
    >
      {/* 强调条：整个界面里唯一的"当前"信号之一（另一处是二级项） */}
      {props.active ? (
        <motion.span
          aria-hidden="true"
          initial={{ scaleY: 0 }}
          animate={{ scaleY: 1 }}
          transition={{ duration: 0.26, ease: [0.16, 1, 0.3, 1] }}
          className="absolute left-0 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-full bg-accent"
        />
      ) : null}
      <Icon size={17} aria-hidden="true" className={props.active ? 'opacity-100' : 'opacity-75'} />
      {props.expanded ? <span className="truncate text-[12.5px]">{props.label}</span> : null}
    </button>
  );
}

export function AppShell() {
  const lang = useLang();
  const t = lang.t;
  const [themeMode, setThemeMode] = useState<ThemeMode>(readThemeMode);
  /** 左轨悬停展开：鼠标进轨道时展开到 216px 露出文字 */
  const [railOpen, setRailOpen] = useState(false);
  const [navOpen, setNavOpen] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const health = useQuery({ queryKey: ['hub-health'], queryFn: hubHealth });
  const version = health.data?.version ? 'v' + health.data.version : '';

  /** 当前一级模块（决定右面板显示什么） */
  const currentGroup = (function () {
    const hit = ROUTES.find(function (r) { return location.pathname.startsWith(r.path); });
    return hit ? hit.group : 'main';
  })();
  const panelItems = ROUTES.filter(function (r) { return r.group === currentGroup; });
  const current = ROUTES.find(function (r) { return location.pathname.startsWith(r.path); }) ?? ROUTES[0];
  const title = t(current.labelKey);

  /** 主题：三态循环，系统态跟随系统变化实时响应 */
  useEffect(function () {
    applyTheme(themeMode);
    try { localStorage.setItem(THEME_KEY, themeMode); } catch { /* ignore */ }
  }, [themeMode]);

  useEffect(function () {
    if (themeMode !== 'system' || typeof window.matchMedia !== 'function') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    function onChange() { applyTheme('system'); }
    mq.addEventListener('change', onChange);
    return function () { mq.removeEventListener('change', onChange); };
  }, [themeMode]);

  function cycleTheme() {
    setThemeMode(function (m) { return m === 'light' ? 'dark' : m === 'dark' ? 'system' : 'light'; });
  }
  const themeIcon = themeMode === 'light' ? <Sun size={14} /> : themeMode === 'dark' ? <Moon size={14} /> : <Monitor size={14} />;
  const themeLabel = themeMode === 'light' ? '明' : themeMode === 'dark' ? '暗' : '跟随系统';

  const groups: RouteMeta['group'][] = ['main', 'ops', 'config'];
  const groupIcon: Record<RouteMeta['group'], typeof ROUTES[number]['icon']> = {
    main: ROUTES.find(function (r) { return r.group === 'main'; })!.icon,
    ops: ROUTES.find(function (r) { return r.group === 'ops'; })!.icon,
    config: ROUTES.find(function (r) { return r.group === 'config'; })!.icon,
  };

  return (
    <div className="ad-island">
      {/* ══ 左侧导航：全页唯一的深色区 ═══════════════════════════ */}
      <div className="hidden shrink-0 lg:flex">
        {/* 左轨 72px：只放图标，悬停展开到 216px */}
        <div
          onMouseEnter={function () { setRailOpen(true); }}
          onMouseLeave={function () { setRailOpen(false); }}
          className="relative z-10 flex shrink-0 flex-col border-r border-white/5 bg-nav-bg transition-[width] duration-260"
          style={{ width: railOpen ? 'var(--rail-open)' : 'var(--rail-w)', transitionTimingFunction: 'var(--ease)' }}
        >
          {/* 品牌 */}
          <div className={cn('flex h-14 shrink-0 items-center', railOpen ? 'gap-2.5 px-3' : 'justify-center')}>
            <img
              src={LOGO}
              alt=""
              aria-hidden="true"
              className="logo-bob h-8 w-8 shrink-0 rounded-[9px] object-cover ring-1 ring-white/10"
            />
            {railOpen ? (
              <div className="min-w-0 flex-1">
                <p className="truncate text-[12.5px] font-semibold leading-4 text-nav-fg-active" title={t('app.name')}>
                  {t('app.name')}
                </p>
                <p className="truncate text-[10px] leading-4 text-nav-fg" title={t('app.short')}>
                  {t('app.short')} {version}
                </p>
              </div>
            ) : null}
          </div>

          {/* 一级模块 */}
          <div className={cn('flex min-h-0 flex-1 flex-col gap-1 overflow-y-auto py-2', railOpen ? 'px-2' : 'px-2')}>
            {groups.map(function (g, i) {
              const active = currentGroup === g;
              return (
                <div key={g} className={i > 0 ? 'mt-6' : ''}>
                  <RailItem
                    group={g}
                    label={t(GROUP_LABEL[g])}
                    icon={groupIcon[g]}
                    active={active}
                    expanded={railOpen}
                    onClick={function () {
                      const first = ROUTES.find(function (r) { return r.group === g; });
                      if (first) navigate(first.path);
                      // 点完一级项，轨道**立刻收回**：展开状态会压住右面板，
                      // 而"选择模块"这个动作已经完成，没有继续展开的理由。
                      setRailOpen(false);
                    }}
                  />
                </div>
              );
            })}
          </div>

          {/* 底部：主题 + 语言 */}
          <div className={cn('flex shrink-0 items-center gap-1 border-t border-white/5 py-2', railOpen ? 'px-2' : 'justify-center px-1')}>
            <button
              type="button"
              onClick={cycleTheme}
              aria-label={themeLabel}
              title={themeLabel}
              className="rail-item grid h-8 w-8 shrink-0 cursor-pointer place-items-center rounded-[8px] text-nav-fg hover:text-nav-fg-active"
            >
              {themeIcon}
            </button>
            {railOpen ? (
              <button
                type="button"
                onClick={function () { lang.setLang(lang.lang === 'zh' ? 'en' : 'zh'); }}
                className="rail-item ml-auto h-8 cursor-pointer rounded-[8px] px-2.5 text-[11px] text-nav-fg hover:text-nav-fg-active"
              >
                {lang.lang === 'zh' ? 'EN' : '中'}
              </button>
            ) : null}
          </div>
        </div>

        {/* 右面板 240px：当前模块的二级菜单，独立滚动，底部固定用户信息 */}
        <div
          className="flex shrink-0 flex-col border-r border-line-faint bg-surface-2/40"
          style={{ width: 'var(--panel-w)' }}
        >
          <div className="flex h-14 shrink-0 items-center px-4">
            <p className="truncate text-[12px] font-semibold tracking-tight text-fg">{t(GROUP_LABEL[currentGroup])}</p>
          </div>
          <nav aria-label={t('nav.title')} className="nav-scroll min-h-0 flex-1 overflow-y-auto px-2 pb-2">
            <div className="flex flex-col gap-1">
              {panelItems.map(function (r) {
                return (
                  <NavItem
                    key={r.id}
                    route={r}
                    label={t(r.labelKey)}
                    onNavigate={function () { setNavOpen(false); }}
                  />
                );
              })}
            </div>
          </nav>
          <div className="flex shrink-0 items-center gap-2 border-t border-line-faint px-3 py-2.5">
            <span
              className="ad-dot"
              style={{ background: health.data?.ok === false ? 'var(--err)' : 'var(--ok)' }}
              aria-hidden="true"
            />
            <span className="min-w-0 flex-1 truncate text-[11px] text-fg-mute">
              {t('engine.online', { up: '—', total: '3' }).split(' ')[0]} {version}
            </span>
          </div>
        </div>
      </div>

      {/* ══ 主区域 ═══════════════════════════════════════════════ */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* 窄屏顶栏（桌面下不需要：标题已在标签上、面板高亮上） */}
        <header className="flex h-12 shrink-0 items-center gap-2 border-b border-line-faint px-3 lg:hidden">
          <button
            type="button"
            aria-label={navOpen ? '关闭导航' : '打开导航'}
            aria-expanded={navOpen}
            onClick={function () { setNavOpen(function (v) { return !v; }); }}
            className="icon-btn grid h-8 w-8 cursor-pointer place-items-center rounded-[8px] text-fg-dim"
          >
            {navOpen ? <X size={16} /> : <Menu size={16} />}
          </button>
          <img src={LOGO} alt="" aria-hidden="true" className="h-5 w-5 rounded-[5px] object-cover" />
          <h1 className="min-w-0 truncate text-[13px] font-semibold">{title}</h1>
          <button
            type="button"
            onClick={cycleTheme}
            aria-label={themeLabel}
            className="icon-btn ml-auto grid h-8 w-8 cursor-pointer place-items-center rounded-[8px] text-fg-dim"
          >
            {themeIcon}
          </button>
        </header>

        {navOpen ? (
          <div className="border-b border-line-faint bg-nav-bg px-2 py-2 lg:hidden" data-mobile-nav="1">
            <div className="flex items-center gap-2 px-2 pb-2">
              <img src={LOGO} alt="" aria-hidden="true" className="h-6 w-6 rounded-[6px] object-cover" />
              <div className="min-w-0">
                <p className="truncate text-[12px] font-semibold leading-4 text-nav-fg-active">{t('app.name')}</p>
                <p className="truncate text-[10px] leading-4 text-nav-fg">{t('app.short')} {version}</p>
              </div>
            </div>
            <nav aria-label={t('nav.title')} className="flex flex-col gap-1">
              {groups.map(function (g) {
                const items = ROUTES.filter(function (r) { return r.group === g; });
                if (!items.length) return null;
                return (
                  <div key={g} className="flex flex-col gap-1">
                    <p className="px-2 pt-2 pb-1 text-[9.5px] font-medium uppercase tracking-[0.09em] text-nav-fg">
                      {t(GROUP_LABEL[g])}
                    </p>
                    {items.map(function (r) {
                      return (
                        <NavItem key={r.id} route={r} label={t(r.labelKey)} onNavigate={function () { setNavOpen(false); }} />
                      );
                    })}
                  </div>
                );
              })}
            </nav>
          </div>
        ) : null}

        {/* 标签页取代面包屑：访问过的页面变成一条可关闭的标签 */}
        <TabBar />

        <main className="@container min-w-0 flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-5">
          <RouteTransition>
            <UpdateBanner t={t} />
            <Outlet />
          </RouteTransition>
        </main>
      </div>
    </div>
  );
}
