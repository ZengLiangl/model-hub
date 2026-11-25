/**
 * lib/format.ts —— 数字显示口径的判据。
 *
 * 这些判据的存在理由：口径分叉过一次（四个页面各有一份 fmt，阈值和精度还不一样），
 * 结果是**同一个数字在两页看起来不一样**。这里把阈值边界与小数位规则钉住。
 */
import { describe, expect, it } from 'vitest';
import { fmtClock, fmtCompact, fmtDuration, fmtInt, fmtMoney, fmtMs, fmtPct, fmtTokens } from './format';

describe('fmtCompact：K/M/B', () => {
  it('一万以下保持千分位精确值（四位数字还读得清，缩成 K 反而丢信息）', () => {
    expect(fmtCompact(0)).toBe('0');
    expect(fmtCompact(1)).toBe('1');
    expect(fmtCompact(999)).toBe('999');
    expect(fmtCompact(1234)).toBe('1,234');
    expect(fmtCompact(9999)).toBe('9,999');
  });

  it('一万起跳 K，小数位随量级收缩', () => {
    expect(fmtCompact(10000)).toBe('10K');
    expect(fmtCompact(12345)).toBe('12.3K');
    expect(fmtCompact(123456)).toBe('123K');
    // 999999 落在 K 档、三位数 -> 0 位小数，四舍五入成 1000K（进位到下一档是
    // 刻意不做的：那会让 999999 和 1000000 都显示 1M，反而看不出差别）
    expect(fmtCompact(999999)).toBe('1000K');
  });

  it('百万起跳 M', () => {
    expect(fmtCompact(1e6)).toBe('1M');
    expect(fmtCompact(1.23e6)).toBe('1.23M');
    expect(fmtCompact(12345678)).toBe('12.3M');
    expect(fmtCompact(123456789)).toBe('123M');
  });

  it('十亿起跳 B', () => {
    expect(fmtCompact(1e9)).toBe('1B');
    expect(fmtCompact(2.5e9)).toBe('2.5B');
    expect(fmtCompact(123e9)).toBe('123B');
  });

  it('尾零去掉，不出现 "1.00M" 这种形态', () => {
    expect(fmtCompact(1.5e6)).toBe('1.5M');
    expect(fmtCompact(1.25e6)).toBe('1.25M');
    expect(fmtCompact(2e6)).toBe('2M');
  });

  it('负数按绝对值分档，符号保留', () => {
    expect(fmtCompact(-12345)).toBe('-12.3K');
    expect(fmtCompact(-1.5e6)).toBe('-1.5M');
  });

  it('非法值给破折号，不给 NaN', () => {
    expect(fmtCompact(undefined)).toBe('—');
    expect(fmtCompact(null)).toBe('—');
    expect(fmtCompact(NaN)).toBe('—');
    expect(fmtCompact(Infinity)).toBe('—');
  });
});

describe('fmtTokens 与 fmtCompact 同口径', () => {
  it('token 数走同一套单位', () => {
    for (const n of [0, 999, 12345, 1.5e6, 2e9]) {
      expect(fmtTokens(n)).toBe(fmtCompact(n));
    }
  });
});

describe('fmtInt：精确千分位', () => {
  it('四舍五入后带千分位', () => {
    expect(fmtInt(1234)).toBe('1,234');
    expect(fmtInt(1234.6)).toBe('1,235');
    expect(fmtInt(0)).toBe('0');
  });

  it('非法值给破折号', () => {
    expect(fmtInt(undefined)).toBe('—');
    expect(fmtInt(NaN)).toBe('—');
  });
});

describe('fmtMoney / fmtMs / fmtPct', () => {
  it('货币两位小数', () => {
    expect(fmtMoney(0)).toBe('¥0.00');
    expect(fmtMoney(1.5)).toBe('¥1.50');
    expect(fmtMoney(undefined)).toBe('—');
  });

  it('毫秒在 1000 处切到秒', () => {
    expect(fmtMs(41)).toBe('41 ms');
    expect(fmtMs(999)).toBe('999 ms');
    expect(fmtMs(1000)).toBe('1.00 s');
    expect(fmtMs(1234)).toBe('1.23 s');
    expect(fmtMs(null)).toBe('—');
  });

  it('百分比一位小数；undefined 不显示成 0.0%', () => {
    expect(fmtPct(0)).toBe('0.0%');
    expect(fmtPct(99.44)).toBe('99.4%');
    expect(fmtPct(undefined)).toBe('—');
  });
});

describe('fmtDuration：冷却窗口要人读得懂', () => {
  it('秒 / 分 / 小时 / 天 逐级', () => {
    expect(fmtDuration(30)).toBe('30秒');
    expect(fmtDuration(90)).toBe('1分');
    expect(fmtDuration(3600)).toBe('1小时');
    expect(fmtDuration(3900)).toBe('1小时5分');
    expect(fmtDuration(86400)).toBe('1天');
    expect(fmtDuration(90000)).toBe('1天1小时');
  });

  it('70621 秒（实测的冷却窗口）读作 19 小时级而不是一串数字', () => {
    expect(fmtDuration(70621)).toBe('19小时37分');
  });

  it('0 与负值给破折号（已到期不该显示 "0秒"）', () => {
    expect(fmtDuration(0)).toBe('—');
    expect(fmtDuration(-5)).toBe('—');
    expect(fmtDuration(undefined)).toBe('—');
  });
});

describe('fmtClock：恢复时刻', () => {
  it('当天只给时分，跨天带月日', () => {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 15, 30, 0).getTime() / 1000;
    expect(fmtClock(today)).toBe('15:30');

    const other = new Date(now.getFullYear(), now.getMonth(), now.getDate() - 2, 9, 5, 0).getTime() / 1000;
    expect(fmtClock(other)).toMatch(/^\d{2}-\d{2} 09:05$/);
  });

  it('非法值给破折号', () => {
    expect(fmtClock(0)).toBe('—');
    expect(fmtClock(undefined)).toBe('—');
  });
});
