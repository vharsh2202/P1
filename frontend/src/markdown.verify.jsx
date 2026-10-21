/**
 * Renders the chat UI's markdown pipeline server-side and asserts on the
 * resulting HTML. This proves two things without needing a browser:
 *
 *   1. GFM tables and bullet lists render into real markup.
 *   2. The renderer is actually SAFE - raw HTML and javascript: URLs in the
 *      payload never become live DOM.
 *
 * Run with:  npm run verify:markdown
 */

import { renderToStaticMarkup } from 'react-dom/server';
import { Markdown } from './App.jsx';

let failures = 0;

function check(name, condition, detail) {
  const ok = Boolean(condition);
  if (!ok) failures += 1;
  console.log(`  [${ok ? 'ok  ' : 'FAIL'}] ${name}${ok ? '' : `  <-- ${detail}`}`);
}

const render = (md) => renderToStaticMarkup(<Markdown>{md}</Markdown>);

console.log('MARKDOWN RENDERER VERIFICATION');
console.log('='.repeat(60));

// --- 1. GFM table ---------------------------------------------------------
const tableMd = `| Region | Revenue | Share % |
| --- | --- | --- |
| North America | $257,702.21 | 42.0% |
| EMEA | $219,546.24 | 35.8% |`;

const tableHtml = render(tableMd);
console.log(`\n  table html: ${tableHtml.slice(0, 220)}...\n`);
check('renders a <table>', tableHtml.includes('<table'), 'no <table> in output');
check('renders <th> header cells', tableHtml.includes('<th'), 'no <th> in output');
check('renders <td> body cells', tableHtml.includes('<td'), 'no <td> in output');
check('keeps the $ formatting', tableHtml.includes('$257,702.21'), 'cell content lost');
check('wraps table in .table-scroll', tableHtml.includes('table-scroll'), 'scroll wrapper missing');

// --- 2. Bullet list -------------------------------------------------------
const bulletsHtml = render('- **Total revenue:** $613,708.25\n- **Orders:** 1,427');
check('renders <ul>', bulletsHtml.includes('<ul'), 'no <ul> in output');
check('renders <li>', bulletsHtml.includes('<li'), 'no <li> in output');
check('renders <strong>', bulletsHtml.includes('<strong'), 'no <strong> in output');

// --- 3. Raw HTML must NOT become live markup -----------------------------
const xssHtml = render('<script>alert(1)</script><img src=x onerror="alert(2)">');
console.log(`\n  xss html:  ${xssHtml}\n`);
check('no <script> element', !xssHtml.includes('<script'), 'script tag survived');
check('no onerror handler', !xssHtml.toLowerCase().includes('onerror'), 'event handler survived');
check('no <img> element', !xssHtml.toLowerCase().includes('<img'), 'img tag survived');

// --- 4. javascript: URLs must be stripped ---------------------------------
const linkHtml = render('[click me](javascript:alert(3))');
console.log(`  link html: ${linkHtml}\n`);
check(
  'no javascript: URL',
  !linkHtml.toLowerCase().includes('javascript:'),
  'javascript: URL survived',
);
check('link opens safely', linkHtml.includes('rel="noopener noreferrer"'), 'missing rel=noopener');

// --- 5. A normal link is preserved ---------------------------------------
const safeLinkHtml = render('[docs](https://example.com)');
check('keeps safe href', safeLinkHtml.includes('href="https://example.com"'), 'href lost');
check('adds target=_blank', safeLinkHtml.includes('target="_blank"'), 'target missing');

// --- 6. Code fence / inline code -----------------------------------------
const codeHtml = render('Use `uvicorn main:app` to start.');
check('renders inline code', codeHtml.includes('inline-code'), 'inline code class missing');

console.log('\n' + '='.repeat(60));
console.log(failures === 0 ? 'RESULT: PASS' : `RESULT: FAIL (${failures} failure(s))`);
process.exit(failures === 0 ? 0 : 1);
