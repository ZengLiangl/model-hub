import { useEffect, useMemo } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { X } from 'lucide-react';
import { useLang } from '@/lib/i18n';
import { ROUTES } from '@/app/routes';
import { cn } from '@/lib/cn';

/**
 * 标签页 —— 取代顶栏标题 + 面包屑。
 *
 * 为什么删掉顶栏：原来的「管理端 / 概览」标题栏与面包屑，和标签页在语义上
 * 重复 —— 都在回答"我在哪"。留着两条是让用户读两遍同一句话。标题信息已经在
 * 标签上、在面板高亮上，第三处纯属噪音。
 *
 * 标签是**会话内**的访问记录（localStorage 持久化）：关掉标签 = 从记录里移除，
 * 但不会影响路由；最后一个标签不可关闭（否则会出现"没有任何标签"的空状态）。
 */

const TABS_KEY = 'hub-tabs';

function readTabs(): string[] {
  try {
    const raw = localStorage.getItem(TABS_KEY);
    if (raw) {
      const parsed = JSON.parse(raw) as unknown;
      if (Array.isArray(parsed)) {
        const known = parsed.filter(function (p): p is string {
          return typeof p === 'string' && ROUTES.some(function (r) { return r.path === p; });
        });
        if (known.length) return known;
      }
    }
  } catch { /* ignore */ }
  return ['/overview'];
}

export function TabBar() {
  const { t } = useLang();
  const location = useLocation();
  const navigate = useNavigate();

  // 标签列表直接由当前路径派生 + localStorage 记忆，避免额外状态机
  const tabs = useMemo(function () {
    const saved = readTabs();
    if (saved.indexOf(location.pathname) === -1) return saved.concat([location.pathname]);
    return saved;
  }, [location.pathname]);

  useEffect(function () {
    try { localStorage.setItem(TABS_KEY, JSON.stringify(tabs)); } catch { /* ignore */ }
  }, [tabs]);

  function closeTab(path: string) {
    const next = tabs.filter(function (p) { return p !== path; });
    if (!next.length) return;                       // 最后一个标签不关
    try { localStorage.setItem(TABS_KEY, JSON.stringify(next)); } catch { /* ignore */ }
    if (path === location.pathname) {
      // 关掉当前标签 -> 跳到右边那个，没有右边就跳左边（浏览器的通用习惯）
      const idx = tabs.indexOf(path);
      navigate(next[Math.min(idx, next.length - 1)]);
    } else {
      // 关的不是当前页：强制重渲染（tabs 由 location 派生，需要一次导航或本地刷新）
      navigate(location.pathname);
    }
  }

  if (tabs.length <= 1) return null;

  return (
    <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-line-faint px-3 py-1.5">
      {tabs.map(function (path) {
        const meta = ROUTES.find(function (r) { return r.path === path; });
        if (!meta) return null;
        const active = path === location.pathname;
        const Icon = meta.icon;
        return (
          <div
            key={path}
            className={cn(
              'group flex h-7 shrink-0 items-center gap-1.5 rounded-[6px] pl-2 pr-1 text-[11.5px]',
              active ? 'bg-surface-2 font-medium text-fg' : 'text-fg-mute hover:bg-surface-2/60 hover:text-fg-dim',
            )}
          >
            <button
              type="button"
              onClick={function () { navigate(path); }}
              className="flex cursor-pointer items-center gap-1.5"
            >
              <Icon size={12} aria-hidden="true" className={active ? 'text-accent' : 'opacity-70'} />
              <span className="truncate">{t(meta.labelKey)}</span>
            </button>
            {tabs.length > 1 ? (
              <button
                type="button"
                onClick={function () { closeTab(path); }}
                aria-label={'关闭 ' + t(meta.labelKey)}
                className="grid h-4 w-4 cursor-pointer place-items-center rounded-[3px] text-fg-mute opacity-0 hover:bg-line-faint hover:text-fg group-hover:opacity-100"
              >
                <X size={10} />
              </button>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
