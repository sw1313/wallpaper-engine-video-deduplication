// ==UserScript==
// @name         Steam Workshop 取消订阅·固定池轮转（强门控｜UI-only｜GM_xmlhttpRequest）
// @namespace    local.bulk-unsub
// @version      14.3.0
// @description  仅当 URL 含 #bulk_unsub=1：先检测18+门→过门→待订阅UI出现后仅用UI退订/判定未订阅→捕获DOM或网络成功信号再回参→当前标签跳到下一条；绝不点“订阅”。
// @match        https://steamcommunity.com/sharedfiles/filedetails/*
// @run-at       document-end
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(function () {
  'use strict';
  if (!/(\b|#|&)bulk[_-]?unsub=1\b/i.test(location.hash||'')) return;

  const API_NEXT = 'http://127.0.0.1:8787/next';

  const D = document;
  const isVis = (el)=> !!el && el.getBoundingClientRect().width>0 && el.getBoundingClientRect().height>0;
  const txt   = (el)=> (el && (el.innerText || el.textContent) || '').trim();
  const norm  = (s)=> (s||'').toLowerCase().replace(/\s+/g,'').trim();
  const area  = ()=> D.querySelector('#responsive_page_template_content .workshopItemSubscribeArea')
                  || D.querySelector('.workshopItemSubscribeArea') || D.body;

  const SEL = {
    addItem:      '#SubscribeItemOptionAdd',
    toggleBtn:    '#SubscribeItemBtn, a.btn_green_white_innerfade.btn_border_2px.btn_medium, a.btn_green_steamui, a.btn_blue_steamui, button.btn_green_steamui, button.btn_blue_steamui',
    toggledScope: '#SubscribeItemBtn.toggled, a.btn_green_white_innerfade.btn_border_2px.btn_medium.toggled',
    removeItems:  'span.subscribeText > div.subscribeOption.remove'
  };

  function getAdd(){ const el = D.querySelector(SEL.addItem); return isVis(el)?el:null; }
  function getToggle(){ const el = area().querySelector(SEL.toggleBtn); return isVis(el)?el:null; }
  function findRemovePrecise(){
    const scope = D.querySelector(SEL.toggledScope); if (!scope) return null;
    const all = Array.from(scope.querySelectorAll(SEL.removeItems)).filter(isVis);
    if (!all.length) return null;
    const byLabel = all.find(el=> /取消订阅|unsubscribe/i.test(norm(txt(el))));
    return byLabel || all[all.length-1];
  }

  // —— 强化 18+ 门检测 —— //
  function findGateBtn(){
    // 常见绿色/蓝色按钮、以及“继续/查看/我已满18岁/continue/view item/view content”
    const wanted = ['查看物品','查看该物品','继续浏览','继续','我已年满18岁','confirm','view item','view content','continue','enter'];
    const sel = [
      'a.btn_blue_steamui','button.btn_blue_steamui','.btn_blue_steamui a',
      'a.btn_green_steamui','button.btn_green_steamui','.btn_green_steamui a',
      'input[type="submit"]','button[type="submit"]'
    ].join(',');
    // 也兼容经典 agegate 表单
    let btn = Array.from(D.querySelectorAll(sel)).find(el=>{
      if (!isVis(el)) return false;
      const t = norm(txt(el));
      return t && wanted.some(w=> t.includes(norm(w)));
    });
    if (btn) return btn;
    // 极端：整页只有遮罩上的“继续”链接
    btn = Array.from(D.querySelectorAll('a,button')).find(el=>{
      if (!isVis(el)) return false;
      const t = norm(txt(el));
      return t && wanted.some(w=> t.includes(norm(w)));
    });
    return btn || null;
  }
  function isGateScreen(){
    // 若订阅 UI 已出现则不是门
    if (getAdd() || getToggle()) return false;
    // 有明显年龄表单/提示
    if (D.querySelector('#agegate_box, .agegate_birthday_selector, #app_agegate')) return true;
    // 有“继续/查看”按钮但还没订阅 UI
    if (findGateBtn()) return true;
    return false;
  }
  function click(el){
    try{
      const r = el.getBoundingClientRect();
      const cx = Math.floor(r.left + r.width/2), cy = Math.floor(r.top + r.height/2);
      const common={bubbles:true,cancelable:true,clientX:cx,clientY:cy,button:0,buttons:1,detail:1};
      const p={...common, pointerId:1, isPrimary:true};
      el.dispatchEvent(new PointerEvent('pointermove',p));
      el.dispatchEvent(new PointerEvent('pointerdown',p));
      el.dispatchEvent(new MouseEvent('mousedown',common));
      el.dispatchEvent(new MouseEvent('mouseup',{...common,buttons:0}));
      el.dispatchEvent(new MouseEvent('click',{...common,buttons:0}));
      el.dispatchEvent(new PointerEvent('pointerup',{...p,buttons:0}));
    }catch(e){ try{ el.click(); }catch(_){} }
  }

  // —— 只在确认“过门且订阅UI已现”后，才进入后续逻辑 —— //
  function ensureGatePassed(onReady){
    // 1) 已经有订阅 UI => 直接 ready
    if (getAdd() || getToggle()) { onReady(); return; }

    // 2) 装观察器：门出现→点门；订阅 UI 出现→ready
    const mo = new MutationObserver(()=>{
      if (getAdd() || getToggle()) { try{mo.disconnect();}catch(_){ } onReady(); return; }
      const g = findGateBtn();
      if (g) click(g);
      // 有些门会把 #… 换掉，确保保留触发 hash
      if (/^#(changenotes|comments|discussions)/i.test(location.hash)) {
        history.replaceState(null,'', location.href.replace(/#.*$/,'') + '#bulk_unsub=1');
      }
    });
    mo.observe(D.documentElement || D.body, {childList:true, subtree:true});

    // 3) 初始就有门按钮的话先点一次
    const g0 = findGateBtn();
    if (g0) click(g0);
  }

  // —— 只上报一次 —— //
  const goNext = (()=> {
    let sent=false;
    return function(status){
      if (sent) return; sent=true;
      const payload = {
        slot: getSlot(),
        url: location.href,
        id: getWorkshopId(),
        appid: getAppId(),
        status, ts: Date.now()
      };
      GM_xmlhttpRequest({
        method:'POST',
        url: API_NEXT,
        headers:{'Content-Type':'application/json'},
        data: JSON.stringify(payload),
        onload: function(res){
          let data={}; try{ data=JSON.parse(res.responseText||'{}'); }catch(_){}
          const next = data && data.url;
          if (next) location.assign(next);
        },
        onerror: function(){ /* 后端挂了就留在此页 */ }
      });
    };
  })();

  // —— 退订成功的两个信号：Add 出现 或 /unsubscribe 200 —— //
  (function netProbe(){
    if (window.__bulk_unsub_net_patched) return; window.__bulk_unsub_net_patched = true;
    const of = window.fetch?.bind(window);
    if (of){
      window.fetch = function(res, init){
        const url = (typeof res==='string') ? res : (res && res.url) || '';
        const body = init && init.body ? String(init.body) : '';
        const s = (url + ' ' + body).toLowerCase();
        const isUnsub = /\/sharedfiles\/unsubscribe/.test(s);
        return of(res, init).then(r=>{ if(isUnsub && r && r.status===200) goNext('net_unsub'); return r; });
      };
    }
    const X = window.XMLHttpRequest;
    if (X){
      const open=X.prototype.open, send=X.prototype.send;
      X.prototype.open=function(m,u,...rest){ this.__url=u||''; return open.call(this,m,u,...rest); };
      X.prototype.send=function(b){
        const body=b?String(b):''; const url=String(this.__url||'').toLowerCase();
        const isUnsub=/\/sharedfiles\/unsubscribe/.test(url+' '+body);
        this.addEventListener('loadend',()=>{ if(isUnsub && this.status===200) goNext('net_unsub'); });
        return send.call(this,b);
      };
    }
  })();

  function watchAddThenFinish(statusIfAdd){
    if (getAdd()) { goNext(statusIfAdd); return; }
    const mo = new MutationObserver(()=>{ if(getAdd()){ try{mo.disconnect();}catch(_){ } goNext(statusIfAdd); } });
    mo.observe(D.documentElement||D.body, {childList:true, subtree:true});
  }

  function getWorkshopId(){ const m = location.search.match(/[?&]id=(\d+)/); return m?m[1]:''; }
  function getAppId(){
    const q = location.search.match(/[?&]appid=(\d+)/); if (q) return q[1];
    const map = new Map();
    Array.from(D.querySelectorAll('a[href*="/app/"]')).forEach(a=>{
      const m=(a.getAttribute('href')||'').match(/\/app\/(\d+)\b/); if(m){const k=m[1]; map.set(k,(map.get(k)||0)+1);}
    });
    let best='',cnt=0; map.forEach((v,k)=>{ if(v>cnt){cnt=v; best=k;} }); return best;
  }
  function getSlot(){
    if (!window.name){
      try{ window.name = 'slot-' + (crypto.getRandomValues(new Uint32Array(1))[0]>>>0).toString(36); }
      catch(_){ window.name = 'slot-' + Math.random().toString(36).slice(2); }
    }
    return window.name;
  }

  // —— 主流程（严格门控 + 事件驱动）—— //
  ensureGatePassed(function onUIReady(){
    // A) 未订阅：直接回参要下一条
    if (getAdd()) { goNext('already'); return; }

    // B) 已订阅：优先点“取消订阅”，否则把主按钮当开关；成功信号由 watch/netProbe 捕捉
    //    注意：这里不做任何 sleep
    // 打开一次菜单（悬停 + 轻点）
    (function openMenuOnce(){
      const t = getToggle(); if (!t) return;
      try{
        const r=t.getBoundingClientRect();
        const base={bubbles:true,cancelable:true,clientX:r.left+r.width/2,clientY:r.top+r.height/2,pointerId:1,buttons:0,isPrimary:true};
        t.dispatchEvent(new PointerEvent('pointerover',base));
        t.dispatchEvent(new MouseEvent('mouseover',base));
        t.dispatchEvent(new MouseEvent('mouseenter',base));
        t.dispatchEvent(new MouseEvent('mousemove',base));
        t.dispatchEvent(new PointerEvent('pointerdown',base));
        t.dispatchEvent(new MouseEvent('mousedown', {...base, button:0, buttons:1, detail:1}));
        t.dispatchEvent(new MouseEvent('mouseup',   {...base, button:0, buttons:0, detail:1}));
        t.dispatchEvent(new MouseEvent('click',     {...base, button:0, buttons:0, detail:1}));
        t.dispatchEvent(new PointerEvent('pointerup',base));
      }catch(_){}
    })();

    const rm = findRemovePrecise();
    if (rm){
      click(rm);
      const anc = rm.closest('a,button,[role="button"]'); if (anc && anc!==rm) click(anc);
      watchAddThenFinish('ui_unsub');  // DOM 成功信号
      return;
    }
    const t = getToggle();
    if (t){
      click(t);                        // 主按钮开关
      watchAddThenFinish('ui_toggle'); // DOM 成功信号
      return;
    }

    // C) 极少数 UI 迟到：再观察一次，一旦出现 Add 就收尾
    const mo = new MutationObserver(()=>{
      if (getAdd()){ try{mo.disconnect();}catch(_){ } goNext('already'); }
    });
    mo.observe(D.documentElement||D.body, {childList:true, subtree:true});
  });
})();