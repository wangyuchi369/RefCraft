'use strict';

(() => {
  const data = window.REFCRAFT;
  if (!data) return;
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHTML = value => String(value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  $('#abstract-text').textContent = data.abstract;
  for (const author of data.authors || []) {
    const link = $(`[data-author="${author.id}"]`);
    if (!link || !/^https:\/\//.test(author.url)) continue;
    link.href = author.url;
    link.title = author.label;
    link.setAttribute('aria-label', author.label);
  }
  for (const [key, url] of Object.entries(data.config)) {
    if (!url || !/^https:\/\//.test(url)) continue;
    const placeholder = $(`[data-resource="${key}"]`);
    if (!placeholder) continue;
    const link = document.createElement('a');
    link.className = 'resource'; link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    link.innerHTML = placeholder.innerHTML; $('small', link)?.remove(); placeholder.replaceWith(link);
  }

  function initExamples() {
    if (!$('#example-tabs') || !$('#example-panel') || !$('#demo-dialog') || !data.examples?.length) return;
    const shortNames = {280:'Air quality',35:'Seismic observatory',125:'Clothing collection',273:'Mini-game arcade'};
    const interactions = {
      280:['Switch the pollutant layers.', 'Move the time slider and watch the map.', 'Select a station to inspect its readings.'],
      35:['Switch between Chinese and English.', 'Filter earthquake records by magnitude.', 'Select a table row to locate it on the map.'],
      125:['Filter the grid by a colour tag, then clear the filters.', 'Open a product to see its reverse view.', 'Choose a size inside the product dialog.'],
      273:['Start the sliding puzzle and make a legal move.', 'Play the signal game and hit the target.', 'Search and sort the weekly ranking.']
    };
    const tabs = $('#example-tabs');
    const demoDialog = $('#demo-dialog');
    const exampleFromHash = () => data.examples.findIndex(example => location.hash === `#example-${example.id}`);
    const setExampleHash = index => {const hash=`#example-${data.examples[index].id}`;if(location.hash !== hash) history.replaceState(null,'',hash);};
    let activeExample = 0, device = window.innerWidth < 650 ? 'mobile' : 'desktop', actualSize = false;
    let liveResize, frameObserver, downloadURL;
    tabs.innerHTML = data.examples.map((example, index) => `<button type="button" role="tab" id="case-tab-${example.id}" aria-controls="example-panel" aria-selected="${index === 0}" tabindex="${index === 0 ? 0 : -1}"><span class="gallery-preview"><img src="${escapeHTML(example.output.src)}" alt="${escapeHTML(example.output.alt)}" loading="lazy"><span class="gallery-open">Try this webpage ↓</span></span><span class="gallery-label"><small>Task ${String(example.id).padStart(3,'0')}</small><b>${escapeHTML(shortNames[example.id])}</b><span>${example.referenceCount} references · ${example.language.toUpperCase()}</span></span></button>`).join('');

    function decodeDemo(example) {
      const binary = atob(example.demo.base64);
      return Uint8Array.from(binary, char => char.charCodeAt(0));
    }
    function resizeDemo() {
      const stage = $('#demo-stage'), canvas = $('#demo-canvas'), frame = $('#demo-frame');
      if (!stage || !frame) return;
      const width = device === 'mobile' ? 390 : 1440, height = device === 'mobile' ? 844 : 900;
      const chromeHeight = $('.demo-dialog-heading').offsetHeight + $('.live-toolbar').offsetHeight + $('.live-footer').offsetHeight;
      const availableHeight = demoDialog.matches(':modal') ? Math.max(180, window.innerHeight - chromeHeight - 38) : 660;
      const scale = actualSize ? 1 : Math.min(stage.clientWidth / width, availableHeight / height, 1);
      canvas.style.width = `${width * scale}px`; canvas.style.height = `${height * scale}px`;
      frame.style.width = `${width}px`; frame.style.height = `${height}px`; frame.style.transform = `scale(${scale})`;
      stage.style.height = `${Math.min(height * scale, availableHeight)}px`;
      stage.classList.toggle('actual-size',actualSize);
      $$('#live-browser [data-device]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.device === device)));
      $('#demo-size').textContent = `${width} × ${height}`;
      $('#demo-scale').textContent = actualSize ? 'Fit' : '100%';
      $('#demo-scale').setAttribute('aria-label',actualSize ? 'Fit webpage to the available space' : 'Show webpage at 100 percent size');
      $('#demo-scale').setAttribute('aria-pressed',String(actualSize));
    }
    function startDemo() {
      const frame = $('#demo-frame');
      if (!frame || frame.dataset.started) return;
      frame.dataset.started = 'true';
      const example = data.examples[activeExample];
      const originalHTML = new TextDecoder().decode(decodeDemo(example));
      // Only the viewer gets this keyboard bridge; downloads retain the original bytes.
      const escapeBridge = '<script>window.addEventListener("keydown",function(event){if(event.key==="Escape"&&!event.defaultPrevented&&!document.querySelector("dialog:modal")){parent.postMessage({type:"refcraft:demo-escape"},"*");}});</scr' + 'ipt>';
      frame.srcdoc = originalHTML.replace(/<head\b[^>]*>/i, head => head + escapeBridge);
      $('#demo-loading').hidden = false;
      frame.addEventListener('load',() => {$('#demo-loading').hidden = true;},{once:true});
    }
    function renderExample(index, startImmediately = false) {
      activeExample = index;
      liveResize?.disconnect(); frameObserver?.disconnect();
      if(downloadURL) URL.revokeObjectURL(downloadURL);
      const example = data.examples[index];
      $('#demo-dialog-title').textContent = `Task ${example.id} · ${example.title}`;
      const current = $(`#case-tab-${example.id}`);
      $$('button',tabs).forEach(button => {const selected = button === current;button.setAttribute('aria-selected',String(selected));button.tabIndex = selected ? 0 : -1;});
      document.body.append(demoDialog);
      const panel = $('#example-panel'); panel.setAttribute('aria-labelledby',current.id);
      panel.innerHTML = `<div class="case-heading"><h3>Task ${example.id} · ${escapeHTML(example.title)} <a class="case-permalink" href="#example-${example.id}" aria-label="Link to Task ${example.id}">#</a></h3><span>${escapeHTML(example.output.model)}${example.id === 280 ? ' · recorded rerun' : ' · benchmark artifact'}</span></div>
        <div class="example-workspace"><div id="live-home"><div id="live-browser">
          <div class="live-toolbar"><span class="live-title"><i></i>Live webpage</span><div class="viewport-controls" role="group" aria-label="Example viewport"><button type="button" data-device="desktop">Desktop</button><button type="button" data-device="mobile">Mobile</button><button type="button" id="demo-scale" aria-pressed="false">100%</button></div><button type="button" id="expand-demo">Expand ↗</button></div>
          <div id="demo-stage" class="demo-stage" tabindex="0" aria-label="Interactive generated webpage; use 100 percent size to inspect details"><div id="demo-canvas" class="demo-canvas"><iframe id="demo-frame" title="Interactive example: ${escapeHTML(example.title)}" sandbox="allow-scripts allow-forms" referrerpolicy="no-referrer"></iframe></div><span id="demo-loading">Loading example…</span></div>
          <div class="live-footer"><span id="demo-size"></span><span class="live-help">Click and interact directly</span><button id="reset-demo" type="button">↻ Reset</button><a id="download-demo" download="refcraft-task-${example.id}.html">HTML ↓</a></div>
        </div></div><aside class="case-inspector"><h4>Try it yourself</h4><ol>${(interactions[example.id] || []).map(text => `<li>${escapeHTML(text)}</li>`).join('')}</ol><h4>Input references <span>click to inspect</span></h4><div class="references-grid">${example.references.map(ref => `<button type="button" class="reference-image" data-enlarge="${escapeHTML(ref.src)}" data-title="${escapeHTML(ref.alt)}" aria-label="Enlarge ${escapeHTML(ref.alt)}"><img src="${escapeHTML(ref.src)}" alt="${escapeHTML(ref.alt)}" loading="lazy"><span class="image-label">${escapeHTML(ref.label)} · ${escapeHTML(ref.caption)}</span></button>`).join('')}</div><p class="reference-credit"><a href="credits.html">Reference credits</a></p></aside></div>
        <div class="case-brief"><p><b>The task.</b> ${escapeHTML(example.summary)}</p><details><summary>Read the original instruction</summary><pre tabindex="0" lang="${example.language}">${escapeHTML(example.query)}</pre></details></div>
        <p class="case-caption">${example.id === 280 ? 'Task 280 is a separately recorded OpenCode rerun, outside the frozen leaderboard run.' : 'Original generated HTML from the frozen benchmark.'} ${example.id === 96 ? 'Saved items last for the current demo session.' : example.id === 35 || example.id === 280 ? 'Displayed environmental data are simulated.' : 'Bookings are simulated.'} These selected artifacts are qualitative examples.</p>`;
      $('#demo-dialog-body').replaceChildren($('#live-browser'));
      $('#live-home').append(demoDialog);demoDialog.setAttribute('open','');
      downloadURL = URL.createObjectURL(new Blob([decodeDemo(example)], {type:'text/html;charset=utf-8'}));
      $('#download-demo').href = downloadURL;
      $$('[data-device]',panel).forEach(button => button.addEventListener('click',() => {device=button.dataset.device;resizeDemo();}));
      $('#demo-scale').addEventListener('click',() => {actualSize=!actualSize;resizeDemo();});
      $('#reset-demo').addEventListener('click',() => {const frame=$('#demo-frame');delete frame.dataset.started;startDemo();});
      $('#expand-demo').addEventListener('click',() => {
        if(demoDialog.matches(':modal')) {demoDialog.close();return;}
        $('#demo-dialog-title').textContent = `Task ${example.id} · ${example.title}`;
        $('#expand-demo').textContent = 'Restore ↙';
        demoDialog.removeAttribute('open');demoDialog.showModal(); document.body.classList.add('modal-open');
        resizeDemo();startDemo();
      });
      liveResize = new ResizeObserver(resizeDemo); liveResize.observe($('#live-browser'));
      resizeDemo();
      if(startImmediately || !('IntersectionObserver' in window)) startDemo();
      else {frameObserver = new IntersectionObserver(entries => {if(entries.some(entry=>entry.isIntersecting)) {startDemo();frameObserver.disconnect();}},{rootMargin:'200px'});frameObserver.observe(panel);}
    }
    $('#close-demo').addEventListener('click',() => demoDialog.close());
    window.addEventListener('message',event => {
      if(event.source === $('#demo-frame')?.contentWindow && event.data?.type === 'refcraft:demo-escape' && demoDialog.matches(':modal')) demoDialog.close();
    });
    demoDialog.addEventListener('close',() => {
      demoDialog.setAttribute('open','');$('#expand-demo').textContent='Expand ↗';
      document.body.classList.remove('modal-open');resizeDemo();$('#expand-demo').focus({preventScroll:true});
    });
    window.addEventListener('resize',resizeDemo);
    tabs.addEventListener('click', event => {const button = event.target.closest('button');if(button) {const index=$$('button',tabs).indexOf(button);renderExample(index,true);setExampleHash(index);}});
    tabs.addEventListener('keydown', event => {
      let next;
      if(event.key === 'ArrowRight') next = (activeExample + 1) % data.examples.length;
      if(event.key === 'ArrowLeft') next = (activeExample + data.examples.length - 1) % data.examples.length;
      if(event.key === 'Home') next = 0;
      if(event.key === 'End') next = data.examples.length - 1;
      if(next !== undefined) {event.preventDefault();renderExample(next,true);setExampleHash(next);$$('button',tabs)[next].focus();}
    });
    const linkedExample = exampleFromHash();
    renderExample(linkedExample >= 0 ? linkedExample : 0, linkedExample >= 0);
    if(linkedExample >= 0) requestAnimationFrame(() => $('#examples').scrollIntoView());
    window.addEventListener('hashchange',() => {const index=exampleFromHash();if(index < 0) return;if(index !== activeExample) renderExample(index,true);$('#examples').scrollIntoView({behavior:'smooth'});});
  }
  initExamples();

  window.RefCraftPareto?.init();
  window.RefCraftLeaderboard?.init();

  const dialog = $('#image-dialog');let returnFocus;
  function setImageZoom(actualSize) {
    $('#image-viewport').classList.toggle('actual-size',actualSize);
    $('#image-fit').setAttribute('aria-pressed',String(!actualSize));
    $('#image-actual').setAttribute('aria-pressed',String(actualSize));
    $('#image-viewport').scrollLeft = 0;$('#image-viewport').scrollTop = 0;
  }
  $('#dialog-image').addEventListener('load',() => {$('#image-viewport').style.setProperty('--image-width',`${$('#dialog-image').naturalWidth}px`);});
  $('#image-fit').addEventListener('click',() => setImageZoom(false));
  $('#image-actual').addEventListener('click',() => {setImageZoom(true);$('#image-viewport').focus({preventScroll:true});});
  document.addEventListener('click', event => {
    const target = event.target.closest('[data-enlarge]');if(!target) return;
    returnFocus = target;$('#image-dialog-title').textContent = target.dataset.title;
    setImageZoom(false);
    $('#dialog-image').src = target.dataset.enlarge;$('#dialog-image').alt = target.dataset.title;
    $('#download-image').href = target.dataset.enlarge;
    $('#download-image').download = target.dataset.enlarge.startsWith('data:') ? 'refcraft-figure.webp' : target.dataset.enlarge.split('/').pop();
    dialog.showModal();dialog.scrollTop = 0;document.body.classList.add('modal-open');
  });
  $('#close-dialog').addEventListener('click', () => dialog.close());
  dialog.addEventListener('click', event => {if(event.target !== dialog) return;const r = dialog.getBoundingClientRect();if(event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) dialog.close();});
  dialog.addEventListener('close', () => {document.body.classList.remove('modal-open');returnFocus?.focus({preventScroll:true});});

  let toastTimer;
  function toast(message) {clearTimeout(toastTimer);$('#toast').textContent = message;$('#toast').classList.add('show');toastTimer = setTimeout(() => $('#toast').classList.remove('show'),3000);}
  $('#copy-citation').addEventListener('click', async () => {
    const text = $('#bibtex').textContent;
    try {
      if(navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const temporary = document.createElement('textarea');temporary.value = text;temporary.style.cssText = 'position:fixed;left:-9999px;top:0';document.body.append(temporary);temporary.select();
        const copied = document.execCommand('copy');temporary.remove();if(!copied) throw new Error('Copy unavailable');$('#copy-citation').focus({preventScroll:true});
      }
      toast('Citation copied');$('#copy-citation').textContent = 'Copied';setTimeout(() => {$('#copy-citation').textContent = 'Copy citation';},2500);
    } catch {
      const range = document.createRange();range.selectNodeContents($('#bibtex'));const selection = window.getSelection();selection.removeAllRanges();selection.addRange(range);toast('Citation selected. Copy it or download the .bib file.');
    }
  });

  const sections = $$('.site-header nav a').map(link => $(link.hash)).filter(Boolean);
  if('IntersectionObserver' in window) {
    const observer = new IntersectionObserver(entries => {for(const entry of entries) if(entry.isIntersecting) $$('nav a').forEach(link => {if(link.hash === `#${entry.target.id}`) link.setAttribute('aria-current','location');else link.removeAttribute('aria-current');});},{rootMargin:'-10% 0px -65% 0px'});
    sections.forEach(section => observer.observe(section));
  }
})();
