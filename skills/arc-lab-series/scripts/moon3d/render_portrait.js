// node render_portrait.js <dir> <out.png> <pose> <t> '<framing json>'
const { chromium } = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path');
const [dir, out, poseName, t, framing] = process.argv.slice(2);
(async () => {
  const srv = http.createServer((q, s) => { const f = path.join(dir, q.url.split('?')[0]);
    if (!fs.existsSync(f)) { s.statusCode = 404; return s.end(); }
    s.setHeader('Content-Type', f.endsWith('.js') ? 'text/javascript' : 'text/html'); fs.createReadStream(f).pipe(s); }).listen(0);
  const br = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const pg = await br.newPage({ viewport: { width: 1080, height: 1920 } });
  pg.on('pageerror', e => { console.error(e.message); process.exit(2); });
  await pg.goto(`http://localhost:${srv.address().port}/scene.html`);
  await pg.waitForFunction('window.ready === true');
  await pg.evaluate(([p, tt, f]) => window.renderPortrait(p, tt, JSON.parse(f)), [poseName, +t, framing]);
  const url = await pg.evaluate(() => document.getElementById('c').toDataURL('image/png'));
  fs.writeFileSync(out, Buffer.from(url.split(',')[1], 'base64'));
  await br.close(); srv.close();
})();
