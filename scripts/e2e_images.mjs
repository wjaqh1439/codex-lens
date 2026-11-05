const task = globalThis.browserTask, page = task.page('p1');
const fs = await import('node:fs/promises');
const results = [];
function must(ok, msg) { if (!ok) throw new Error(msg); results.push(msg); }
async function wait(fn) { await page.waitForFunction(fn, undefined, { timeout: 15000 }); }
async function paste() {
  await page.evaluate(() => {
    const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='), c => c.charCodeAt(0));
    const dt = new DataTransfer(); dt.items.add(new File([bytes], 'clipboard.png', {type: 'image/png'}));
    document.querySelector('.composer textarea').dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
  });
  await wait(() => document.querySelectorAll('.image-preview').length === 1);
}
try {
  await page.cdp('Emulation.setDeviceMetricsOverride', {width:412,height:915,deviceScaleFactor:1,mobile:true});
  await page.goto(BASE);
  await wait(() => !!document.querySelector('.srow'));
  await page.click('css=.srow >> nth=0');
  await wait(() => !!document.querySelector('.composer textarea'));
  const session = await page.url();
  await paste();
  must(await page.evaluate(() => document.querySelector('.image-preview img').complete), '붙여넣은 이미지 미리보기');
  await page.goto(BASE);
  await page.goto(session);
  await wait(() => !!document.querySelector('.image-preview'));
  await page.reload();
  await wait(() => !!document.querySelector('.image-preview'));
  must(true, '화면 이동·새로고침 후 이미지 초안 복원');
  await page.click('css=.image-preview button');
  must(await page.evaluate(() => !document.querySelector('.image-preview')), '첨부 삭제');
  await paste();
  // 실제 API 오류가 나도 첨부가 남는다.
  await page.evaluate(() => { window.originalFetch = window.fetch; window.fetch = (u, o) => /\/turns$/.test(u) ? Promise.resolve(new Response(JSON.stringify({error:'test failure'}), {status:500})) : window.originalFetch(u,o); });
  await page.click('css=.composer-bar .primary');
  await wait(() => document.querySelector('.composer-hint').textContent.includes('보내지 못했습니다'));
  must(await page.evaluate(() => !!document.querySelector('.image-preview')), '실패 후 첨부 유지');
  await page.evaluate(() => { window.fetch = window.originalFetch; });
  await page.click('css=.composer-bar .primary');
  await wait(() => !document.querySelector('.image-preview') && !!document.querySelector('.request-image'));
  must(true, '이미지 단독 전송 및 요청 이미지 표시');
  must(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), '412px 가로 넘침 없음');
  await page.goto(BASE + '#/new');
  await wait(() => !!document.querySelector('.composer textarea'));
  await paste();
  // 새 세션 이미지 단독 전송도 동일한 경로를 사용한다.
  await page.evaluate((proj) => { const input = document.querySelector('#view input:not([type=checkbox]):not([type=file])'); input.value=proj; input.dispatchEvent(new Event('input',{bubbles:true})); }, PROJ);
  await page.click('css=.composer-bar .primary');
  await wait(() => /#\/s\//.test(location.hash) && !!document.querySelector('.request-image'));
  must(true, '새 세션 이미지 단독 시작');
  await wait(() => !!document.querySelector('.turn .ans .md') && document.querySelector('.turn .ans .md').textContent.length > 0);
  await page.reload();
  await wait(() => !!document.querySelector('.request-image'));
  must(true, '완료 후 다시 읽어도 이미지 유지');
  await fs.writeFile(OUT + '/result.json', JSON.stringify({ok:true,results},null,2));
  console.log(JSON.stringify({ok:true,results}));
} catch(e) {
  await fs.writeFile(OUT+'/result.json',JSON.stringify({ok:false,results,error:String(e.stack||e)}));
  throw e;
} finally { await task.finish({keep:[]}); }
