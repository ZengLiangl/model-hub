/**
 * 数字/货币/时长的显示口径 —— **全站唯一实现**。
 *
 * 为什么要有这个文件：同样的 token 数在总览、指标、模型、奖励四页各有一份私有
 * 的 `fmt`，阈值和精度还不一样（metrics 那份用 1e3 起跳、两位小数，于是
 * 「980」显示成 "980" 而「1200」显示成 "1.2K"，同一列里两种形态）。口径分叉的
 * 直接后果是**同一个数字在两页看起来不一样**，用户会以为是两个数。
 *
 * 阈值与精度对齐 workbuddy2api-hub 的 fmtCompact：
 *   < 1 万   → 千分位原样（1234 → "1,234"）
 *   ≥ 1 万   → K
 *   ≥ 100 万 → M
 *   ≥ 10 亿  → B
 * 小数位随量级收缩（123K / 12.3K / 1.23K），尾零去掉。
 * 「1 万」而不是「1 千」起跳是刻意的：四位数字还读得清，缩成 1.2K 反而丢信息。
 */

/** 千分位整数；非法值给破折号（不要显示 NaN） */
export function fmtInt(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
  return Math.round(n).toLocaleString('en-US');
}

/**
 * 紧凑计数：K / M / B。
 *
 * 刻意**不用** toLocaleString 的 notation:'compact' —— 它在不同浏览器/locale 下
 * 给出 "1.2万"、"1.2K"、"1K" 三种结果，而且中文 locale 会输出「万/亿」，
 * 与表头排序解析器认的 K/M/B 对不上。
 */
export function fmtCompact(n: number | null | undefined): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
  const abs = Math.abs(n);
  const pick = (div: number, suffix: string): string => {
    const x = n / div;
    const a = Math.abs(x);
    // 小数位随量级收缩：123K 不需要小数，1.23K 需要两位才不丢精度
    const digits = a >= 100 ? 0 : a >= 10 ? 1 : 2;
    return x.toFixed(digits).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '') + suffix;
  };
  if (abs >= 1e9) return pick(1e9, 'B');
  if (abs >= 1e6) return pick(1e6, 'M');
  if (abs >= 1e4) return pick(1e3, 'K');
  return fmtInt(n);
}

/** Token 数：与 fmtCompact 同一口径（单独导出是为了让调用点自解释） */
export function fmtTokens(n: number | null | undefined): string {
  return fmtCompact(n);
}

/** 货币：默认人民币，两位小数 */
export function fmtMoney(n: number | null | undefined, symbol = '¥'): string {
  if (typeof n !== 'number' || !Number.isFinite(n)) return '—';
  return symbol + n.toFixed(2);
}

/** 毫秒 → "820 ms" / "1.25 s"；空值给破折号 */
export function fmtMs(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms';
}

/** 百分比：一位小数；undefined 给破折号（**不要**把「没数据」画成 0.0%） */
export function fmtPct(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v)) return '—';
  return v.toFixed(1) + '%';
}

/**
 * 剩余时长：秒 → "3天2小时" / "2小时15分" / "45分" / "30秒"。
 * 冷却窗口动辄十几小时，显示成 "70621 秒" 没人读得出来。
 */
export function fmtDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds <= 0) return '—';
  const s = Math.floor(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return h > 0 ? d + '天' + h + '小时' : d + '天';
  if (h > 0) return m > 0 ? h + '小时' + m + '分' : h + '小时';
  if (m > 0) return m + '分';
  return s + '秒';
}

/**
 * 绝对时刻：epoch 秒 → "11-25 16:19"（同一天只给时分，跨天带月日）。
 * 冷却恢复时刻要能一眼换算成「还要等多久」，所以本地时区、短格式。
 */
export function fmtClock(epoch: number | null | undefined): string {
  if (typeof epoch !== 'number' || !Number.isFinite(epoch) || epoch <= 0) return '—';
  const d = new Date(epoch * 1000);
  const now = new Date();
  const sameDay = d.getFullYear() === now.getFullYear()
    && d.getMonth() === now.getMonth()
    && d.getDate() === now.getDate();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (sameDay) return hh + ':' + mm;
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return mo + '-' + dd + ' ' + hh + ':' + mm;
}
