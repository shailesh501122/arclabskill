// Render frames of scene.html with headless Chromium (WebGL via SwiftShader).
// usage: node render_frames.js <dir-with-scene.html> <outDir> <startFrame> <endFrame> <fps>
const { chromium } = require('playwright');
const http = require('http'), fs = require('fs'), path = require('path');
const [dir, outDir, a, b, fps] = process.argv.slice(2);
(async () => {
  const srv = http.createServer((q, s) => {
    const f = path.join(dir, decodeURIComponent(q.url.split('?')[0]));
    if (!fs.existsSync(f)) { s.statusCode = 404; return s.end(); }
    s.setHeader('Content-Type', f.endsWith('.js') ? 'text/javascript' : 'text/html');
    fs.createReadStream(f).pipe(s);
  }).listen(0);
  const port = srv.address().port;
  const br = await chromium.launch({ args: ['--use-gl=angle', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
  const pg = await br.newPage({ viewport: { width: 1080, height: 1920 } });
  pg.on('pageerror', e => { console.error('PAGEERROR', e.message); process.exit(2); });
  await pg.goto(`http://localhost:${port}/scene.html`);
  await pg.waitForFunction('window.ready === true', null, { timeout: 60000 });
  const canvas = pg.locator('#c');
  const info = [];
  for (let i = +a; i < +b; i++) {
    const r = await pg.evaluate(t => window.renderAt(t), i / +fps);
    info.push([i, r.tilt]);
    await canvas.screenshot({ path: path.join(outDir, `f${String(i).padStart(5, '0')}.jpg`), type: 'jpeg', quality: 93 });
  }
  fs.writeFileSync(path.join(outDir, `info_${a}.json`), JSON.stringify(info));
  await br.close(); srv.close();
})();
