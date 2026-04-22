const {
  escapeHtml,
  formatTime,
  formatTimeFull,
  formatDuration,
  parseTime,
  scoreBadge,
  statusBadge,
} = require('../utils');

describe('escapeHtml', () => {
  test('returns empty string for null', () => {
    expect(escapeHtml(null)).toBe('');
  });

  test('returns empty string for undefined', () => {
    expect(escapeHtml(undefined)).toBe('');
  });

  test('escapes angle brackets', () => {
    expect(escapeHtml('<script>alert("xss")</script>')).toBe(
      '&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;'
    );
  });

  test('escapes ampersand', () => {
    expect(escapeHtml('a & b')).toBe('a &amp; b');
  });

  test('escapes quotes', () => {
    expect(escapeHtml('"hello"')).toBe('&quot;hello&quot;');
  });

  test('escapes single quotes', () => {
    expect(escapeHtml("it's")).toBe("it&#039;s");
  });

  test('passes through normal text', () => {
    expect(escapeHtml('Hello world')).toBe('Hello world');
  });

  test('converts number to string', () => {
    expect(escapeHtml(42)).toBe('42');
  });
});

describe('formatTime', () => {
  test('formats zero', () => {
    expect(formatTime(0)).toBe('0:00');
  });

  test('formats seconds only', () => {
    expect(formatTime(45)).toBe('0:45');
  });

  test('formats minutes and seconds', () => {
    expect(formatTime(90)).toBe('1:30');
  });

  test('formats large values', () => {
    expect(formatTime(3661)).toBe('61:01');
  });

  test('returns placeholder for null', () => {
    expect(formatTime(null)).toBe('--:--');
  });

  test('handles fractional seconds', () => {
    expect(formatTime(90.7)).toBe('1:30');
  });
});

describe('formatTimeFull', () => {
  test('formats with hours', () => {
    expect(formatTimeFull(3661)).toBe('1:01:01');
  });

  test('formats without hours', () => {
    expect(formatTimeFull(90)).toBe('1:30');
  });

  test('returns placeholder for null', () => {
    expect(formatTimeFull(null)).toBe('--:--:--');
  });
});

describe('formatDuration', () => {
  test('formats hours', () => {
    expect(formatDuration(3700)).toBe('1h 1m');
  });

  test('formats minutes', () => {
    expect(formatDuration(90)).toBe('1m 30s');
  });

  test('formats seconds only', () => {
    expect(formatDuration(45)).toBe('45s');
  });

  test('returns placeholder for null', () => {
    expect(formatDuration(null)).toBe('--');
  });

  test('formats zero', () => {
    expect(formatDuration(0)).toBe('0s');
  });
});

describe('parseTime', () => {
  test('parses mm:ss', () => {
    expect(parseTime('1:30')).toBe(90);
  });

  test('parses hh:mm:ss', () => {
    expect(parseTime('01:30:00')).toBe(5400);
  });

  test('returns NaN for empty', () => {
    expect(parseTime('')).toBeNaN();
  });

  test('returns NaN for null', () => {
    expect(parseTime(null)).toBeNaN();
  });

  test('returns NaN for invalid', () => {
    expect(parseTime('abc')).toBeNaN();
  });

  test('returns NaN for single number', () => {
    expect(parseTime('90')).toBeNaN();
  });

  test('handles leading zeros', () => {
    expect(parseTime('01:05')).toBe(65);
  });

  test('handles whitespace', () => {
    expect(parseTime('  1:30  ')).toBe(90);
  });
});

describe('scoreBadge', () => {
  test('high score (>=7)', () => {
    const badge = scoreBadge(8);
    expect(badge).toContain('score-high');
    expect(badge).toContain('8');
  });

  test('mid score (4-6)', () => {
    const badge = scoreBadge(5);
    expect(badge).toContain('score-mid');
  });

  test('low score (<4)', () => {
    const badge = scoreBadge(2);
    expect(badge).toContain('score-low');
  });

  test('null score', () => {
    const badge = scoreBadge(null);
    expect(badge).toContain('?');
    expect(badge).toContain('score-low');
  });

  test('boundary score 7', () => {
    expect(scoreBadge(7)).toContain('score-high');
  });

  test('boundary score 4', () => {
    expect(scoreBadge(4)).toContain('score-mid');
  });
});

describe('statusBadge', () => {
  test('pending status', () => {
    const badge = statusBadge('pending');
    expect(badge).toContain('status-pending');
    expect(badge).toContain('pending');
  });

  test('approved status', () => {
    const badge = statusBadge('approved');
    expect(badge).toContain('status-approved');
  });

  test('rejected status', () => {
    const badge = statusBadge('rejected');
    expect(badge).toContain('status-rejected');
  });

  test('default for falsy', () => {
    const badge = statusBadge('');
    expect(badge).toContain('status-pending');
  });

  test('underscores to spaces', () => {
    const badge = statusBadge('ready_for_review');
    expect(badge).toContain('ready for review');
  });

  test('null defaults to pending', () => {
    const badge = statusBadge(null);
    expect(badge).toContain('status-pending');
  });
});
