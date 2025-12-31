// ==UserScript==
// @name         Steam Workshop 取消订阅·固定池轮转（server/local兼容）+ 错误页面处理 + 单页面模式
// @namespace    local.bulk-unsub
// @version      16.0.0
// @description  仅当 URL 含 #bulk_unsub=1：自动退订并通过 cb 或本机端口拉取下一条；兼容服务器 /unsub/next 与本机 127.0.0.1:8787。遇到错误页面时使用API直接取消订阅。支持单页面批量模式（避免速率限制）。
// @match        https://steamcommunity.com/sharedfiles/filedetails/*
// @run-at       document-end
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==

(function () {
  'use strict';
  if (!/(\b|#|&)bulk[_-]?unsub=1\b/i.test(location.hash||'')) return;

  function parseHashParam(name){
    const h = (location.hash||'').replace(/^#/, '');
    const m = h.split('&').map(s=>s.trim()).filter(Boolean).map(kv=>kv.split('='))
      .reduce((acc,[k,v])=>{ if(k) acc[decodeURIComponent(k)]=decodeURIComponent(v||''); return acc; },{});
    return m[name];
  }
  const API_NEXT = (function(){
    const cb = parseHashParam('cb');
    return cb && /^https?:\/\//i.test(cb) ? cb : 'http://127.0.0.1:8787/next';
  })();

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

  // 读取sessionid
  function get_sessionid(){
    let sessionid = '';
    const steam_cookie = document.cookie.split("; ").map(a => a.split("="));
    steam_cookie.forEach(element => {
      if(element[0] == "sessionid"){
        sessionid = element[1];
      }
    });
    return sessionid;
  }

  // 直接通过API取消订阅（用于错误页面）
  async function unsubscribeByAPI(id, appid){
    const sessionid = get_sessionid();
    if (!sessionid) {
      console.warn('无法获取sessionid，无法通过API取消订阅');
      return false;
    }

    return new Promise((resolve, reject) => {
      // 构建表单数据字符串（兼容旧环境）
      function encodeURIComponentSafe(s) {
        return encodeURIComponent(String(s || ''));
      }
      const data = 'id=' + encodeURIComponentSafe(id) +
                   '&appid=' + encodeURIComponentSafe(appid) +
                   '&sessionid=' + encodeURIComponentSafe(sessionid);

      GM_xmlhttpRequest({
        method: 'POST',
        url: 'https://steamcommunity.com/sharedfiles/unsubscribe',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded'
        },
        data: data,
        onload: function(res) {
          if (res.status === 200) {
            resolve(true);
          } else {
            reject(new Error(`HTTP error! status: ${res.status}`));
          }
        },
        onerror: function(err) {
          reject(new Error("Network error: " + err));
        }
      });
    });
  }

  // 全局变量：单页面模式标记
  let SINGLE_PAGE_MODE = false;
  let PROCESSING = false;

  const goNext = (()=> {
    let sent=false;
    return function(status, callback){
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
          
          // 检测是否是单页面模式：返回了 id 而不是 url
          if (data && data.id && !data.url) {
            SINGLE_PAGE_MODE = true;
            console.log('[单页面模式] 检测到单页面模式，接收到新ID:', data.id);
            sent = false; // 重置标志，允许下次调用
            if (callback) callback(data.id);
            return;
          }
          
          // 多页面模式：跳转到新 URL
          const next = data && data.url;
          if (next) {
            console.log('[多页面模式] 跳转到下一个页面:', next);
            location.assign(next);
          } else {
            console.log('[完成] 队列已空，所有项目处理完毕');
            if (callback) callback(null);
          }
        },
        onerror: function(err){
          console.error('请求下一个项目失败:', err);
          sent = false;
          if (callback) callback(null);
        }
      });
    };
  })();

  function isErrorOrGonePage(){
    const title = (document.title || '').toLowerCase();
    if (/错误|error/.test(title)) return true;
    const bodyText = ((D.body && D.body.innerText) || '').toLowerCase();
    const hit = [
      '未找到','不存在','已被删除','已被移除','无法使用','无效的物品','您请求的项目',
      '处理您的请求时遇到错误','查看物品时出现错误','请稍后再试',
      'not found','no longer available','has been removed','was removed','invalid item',
      'an error occurred while processing your request','there was an error viewing this item','please try again later'
    ].some(w => bodyText.includes(w.toLowerCase()));
    if (hit) return true;
    if (D.querySelector('.error_ctn, .error, #error_box, .pagecontent .error')) return true;
    if (!(getAdd() || getToggle())) {
      const len = bodyText.length;
      if (len>0 && len<200 && /error|错误|not\s*found|removed/i.test(bodyText)) return true;
    }
    return false;
  }

  function findGateBtn(){
    const wanted = ['查看物品','查看该物品','继续浏览','继续','我已年满18岁','confirm','view item','view content','continue','enter'];
    const sel = [
      'a.btn_blue_steamui','button.btn_blue_steamui','.btn_blue_steamui a',
      'a.btn_green_steamui','button.btn_green_steamui','.btn_green_steamui a',
      'input[type="submit"]','button[type="submit"]'
    ].join(',');
    let btn = Array.from(D.querySelectorAll(sel)).find(el=>{
      if (!isVis(el)) return false;
      const t = norm(txt(el));
      return t && wanted.some(w=> t.includes(norm(w)));
    });
    if (btn) return btn;
    btn = Array.from(D.querySelectorAll('a,button')).find(el=>{
      if (!isVis(el)) return false;
      const t = norm(txt(el));
      return t && wanted.some(w=> t.includes(norm(w)));
    });
    return btn || null;
  }
  function click(el){
    try{
      const r=el.getBoundingClientRect();
      const cx=Math.floor(r.left+r.width/2), cy=Math.floor(r.top+r.height/2);
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

  function ensureGatePassed(onReady){
    if (isErrorOrGonePage()) {
      // 当检测到错误页面时，尝试通过API直接取消订阅
      const id = getWorkshopId();
      const appid = getAppId();
      if (id && appid) {
        unsubscribeByAPI(id, appid)
          .then(() => {
            console.log('已通过API取消订阅错误页面的物品:', id);
            goNext('api_unsub_error');
          })
          .catch((err) => {
            console.error('通过API取消订阅失败:', err);
            goNext('api_unsub_error_failed');
          });
      } else {
        console.warn('无法获取id或appid，无法取消订阅');
        goNext('gone');
      }
      return;
    }
    if (getAdd() || getToggle()) { onReady(); return; }
    const mo = new MutationObserver(()=>{
      if (isErrorOrGonePage()) {
        // 当检测到错误页面时，尝试通过API直接取消订阅
        const id = getWorkshopId();
        const appid = getAppId();
        if (id && appid) {
          try{mo.disconnect();}catch(_){ }
          unsubscribeByAPI(id, appid)
            .then(() => {
              console.log('已通过API取消订阅错误页面的物品:', id);
              goNext('api_unsub_error');
            })
            .catch((err) => {
              console.error('通过API取消订阅失败:', err);
              goNext('api_unsub_error_failed');
            });
          return;
        } else {
          try{mo.disconnect();}catch(_){ }
          goNext('gone');
          return;
        }
      }
      if (getAdd() || getToggle()) { try{mo.disconnect();}catch(_){ } onReady(); return; }
      const g = findGateBtn(); if (g) click(g);
      if (/^#(changenotes|comments|discussions)/i.test(location.hash)) {
        history.replaceState(null,'', location.href.replace(/#.*$/,'') + '#bulk_unsub=1');
      }
    });
    mo.observe(D.documentElement || D.body, {childList:true, subtree:true});
    const g0 = findGateBtn(); if (g0) click(g0);
  }

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

  // 单页面模式：批量处理工作项
  async function processSinglePageMode() {
    if (PROCESSING) return;
    PROCESSING = true;
    
    console.log('[单页面模式] 开始批量处理...');
    
    // 创建进度显示元素
    let progressDiv = null;
    try {
      progressDiv = document.createElement('div');
      progressDiv.id = 'bulk-unsub-progress';
      progressDiv.style.cssText = 'position:fixed;top:10px;right:10px;padding:15px 20px;background:rgba(0,0,0,0.85);color:#fff;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,0.3);z-index:99999;font-family:Arial,sans-serif;font-size:14px;min-width:250px;';
      progressDiv.innerHTML = '<div style="font-weight:bold;margin-bottom:8px;">🔄 批量取消订阅中...</div><div id="progress-text">初始化...</div>';
      document.body.appendChild(progressDiv);
    } catch(e) {
      console.warn('无法创建进度显示:', e);
    }
    
    function updateProgress(text) {
      console.log('[进度]', text);
      if (progressDiv) {
        const textEl = progressDiv.querySelector('#progress-text');
        if (textEl) textEl.textContent = text;
      }
    }
    
    // 首先处理当前页面的 ID（这是第一个打开的页面）
    const currentId = getWorkshopId();
    let idQueue = [];
    let processedCount = 0;
    let failedCount = 0;
    
    if (currentId) {
      console.log('[单页面模式] 首先处理当前页面 ID:', currentId);
      updateProgress('处理当前页面: ' + currentId);
      const appid = getAppId();
      try {
        await unsubscribeByAPI(currentId, appid);
        processedCount++;
        console.log('[单页面模式] ✓ 成功取消订阅当前页面:', currentId);
        updateProgress('✓ 已完成: ' + processedCount + ' | ✗ 失败: ' + failedCount);
        await new Promise(resolve => setTimeout(resolve, 100));
      } catch(err) {
        failedCount++;
        console.error('[单页面模式] ✗ 取消订阅当前页面失败:', currentId, err);
        updateProgress('✓ 已完成: ' + processedCount + ' | ✗ 失败: ' + failedCount);
        await new Promise(resolve => setTimeout(resolve, 300));
      }
    }
    
    // 然后批量获取队列中剩余的 ID
    const BATCH_SIZE = 10;
    
    try {
      updateProgress('正在获取待处理列表...');
      const batchUrl = API_NEXT.replace('/next', '/batch') + '&size=' + BATCH_SIZE;
      const batchPromise = new Promise((resolve, reject) => {
        GM_xmlhttpRequest({
          method: 'GET',
          url: batchUrl,
          onload: function(res) {
            try {
              const data = JSON.parse(res.responseText || '{}');
              if (data.ids && Array.isArray(data.ids)) {
                resolve(data.ids);
              } else {
                resolve([]);
              }
            } catch(e) {
              resolve([]);
            }
          },
          onerror: function() {
            resolve([]);
          }
        });
      });
      
      idQueue = await batchPromise;
      console.log('[单页面模式] 批量获取了', idQueue.length, '个项目');
      updateProgress('已获取 ' + idQueue.length + ' 个项目');
    } catch(e) {
      console.log('[单页面模式] 批量获取失败，将逐个获取');
      updateProgress('批量获取失败，将逐个处理');
    }
    
    // 处理队列中的每个 ID
    async function processNextId() {
      let currentId = null;
      
      // 从批量队列中取ID，如果为空则通过 /next 获取
      if (idQueue.length > 0) {
        currentId = idQueue.shift();
      } else {
        // 尝试重新批量获取
        try {
          const batchUrl = API_NEXT.replace('/next', '/batch') + '&size=' + BATCH_SIZE;
          const batchPromise = new Promise((resolve, reject) => {
            GM_xmlhttpRequest({
              method: 'GET',
              url: batchUrl,
              onload: function(res) {
                try {
                  const data = JSON.parse(res.responseText || '{}');
                  if (data.ids && Array.isArray(data.ids)) {
                    resolve(data.ids);
                  } else {
                    resolve([]);
                  }
                } catch(e) {
                  resolve([]);
                }
              },
              onerror: function() {
                resolve([]);
              }
            });
          });
          
          idQueue = await batchPromise;
          console.log('[单页面模式] 重新批量获取了', idQueue.length, '个项目');
          
          if (idQueue.length > 0) {
            currentId = idQueue.shift();
          }
        } catch(e) {
          console.error('[单页面模式] 批量获取失败:', e);
        }
      }
      
      if (!currentId) {
        console.log('[单页面模式] 所有项目处理完毕！');
        console.log('[单页面模式] 统计 - 成功:', processedCount, '失败:', failedCount);
        PROCESSING = false;
        
        // 更新进度显示为完成状态
        if (progressDiv) {
          progressDiv.style.background = processedCount > 0 ? 'rgba(76, 175, 80, 0.95)' : 'rgba(244, 67, 54, 0.95)';
          progressDiv.innerHTML = '<div style="font-weight:bold;margin-bottom:8px;">✓ 批量取消订阅完成</div>' +
            '<div>✓ 成功: ' + processedCount + ' 个</div>' +
            '<div>✗ 失败: ' + failedCount + ' 个</div>' +
            '<div style="margin-top:10px;font-size:12px;opacity:0.9;">5秒后自动关闭</div>';
          setTimeout(() => {
            try { progressDiv.remove(); } catch(e) {}
          }, 5000);
        }
        return;
      }
      
      console.log('[单页面模式] 正在取消订阅:', currentId);
      updateProgress('处理中: ' + currentId + ' (成功: ' + processedCount + ', 失败: ' + failedCount + ')');
      
      // 获取当前的 appid（从页面中）
      const appid = getAppId();
      
      // 通过 API 取消订阅
      try {
        await unsubscribeByAPI(currentId, appid);
        processedCount++;
        console.log('[单页面模式] ✓ 成功取消订阅:', currentId, '(已完成:', processedCount, ')');
        updateProgress('✓ 已完成: ' + processedCount + ' | ✗ 失败: ' + failedCount);
        
        // 延迟一下避免过快
        await new Promise(resolve => setTimeout(resolve, 100));
        
        // 继续处理下一个
        processNextId();
      } catch(err) {
        failedCount++;
        console.error('[单页面模式] ✗ 取消订阅失败:', currentId, err, '(失败数:', failedCount, ')');
        updateProgress('✓ 已完成: ' + processedCount + ' | ✗ 失败: ' + failedCount);
        
        // 失败后也继续处理下一个
        await new Promise(resolve => setTimeout(resolve, 300));
        processNextId();
      }
    }
    
    // 开始处理
    processNextId();
  }

  // 传统多页面模式的处理逻辑
  function processMultiPageMode() {
    ensureGatePassed(function onUIReady(){
      if (getAdd()) { goNext('already'); return; }
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
        watchAddThenFinish('ui_unsub');
        return;
      }
      const t = getToggle();
      if (t){
        click(t);
        watchAddThenFinish('ui_toggle');
        return;
      }

      const mo = new MutationObserver(()=>{
        if (getAdd()){ try{mo.disconnect();}catch(_){ } goNext('already'); }
      });
      mo.observe(D.documentElement||D.body, {childList:true, subtree:true});
    });
  }

  // 初始化：检测模式
  console.log('[初始化] 检测运行模式...');
  
  // 通过尝试访问 /batch 接口来判断是否是单页面模式
  // 检测时不带 key 参数，避免消耗队列（只检测接口是否存在）
  const baseUrl = API_NEXT.split('?')[0].replace('/next', '/batch');
  const detectUrl = baseUrl + '?size=0';
  
  console.log('[初始化] 检测 URL:', detectUrl);
  
  GM_xmlhttpRequest({
    method: 'GET',
    url: detectUrl,
    timeout: 2000,
    onload: function(res) {
      try {
        const data = JSON.parse(res.responseText || '{}');
        if (data.ids && Array.isArray(data.ids)) {
          // 服务器支持 /batch 接口，说明是单页面模式
          SINGLE_PAGE_MODE = true;
          console.log('[初始化] 检测到单页面模式支持');
          processSinglePageMode();
          return;
        }
      } catch(e) {}
      
      // 不支持 /batch 接口，使用传统多页面模式
      console.log('[初始化] 使用传统多页面模式');
      processMultiPageMode();
    },
    onerror: function() {
      // 请求失败，可能服务器不支持 /batch，使用传统模式
      console.log('[初始化] 检测失败，使用传统多页面模式');
      processMultiPageMode();
    },
    ontimeout: function() {
      // 超时，使用传统模式
      console.log('[初始化] 检测超时，使用传统多页面模式');
      processMultiPageMode();
    }
  });
})();

