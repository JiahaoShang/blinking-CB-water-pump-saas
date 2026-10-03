(() => {
  const slides = [
    {src:'assets/pump-iso.webp',en:'Isometric CAD view',zh:'等轴 CAD 视图',alt:'CAD-derived isometric view of the pump'},
    {src:'assets/pump-front.webp',en:'Front CAD view',zh:'正视 CAD 视图',alt:'CAD-derived front view of the pump'},
    {src:'assets/pump-side.webp',en:'Side CAD view',zh:'侧视 CAD 视图',alt:'CAD-derived side view of the pump'},
    {src:'assets/pump-top.webp',en:'Top CAD view',zh:'俯视 CAD 视图',alt:'CAD-derived top view of the pump'},
    {src:'assets/pump-drawing.webp',en:'2D drawing preview',zh:'2D 图纸预览',alt:'Internal 2D drawing preview of the pump'}
  ];
  const $ = selector => document.querySelector(selector);
  let lang = 'en', slide = 0, zoom = 1, panX = 0, panY = 0, dragging = false, dragX = 0, dragY = 0, swipeX = null;
  const image = $('#main-image'), stage = $('#image-stage');

  function setLang(next) {
    lang = next;
    document.documentElement.lang = next === 'zh' ? 'zh-CN' : 'en';
    document.querySelectorAll('[data-zh]').forEach(el => {
      if (el.dataset.en === undefined) el.dataset.en = el.textContent;
      el.textContent = next === 'zh' ? el.dataset.zh : el.dataset.en;
    });
    document.querySelectorAll('[data-zh-placeholder]').forEach(el => {
      if (el.dataset.enPlaceholder === undefined) el.dataset.enPlaceholder = el.placeholder;
      el.placeholder = next === 'zh' ? el.dataset.zhPlaceholder : el.dataset.enPlaceholder;
    });
    $('#lang-toggle').textContent = next === 'zh' ? 'English' : '中文';
    $('#image-caption').textContent = slides[slide][next];
    $('#menu-toggle').setAttribute('aria-label', next === 'zh' ? '打开导航菜单' : 'Open navigation');
  }

  function transformImage() {
    image.style.transform = `translate(${panX}px,${panY}px) scale(${zoom})`;
    $('#zoom-level').textContent = `${Math.round(zoom * 100)}%`;
    stage.classList.toggle('is-zoomed', zoom > 1);
  }
  function setZoom(value) {
    zoom = Math.max(1, Math.min(3, Math.round(value * 10) / 10));
    if (zoom === 1) panX = panY = 0;
    transformImage();
  }
  function renderGallery() {
    image.src = slides[slide].src;
    image.alt = slides[slide].alt;
    $('#gallery-count').textContent = `${String(slide + 1).padStart(2, '0')} / ${String(slides.length).padStart(2, '0')}`;
    $('#image-caption').textContent = slides[slide][lang];
    $('#gallery-prev').disabled = slide === 0;
    $('#gallery-next').disabled = slide === slides.length - 1;
    $('#thumbs').innerHTML = `<button type="button" class="thumb-scroll" data-thumb-step="-1" aria-label="Previous thumbnail view" ${slide === 0 ? 'disabled' : ''}>↑</button>` + slides.map((item, i) => `<button type="button" data-slide="${i}" class="${i === slide ? 'active' : ''}" aria-label="View image ${i + 1}" aria-current="${i === slide}"><img src="${item.src}" alt=""></button>`).join('') + `<button type="button" class="thumb-scroll" data-thumb-step="1" aria-label="Next thumbnail view" ${slide === slides.length - 1 ? 'disabled' : ''}>↓</button>`;
    $('#gallery-dots').innerHTML = slides.map((item, i) => `<button type="button" data-slide="${i}" class="${i === slide ? 'active' : ''}" aria-label="View image ${i + 1}" aria-current="${i === slide}"></button>`).join('');
    setZoom(1);
  }
  function changeSlide(next) {
    if (next < 0 || next >= slides.length) return;
    slide = next;
    renderGallery();
  }
  $('#gallery-prev').addEventListener('click', () => changeSlide(slide - 1));
  $('#gallery-next').addEventListener('click', () => changeSlide(slide + 1));
  document.addEventListener('click', event => {
    const button = event.target.closest('[data-slide]');
    if (button) changeSlide(Number(button.dataset.slide));
    const thumbStep = event.target.closest('[data-thumb-step]');
    if (thumbStep) changeSlide(slide + Number(thumbStep.dataset.thumbStep));
    const model = event.target.closest('[data-select-model]');
    if (model) {
      $('#rfq-model').value = model.dataset.selectModel;
      $('#inquiry').scrollIntoView({behavior:'smooth'});
      $('#rfq-model').focus({preventScroll:true});
    }
  });
  document.addEventListener('keydown', event => {
    if (['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)) return;
    if (event.key === 'ArrowRight') changeSlide(slide + 1);
    if (event.key === 'ArrowLeft') changeSlide(slide - 1);
  });
  $('#zoom-in').addEventListener('click', () => setZoom(zoom + .5));
  $('#zoom-out').addEventListener('click', () => setZoom(zoom - .5));
  stage.addEventListener('pointerdown', event => {
    if (event.target.closest('button')) return;
    if (zoom === 1) { swipeX = event.clientX; return; }
    dragging = true;
    dragX = event.clientX - panX;
    dragY = event.clientY - panY;
    stage.setPointerCapture(event.pointerId);
    stage.classList.add('is-dragging');
  });
  stage.addEventListener('pointermove', event => {
    if (!dragging) return;
    panX = Math.max(-stage.clientWidth * (zoom - 1) / 2, Math.min(stage.clientWidth * (zoom - 1) / 2, event.clientX - dragX));
    panY = Math.max(-stage.clientHeight * (zoom - 1) / 2, Math.min(stage.clientHeight * (zoom - 1) / 2, event.clientY - dragY));
    transformImage();
  });
  function endDrag(event) {
    if (swipeX !== null && event.pointerType === 'touch' && Math.abs(event.clientX - swipeX) > 45) changeSlide(slide + (event.clientX < swipeX ? 1 : -1));
    swipeX = null;
    dragging = false;
    stage.classList.remove('is-dragging');
  }
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);

  const mobileMenu = $('#mobile-menu'), shade = $('#menu-shade'), toggle = $('#menu-toggle');
  function setMenu(open) {
    mobileMenu.hidden = !open;
    shade.hidden = !open;
    toggle.setAttribute('aria-expanded', String(open));
  }
  toggle.addEventListener('click', () => setMenu(mobileMenu.hidden));
  shade.addEventListener('click', () => setMenu(false));
  mobileMenu.addEventListener('click', event => { if (event.target.closest('a')) setMenu(false); });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') setMenu(false); });

  const anchors = [...document.querySelectorAll('.anchor-nav a')];
  const sections = anchors.map(anchor => document.querySelector(anchor.getAttribute('href')));
  const observer = new IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting).sort((a,b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    anchors.forEach(anchor => anchor.classList.toggle('active', anchor.getAttribute('href') === `#${visible.target.id}`));
  }, {rootMargin:'-30% 0px -55% 0px',threshold:[0,.1,.25,.5]});
  sections.forEach(section => observer.observe(section));

  let toastTimeout;
  function toast(message) {
    let box = $('#demo-toast');
    if (!box) { box = document.createElement('div'); box.id = 'demo-toast'; box.setAttribute('role','status'); document.body.append(box); }
    box.textContent = message;
    box.style.cssText = 'position:fixed;bottom:76px;right:18px;max-width:290px;background:#152c32;color:#fff;border-radius:6px;padding:14px 17px;font-size:12px;line-height:1.5;box-shadow:0 8px 25px #11282a50;z-index:40';
    clearTimeout(toastTimeout);
    toastTimeout = setTimeout(() => box.remove(), 4000);
  }
  ['#whatsapp-hero','#whatsapp-mobile'].forEach(selector => $(selector).addEventListener('click', () => toast(lang === 'zh' ? '演示企业尚未配置 WhatsApp 联系方式；请使用页内询盘表单。' : 'WhatsApp is not configured for this demo seller. Use the inquiry form below.')));

  const form = $('#inquiry-form'), feedback = $('#form-feedback');
  form.addEventListener('submit', event => {
    event.preventDefault();
    feedback.className = 'form-feedback';
    if (!form.checkValidity()) {
      form.reportValidity();
      feedback.classList.add('error');
      feedback.textContent = lang === 'zh' ? '请检查必填项、邮箱格式与隐私确认；已填写内容仍保留。' : 'Check required fields, email format and privacy consent. Your entries remain in place.';
      return;
    }
    feedback.classList.add('success');
    feedback.textContent = lang === 'zh' ? '原型校验通过；没有发送、保存或生成真实 RFQ。正式版本将通过统一 API 提交。' : 'Prototype validation passed. Nothing was sent or saved, and no real RFQ was created. The live version will use the shared RFQ API.';
  });
  $('#lang-toggle').addEventListener('click', () => setLang(lang === 'en' ? 'zh' : 'en'));
  renderGallery();
  setLang('en');
})();
