// Run: node --test tests/test_usage_currency.cjs
// Exercises the actual inline UI code without network calls or browser dependencies.
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const { test } = require('node:test');

const html = readFileSync(join(__dirname, '..', 'demo.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0];
new vm.Script(script); // Check the entire inline script, not just the tested functions.

class Element {
  constructor() {
    this.textContent = '';
    this.children = [];
    this.style = {};
  }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  text() { return [this.textContent, ...this.children.map(child => child.text())].join(' '); }
}

function ui() {
  const elements = new Map();
  const ctx = vm.createContext({
    usageData: null, usageModelFilter: '', usageSortKey: 'ai_credits',
    usageSortDirection: 'desc',
    $: id => {
      if (!elements.has(id)) elements.set(id, new Element());
      return elements.get(id);
    },
    document: { createElement: () => new Element(), querySelectorAll: () => [] },
  });
  vm.runInContext(script.slice(script.indexOf('function formatCreditValue('),
    script.indexOf('function buildPricingSchedule(')), ctx);
  vm.runInContext(script.slice(script.indexOf('const USAGE_POLL_MS'),
    script.indexOf('function formatUsageGenerated(')), ctx);
  return { ctx, elements };
}

const known = value => ({
  ai_credits: value, requests: 1, requests_with_actual_cost: 1,
  requests_without_actual_cost: 0,
});

test('credit-to-USD conversion preserves fractional amounts and true zeros', () => {
  const { ctx } = ui();
  assert.equal(ctx.formatUsageCredits(known(250)), '250 ($2.50 USD)');
  assert.equal(ctx.formatUsageCredits(known(2.5)), '2.5 ($0.025 USD)');
  assert.equal(ctx.formatUsageCredits(known(35.921)), '35.921 ($0.35921 USD)');
  assert.equal(ctx.formatUsageCredits(known(123456.789)), '123,456.79 ($1,234.56789 USD)');
  assert.equal(ctx.formatUsageCredits(known(0)), '0 ($0.00 USD)');
  assert.match(ctx.formatUsageCredits(known(0.0000001)), /\(<\$0\.000001 USD\)/);
});

test('unknown costs stay unknown; partial coverage applies to both values', () => {
  const { ctx } = ui();
  for (const value of [null, undefined, '', NaN, Infinity, 'invalid']) {
    assert.equal(ctx.formatUsageCredits(known(value)), '—');
  }
  assert.equal(ctx.formatUsageCredits({ ai_credits: 0, requests: 2,
    requests_with_actual_cost: 0, requests_without_actual_cost: 2 }), '—');
  assert.equal(ctx.formatUsageCredits({ ...known(250), requests: 2,
    requests_without_actual_cost: 1 }), '250 ($2.50 USD) (partial actual)');
});

test('summary card and all three tables use the combined formatter', () => {
  const { ctx, elements } = ui();
  const item = { ...known(250), model: 'test-model', date: 'test-day',
    input_tokens: 100, output_tokens: 10, cache_read_tokens: 0,
    cache_write_tokens: 0, cache_write_1h_tokens: 0, succeeded: 1, failed: 0 };
  ctx.usageData = { totals: item, daily: [item], by_model: [item], daily_models: [item] };
  ctx.renderUsageDashboard();
  for (const id of ['usage-cards', 'usage-daily-body',
    'usage-model-totals-body', 'usage-detail-body']) {
    assert.ok(elements.get(id).text().includes('250 ($2.50 USD)'), id);
  }
});

test('sorting still uses numeric credits with unknown costs last', () => {
  const { ctx } = ui();
  const rows = [known(9), known(null), known(100)];
  assert.deepEqual(rows.slice().sort(ctx.compareUsageDetailRows).map(r => r.ai_credits), [100, 9, null]);
  ctx.usageSortDirection = 'asc';
  assert.deepEqual(rows.slice().sort(ctx.compareUsageDetailRows).map(r => r.ai_credits), [9, 100, null]);
});
