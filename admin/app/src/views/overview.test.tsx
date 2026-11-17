import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { MemoryRouter } from 'react-router-dom';
import { LangProvider } from '@/lib/i18n';
import OverviewView from './overview';

function renderView() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: 0 } } });
  return render(
    <QueryClientProvider client={qc}>
      <LangProvider>
        <MemoryRouter>
          <OverviewView />
        </MemoryRouter>
      </LangProvider>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

type Handler = (url: string) => Response;
function mockFetch(handler: Handler) {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => handler(String(input))));
}
const down = () => json({ error: "down" }, 502);

beforeEach(() => sessionStorage.setItem("hub-panel-token", "tk"));

describe('OverviewView 三态', () => {
  it('加载中显示骨架（aria-busy / animate-pulse）', async () => {
    mockFetch(() => json({}));
    renderView();
    expect(document.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
  });

  it('全部失败时给错误态 + 重试按钮', async () => {
    mockFetch(() => down());
    renderView();
    const retry = await screen.findByRole("button", { name: /重试|Retry/i });
    expect(retry).toBeTruthy();
  });

  it('空数据时给空状态与主操作（不是一片空白）', async () => {
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true, engines: {} });
      if (url.includes("/usage")) return json({ requests: 0, errors: 0, total_tokens: 0 });
      if (url.includes("/accounts")) return json({ accounts: [], usable: 0 });
      return json({});
    });
    renderView();
    const box = await waitFor(() => {
      const el = document.querySelector("[data-empty-state]");
      if (!el) throw new Error("no empty state yet");
      return el as HTMLElement;
    });
    expect(box).toBeTruthy();
    expect(within(box).getAllByRole("button").length).toBeGreaterThan(0);
  });

  it('有数据时渲染 KPI 与引擎表；一家失败给出局部提示', async () => {
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true, engines: { wb: { ok: true, version: "1.7.16" } } });
      if (url.includes("/qd/usage")) return down();
      if (url.includes("/usage")) return json({ requests: 1200, errors: 3, total_tokens: 45600, cost_cny: 1.25 });
      if (url.includes("/accounts")) return json({ accounts: [{ uid: "a" }], usable: 1 });
      return json({});
    });
    renderView();
    await waitFor(() => expect(screen.getByText("2,400")).toBeInTheDocument());
    expect(screen.getAllByText("6").length).toBeGreaterThan(0); // 两家各 3 条错误（KPI 与环形图例都会出现）
    // 局部失败提示里应出现失败那家的名字
    const status = screen.getAllByRole("status").map((n) => n.textContent || "").join(" ");
    expect(status).toMatch(/Qoder/);
  });

  // 回归守卫（2026-11-17 实测）：错误卡原来读的是引擎**运行日志**里的 ERROR 行，
  // 而上面的「失败请求」KPI 数的是**用量账本**。日志是每引擎 200 行的环形缓冲，
  // 一忙就滚没了 —— 于是出现「上面 318 条失败、下面一条记录都没有」。
  // 现在错误卡读 /usage/recent（账本级逐条记录），副标题写清窗口与口径。
  it('错误卡读账本：列出失败请求，并把窗口与中断数写进副标题', async () => {
    mockFetch((url) => {
      if (url.includes('/health')) return json({ ok: true, engines: {} });
      if (url.includes('/wb/usage/recent')) return json({ total: 3, rows: [
        { at: 1794926789, iso: '2026-11-17T22:46:29', model: 'deepseek-v4.1-flash', outcome: 'failed', elapsed_ms: 2494 },
        { at: 1794926780, iso: '2026-11-17T22:46:20', model: 'deepseek-v4.1-flash', outcome: 'failed', elapsed_ms: 3106 },
        { at: 1794926770, iso: '2026-11-17T22:46:10', model: 'deepseek-v4.1-flash', outcome: 'client_aborted', elapsed_ms: 120 },
      ] });
      if (url.includes('/usage/recent')) return json({ total: 0, rows: [] });
      if (url.includes('/logs')) return json({ logs: [] });
      if (url.includes('/usage')) return json({ requests: 10, errors: 2, total_tokens: 1 });
      return json({});
    });
    renderView();
    await waitFor(() => expect(screen.getByText(/deepseek-v4\.1-flash · 2494ms/)).toBeInTheDocument());
    const sub = document.body.textContent || '';
    expect(sub).toMatch(/(最近 3 条请求里 2 条失败|2 failed out of the last 3 requests)/);
    expect(sub).toMatch(/(客户端中断 1 条另计|1 client aborts excluded)/);
    // 运行日志里那条 ERROR 不该再被当成「失败记录」的来源
    expect(sub).not.toMatch(/(错误流水只在失败时才有内容|This tab only fills up on failures)/);
  });

  it('只有客户端中断时不算失败：空态说明窗口并给出入口', async () => {
    mockFetch((url) => {
      if (url.includes('/health')) return json({ ok: true, engines: {} });
      if (url.includes('/wb/usage/recent')) return json({ total: 2, rows: [
        { at: 1794926789, iso: '2026-11-17T22:46:29', model: 'm', outcome: 'client_aborted' },
        { at: 1794926780, iso: '2026-11-17T22:46:20', model: 'm', outcome: 'client_aborted' },
      ] });
      if (url.includes('/usage/recent')) return json({ total: 0, rows: [] });
      if (url.includes('/logs')) return json({ logs: [] });
      if (url.includes('/usage')) return json({ requests: 4, errors: 2, total_tokens: 1 });
      return json({});
    });
    renderView();
    await waitFor(() => expect(document.body.textContent || '').toMatch(/(最近 2 条请求里没有失败|No failures in the last 2 requests)/));
    const sub = document.body.textContent || '';
    expect(sub).toMatch(/(客户端中断 2 条另计|2 client aborts excluded)/);
    expect(screen.getByRole('button', { name: /请求流水|Requests/i })).toBeInTheDocument();
  });
});
