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

  // 回归守卫：错误卡的副标题曾经复用了**空态**那句文案（logs.errorEmpty.desc），
  // 结果下面明明列着几条错误，卡片头上还写着「错误流水只在失败时才有内容」。
  it('错误卡副标题随实际条数走，不冒充空态文案', async () => {
    mockFetch((url) => {
      if (url.includes("/health")) return json({ ok: true, engines: {} });
      if (url.includes("/wb/logs")) return json({ logs: [
        { ts: "2026-11-17T22:04:27", level: "error", msg: "request error: status=503 no usable account" },
        { ts: "2026-11-17T22:04:28", level: "error", msg: "models.dev refresh skipped" },
        { ts: "2026-11-17T22:04:29", level: "info", msg: "ok line" },
      ] });
      if (url.includes("/logs")) return json({ logs: [] });
      if (url.includes("/usage")) return json({ requests: 10, errors: 2, total_tokens: 1 });
      return json({});
    });
    renderView();
    await waitFor(() => expect(screen.getByText(/no usable account/)).toBeInTheDocument());
    const errCard = screen.getByText(/no usable account/).closest("[class*=rounded-lg]") as HTMLElement | null;
    const sub = document.body.textContent || "";
    // jsdom 的语言跟随运行环境，断言不绑语言：中英两版都要能过。
    expect(sub).toMatch(/(最近 2 条失败|Latest 2 failures)/);
    expect(sub).not.toMatch(/(错误流水只在失败时才有内容|This tab only fills up on failures)/);
    expect(errCard).toBeTruthy();
  });
});
