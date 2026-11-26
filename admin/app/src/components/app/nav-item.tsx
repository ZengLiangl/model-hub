import { NavLink } from 'react-router-dom';
import { motion } from 'framer-motion';
import { cn } from '@/lib/cn';
import type { RouteMeta } from '@/app/routes';

/**
 * 导航项 —— 深色侧栏里的一行。
 *
 * 三条硬规则（来自控制台设计规范）：
 *   1. 未选中是**低饱和灰**，只有当前项亮起（一屏之内最多一处亮）；
 *   2. 强调条只在选中项出现，`scaleY` 从中间展开 —— 它就是"当前"的唯一信号；
 *   3. 深色区里不五彩缤纷：图标与文字同色，靠亮度而非色相区分状态。
 *
 * `compact` 是 72px 左轨形态：只有图标，文字用 sr-only 保留给读屏。
 */
export function NavItem(props: {
  route: RouteMeta;
  label: string;
  onNavigate?: () => void;
  compact?: boolean;
  /** 所在底色：深色导航区（左轨/窄屏抽屉）用 nav，浅色右面板用 panel */
  tone?: 'nav' | 'panel';
}) {
  const Icon = props.route.icon;
  return (
    <NavLink
      to={props.route.path}
      onClick={props.onNavigate}
      title={props.compact ? props.label : undefined}
      className={function (state) {
        return cn(
          'nav-link group relative flex cursor-pointer items-center rounded-[8px]',
          'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
          props.compact ? 'h-9 w-9 justify-center' : 'h-8 gap-2.5 px-2.5',
          // 深色底（左轨 / 窄屏抽屉）用 nav 令牌；右面板是浅底，用 panel 令牌
          props.compact || props.tone === 'nav'
            ? (state.isActive ? 'font-medium text-nav-fg-active' : 'text-nav-fg hover:text-nav-fg-active')
            : (state.isActive ? 'font-medium text-panel-nav-fg-active' : 'text-panel-nav-fg hover:text-panel-nav-fg-active'),
        );
      }}
      style={function (state) {
        if (!state.isActive) return undefined;
        // 深色底上的选中块要用白色叠层，浅底上用墨色叠层
        return { background: (props.compact || props.tone === 'nav')
          ? 'var(--nav-active-bg)' : 'var(--panel-nav-active-bg)' };
      }}
    >
      {function (state) {
        return (
          <>
            {/* 强调条：从中间展开，只在选中项出现 */}
            {state.isActive ? (
              <motion.span
                aria-hidden="true"
                initial={{ scaleY: 0 }}
                animate={{ scaleY: 1 }}
                transition={{ duration: 0.26, ease: [0.16, 1, 0.3, 1] }}
                className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-full bg-accent"
              />
            ) : null}
            <Icon
              size={props.compact ? 16 : 15}
              aria-hidden="true"
              className={cn('shrink-0', state.isActive ? 'opacity-100' : 'opacity-75')}
            />
            {props.compact ? (
              <span className="sr-only">{props.label}</span>
            ) : (
              <span className="truncate">{props.label}</span>
            )}
          </>
        );
      }}
    </NavLink>
  );
}
