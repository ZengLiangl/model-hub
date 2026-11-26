import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { LangProvider } from '@/lib/i18n';
import { ToastProvider } from '@/components/ui';
import { queryClient } from '@/lib/query';
import App from '@/App';

function renderApp() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </LangProvider>
    </QueryClientProvider>,
  );
}

function mockFetch(handler: (url: string, init: RequestInit) => Response) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => handler(String(input), init || {})));
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  window.location.hash = "";
  // ★ 真 App 用的是模块级 queryClient（staleTime 15s），不是本文件里那个
  //   QueryClientProvider —— 不清缓存的话，后一个用例会直接读到前一个用例
  //   缓存的 /api/hub/health（比如没有 version 的那份），测什么都是假的。
  queryClient.clear();
});

describe('AuthGate', () => {
  it('没有令牌时只显示登录表单，不渲染导航', async () => {
    mockFetch(() => json({ ok: false }, 401));
    renderApp();
    await waitFor(() => expect(screen.getByLabelText(/password|面板密码/i)).toBeInTheDocument());
    expect(screen.queryAllByRole("link")).toHaveLength(0);
  });

  it('密码错误时就地提示（不跳走、不弹 toast）', async () => {
    mockFetch((url) => (url.includes("/panel/login") ? json({ error: "bad" }, 401) : json({}, 401)));
    renderApp();
    const pwd = await screen.findByLabelText(/password|面板密码/i);
    await userEvent.type(pwd, "wrong");
    await userEvent.click(screen.getByRole("button", { name: /sign in|进入面板/i }));
    const alert = await screen.findByRole("alert");
    expect(alert).toBeTruthy();
  });
});

describe('AppShell', () => {
  it('已登录后导航项齐全（右面板的二级项 + 窄屏副本）', async () => {
    sessionStorage.setItem("hub-panel-token", "tk");
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true });
      return json({});
    });
    renderApp();
    // 双层导航下，右面板只显示**当前模块**的二级项（overview 属于 main）；
    // 窄屏导航块在 navOpen=false 时不渲染，所以链接数 = main 组的项数。
    await waitFor(() => expect(screen.getAllByRole("link").length).toBeGreaterThanOrEqual(5));
    const links = screen.getAllByRole("link");
    // main 组：overview / accounts / models / plaza / metrics / routing
    expect(links.length).toBe(6);
  });

  // 回归守卫：切换轮次里 App.tsx 曾被简化成不含 ToastProvider，
  // 结果只有"用到 toast 的视图"（accounts/settings/rewards/api）会崩，
  // 而默认页 overview 不用 toast，测试全绿 —— 这条就是补那个洞。
  it('ToastProvider 缺失回归：进入 accounts 页不触发错误边界', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => {
      if (url.includes('/health')) return json({ ok: true, engines: {} });
      if (url.includes('/accounts')) return json({ accounts: [{ uid: 'u1', nickname: 'alpha', realm: 'cn', enabled: true }], usable: 1 });
      return json({});
    });
    window.location.hash = '#/accounts';
    renderApp();
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull(), { timeout: 5000 });
    await waitFor(() => expect(screen.getByText('alpha')).toBeInTheDocument(), { timeout: 5000 });
  });

  // 回归守卫：侧边栏版本号曾经是源码里的硬编码 "v1.0.0"，后端早已 1.1.0 ——
  // 面板自己先把版本说错了。现在取 /api/hub/health 的 version。
  it('侧边栏版本号来自后端 health.version（不是硬编码）', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health')
      ? json({ ok: true, version: '9.9.9', engines: {} })
      : json({})));
    renderApp();
    await waitFor(() => expect(document.body.textContent || '').toMatch(/v9\.9\.9/), { timeout: 5000 });
    expect(document.body.textContent || '').not.toMatch(/v1\.0\.0/);
  });

  it('健康接口没给 version 时只显示「喵枢」，不显示错的版本号', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('.ad-island')).toBeTruthy(), { timeout: 5000 });
    expect(document.body.textContent || '').not.toMatch(/v1\.0\.0/);
  });

  // ── 岛式外壳（控制台设计规范第一条）──────────────────────────────
  it('外壳是浮在背景上的一块板：.ad-island 存在且承载全部内容', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    const island = await waitFor(() => {
      const el = document.querySelector('.ad-island');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    }, { timeout: 5000 });
    // 岛里必须真的有导航与内容，而不是一个空壳
    expect(island.querySelectorAll('a[href]').length).toBeGreaterThan(0);
    expect(island.querySelector('main')).toBeTruthy();
  });

  // ── 双层导航（规范第三条）：72 左轨 + 240 右面板 ──────────────────
  it('双层导航：左轨只放图标、右面板放当前模块的二级项', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('.ad-island')).toBeTruthy(), { timeout: 5000 });
    // 一级模块是 button（不是 link），二级项才是 link。
    // .rail-item 同时被主题按钮复用，所以总数是「三个分组 + 主题」= 4。
    const railButtons = document.querySelectorAll('button.rail-item');
    expect(railButtons.length).toBe(4);
    // 其中 3 个是一级模块（Daily / Operations / Config），第 4 个是主题按钮
    const titles = Array.from(railButtons).map(function (b) { return b.getAttribute('title') || ''; });
    expect(titles.filter(function (x) { return /Daily|Operations|Config|日常|运维|配置/i.test(x); }).length).toBe(3);
  });

  // 回归守卫：岛的「高度」必须是固定一屏，不能是 min-height。
  // 用 min-height 时，日志页几百行内容会把岛撑高、整页跟着滚，左轨被拉出视口
  // —— 左下角的主题/语言切换就在运维页消失（2026-11-26 实测，两页左下角不一致）。
  it('岛高度固定为一屏，左轨左下角不随内容跑掉', async () => {
    // vitest 下 import.meta.url 不是 file:// URL，用 cwd 拼（测试从 admin/app 跑）
    const { readFileSync } = await import('node:fs');
    const { join } = await import('node:path');
    const css = readFileSync(join(process.cwd(), 'src/styles/tokens.css'), 'utf8');
    const island = css.match(/\.ad-island\s*\{([\s\S]*?)\}/);
    expect(island).toBeTruthy();
    const body = island![1];
    expect(body).toMatch(/height:\s*calc\(100dvh/);
    expect(body).not.toMatch(/(?<!-)min-height:\s*calc\(100dvh/);
  });

  // ── 顶栏已删除（规范第七条）：标签页取代面包屑 ────────────────────
  it('不再有重复的顶栏面包屑：桌面下没有 header', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('.ad-island')).toBeTruthy(), { timeout: 5000 });
    // 唯一允许存在的 header 是窄屏那个（lg:hidden），桌面结构里不该有可见 header
    const headers = Array.from(document.querySelectorAll('header'));
    const desktopHeaders = headers.filter(function (h) {
      return !(h.className || '').includes('lg:hidden');
    });
    expect(desktopHeaders.length).toBe(0);
  });

  // ── 状态一律用点（规范第六条）────────────────────────────────────
  it('状态用圆点而不是色块', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('.ad-island')).toBeTruthy(), { timeout: 5000 });
    expect(document.querySelectorAll('.ad-dot').length).toBeGreaterThan(0);
  });

  // ── 主题三态（规范第八条）────────────────────────────────────────
  it('主题按钮在 明/暗/跟随系统 之间循环', async () => {
    sessionStorage.setItem('hub-panel-token', 'tk');
    localStorage.setItem('hub-theme', 'light');
    mockFetch((url) => (url.includes('/health') ? json({ ok: true, engines: {} }) : json({})));
    renderApp();
    await waitFor(() => expect(document.querySelector('.ad-island')).toBeTruthy(), { timeout: 5000 });
    const btn = await waitFor(() => {
      const el = document.querySelector('button[aria-label="明"]');
      expect(el).toBeTruthy();
      return el as HTMLElement;
    }, { timeout: 5000 });
    await userEvent.click(btn);
    // 明 -> 暗
    await waitFor(() => expect(document.documentElement.getAttribute('data-theme')).toBe('dark'));
    expect(localStorage.getItem('hub-theme')).toBe('dark');
    // 暗 -> 跟随系统
    const btn2 = document.querySelector('button[aria-label="暗"]') as HTMLElement;
    await userEvent.click(btn2);
    expect(localStorage.getItem('hub-theme')).toBe('system');
  });

  it('窄屏下点汉堡按钮出现移动端导航容器', async () => {
    sessionStorage.setItem("hub-panel-token", "tk");
    mockFetch((url) => (url.includes("/health") ? json({ ok: true }) : json({})));
    renderApp();
    const toggle = await screen.findByRole("button", { name: /open navigation|打开导航/i });
    expect(document.querySelector("[data-mobile-nav]")).toBeNull();
    await userEvent.click(toggle);
    expect(document.querySelector("[data-mobile-nav]")).toBeTruthy();
  });
});
