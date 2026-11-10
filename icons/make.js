// Значки aspentiv для кнопок бота: белая линейная иконка в круге с розово-синим градиентом, 100×100 PNG.
// node make.js outdir
const { chromium } = require('playwright');
const fs = require('fs');
const out = process.argv[2];
fs.mkdirSync(out, { recursive: true });
const S = 'stroke="#fff" stroke-width="7" stroke-linecap="round" stroke-linejoin="round" fill="none"';
const F = 'fill="#fff"';
// key: внутренность иконки (координаты 0..100, круг-фон рисуется отдельно)
const ICONS = {
  buy:      `<path ${S} d="M26 30h8l7 30h28l6-22H38"/><circle ${F} cx="45" cy="70" r="5"/><circle ${F} cx="66" cy="70" r="5"/>`,
  plus:     `<path ${S} d="M50 28v44M28 50h44"/>`,
  help:     `<path ${S} d="M38 38a12 12 0 1 1 18 10c-4 3-6 5-6 10"/><circle ${F} cx="50" cy="70" r="4.5"/>`,
  cabinet:  `<circle ${S} cx="50" cy="40" r="12"/><path ${S} d="M28 74c3-12 12-18 22-18s19 6 22 18"/>`,
  configs:  `<path ${S} d="M36 26h20l12 12v36H36z"/><path ${S} d="M56 26v12h12M43 52h18M43 62h14"/>`,
  balance:  `<rect ${S} x="26" y="34" width="48" height="34" rx="7"/><path ${S} d="M26 44h48"/><circle ${F} cx="62" cy="57" r="4.5"/>`,
  news:     `<path ${S} d="M50 24l6 16 16 6-16 6-6 16-6-16-16-6 16-6z"/>`,
  gift:     `<rect ${S} x="28" y="44" width="44" height="30" rx="4"/><path ${S} d="M24 36h52v8H24zM50 36v38M50 36c-4-10-16-12-16-4s12 4 16 4c4 0 16 4 16-4s-12-6-16 4"/>`,
  support:  `<circle ${S} cx="50" cy="50" r="22"/><circle ${S} cx="50" cy="50" r="9"/><path ${S} d="M35 35l9 9M65 35l-9 9M35 65l9-9M65 65l-9-9"/>`,
  clear:    `<path ${S} d="M60 26L44 52M34 52h22l4 22H30z"/><path ${S} d="M38 62v8M46 62v8"/>`,
  back:     `<path ${S} d="M58 30L38 50l20 20"/>`,
  download: `<path ${S} d="M50 26v34M36 46l14 14 14-14M30 72h40"/>`,
  renew:    `<path ${S} d="M70 44a21 21 0 0 0-38-8M30 56a21 21 0 0 0 38 8"/><path ${S} d="M32 24v12h12M68 76V64H56"/>`,
  delete:   `<path ${S} d="M30 34h40M44 34v-6h12v6M35 34l3 40h24l3-40M46 46v18M54 46v18"/>`,
  close:    `<path ${S} d="M34 34l32 32M66 34L34 66"/>`,
  video:    `<path ${F} d="M40 30l32 20-32 20z"/>`,
  friends:  `<circle ${S} cx="40" cy="40" r="9"/><circle ${S} cx="62" cy="42" r="7"/><path ${S} d="M24 72c2-10 9-15 16-15s14 5 16 15M60 56c8 0 14 5 16 14"/>`,
  mode:     `<path ${S} d="M26 36h12c10 0 14 28 26 28h10M26 64h12c4 0 7-4 9-8M64 36h10M68 28l8 8-8 8M68 56l8 8-8 8"/>`,
  ping:     `<path ${S} d="M22 52h14l7-16 10 30 8-20 5 6h12"/>`,
  key:      `<circle ${S} cx="38" cy="50" r="12"/><path ${S} d="M50 50h26M66 50v10M74 50v8"/>`,
  qr:       `<rect ${S} x="28" y="28" width="16" height="16" rx="2"/><rect ${S} x="56" y="28" width="16" height="16" rx="2"/><rect ${S} x="28" y="56" width="16" height="16" rx="2"/><path ${S} d="M58 58h4M70 58v4M58 70h14"/>`,
  ok:       `<path ${S} d="M30 52l13 13 27-30"/>`,
  phone:    `<rect ${S} x="36" y="24" width="28" height="52" rx="6"/><path ${S} d="M46 66h8"/>`,
  android:  `<path ${S} d="M32 58a18 18 0 0 1 36 0zM38 36l-5-7M62 36l5-7M32 58v12h36V58"/><circle ${F} cx="43" cy="49" r="3.5"/><circle ${F} cx="57" cy="49" r="3.5"/>`,
  pc:       `<rect ${S} x="24" y="30" width="52" height="34" rx="4"/><path ${S} d="M42 74h16M50 64v10"/>`,
  router:   `<rect ${S} x="26" y="56" width="48" height="16" rx="4"/><path ${S} d="M36 64h4M38 46a16 16 0 0 1 24 0M32 38a26 26 0 0 1 36 0"/>`,
  shield:   `<path ${S} d="M50 24l22 8v16c0 14-9 24-22 28-13-4-22-14-22-28V32z"/>`,
  edit:     `<path ${S} d="M30 70l4-14 26-26 10 10-26 26zM56 34l10 10"/>`,
  box:      `<path ${S} d="M50 24l24 12v28L50 76 26 64V36zM26 36l24 12 24-12M50 48v28"/>`,
  card:     `<rect ${S} x="24" y="32" width="52" height="36" rx="6"/><path ${S} d="M24 44h52M34 58h12"/>`,
  bolt:     `<path ${F} d="M56 22L32 54h16l-4 24 24-32H52z"/>`,
  game:     `<rect ${S} x="22" y="36" width="56" height="30" rx="15"/><path ${S} d="M36 45v12M30 51h12"/><circle ${F} cx="60" cy="47" r="3.5"/><circle ${F} cx="67" cy="55" r="3.5"/>`,
  signal:   `<path ${S} d="M32 70v-8M44 70V54M56 70V44M68 70V32"/>`,
  bell:     `<path ${S} d="M36 64V48a14 14 0 0 1 28 0v16l5 6H31zM46 74a5 5 0 0 0 8 0"/>`,
  chart:    `<path ${S} d="M28 72V28M28 72h46M38 62l10-12 8 6 14-18"/>`,
  ban:      `<circle ${S} cx="50" cy="50" r="22"/><path ${S} d="M35 35l30 30"/>`,
  copy:     `<rect ${S} x="38" y="38" width="30" height="34" rx="5"/><path ${S} d="M30 60V32a4 4 0 0 1 4-4h24"/>`,
  hand:     `<path ${S} d="M40 72V40a5 5 0 0 1 10 0v14M50 50v-6a5 5 0 0 1 10 0v12M60 52a5 5 0 0 1 10 0v6c0 10-6 16-16 16H50c-6 0-10-4-14-10l-6-10a5 5 0 0 1 8-5l2 3"/>`,
  lock:     `<rect ${S} x="30" y="46" width="40" height="28" rx="6"/><path ${S} d="M38 46v-8a12 12 0 0 1 24 0v8"/>`,
};
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 100, height: 100 }, deviceScaleFactor: 1 });
  for (const [k, inner] of Object.entries(ICONS)) {
    const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" viewBox="0 0 100 100">
      <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#ff2d87"/><stop offset="1" stop-color="#2d6bff"/></linearGradient></defs>
      <circle cx="50" cy="50" r="48" fill="url(#g)"/>${inner}</svg>`;
    await p.setContent(`<html><body style="margin:0;background:transparent">${svg}</body></html>`);
    await p.screenshot({ path: `${out}/${k}.png`, omitBackground: true, clip: { x: 0, y: 0, width: 100, height: 100 } });
  }
  await b.close();
  console.log('значков:', Object.keys(ICONS).length);
})();
