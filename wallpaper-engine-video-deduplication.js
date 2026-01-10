// ==UserScript==
// @name         Steam Workshop 取消订阅·固定池轮转（server/local兼容）+ 错误页面处理 + 单页面模式
// @namespace    local.bulk-unsub
// @version      19.0.1
// @description  仅当 URL 含 #bulk_unsub=1：自动退订并通过 cb 或本机端口拉取下一条；兼容服务器 /unsub/next 与本机 127.0.0.1:8787。单页面模式默认极速（fast=1：不验证不重试，只看接口返回），可切换 fast=0 启用慢速验证模式。
// @match        https://steamcommunity.com/sharedfiles/filedetails/*
// @match        https://steamcommunity.com/my/myworkshopfiles*
// @match        https://steamcommunity.com/profiles/*/myworkshopfiles*
// @match        https://steamcommunity.com/id/*/myworkshopfiles*
// @run-at       document-end
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==

(function () {
  'use strict';
  if (!/(\b|#|&)bulk[_-]?unsub=1\b/i.test(location.hash||'')) return;

  function parseHashParam(name){
    // 值里可能包含 '='（例如 cb=http://...?...=...），不能 split('=') 直接拆
    const h = (location.hash||'').replace(/^#/, '');
    const out = {};
    h.split('&').map(s=>s.trim()).filter(Boolean).forEach(kv=>{
      const i = kv.indexOf('=');
      const k = decodeURIComponent(i>=0 ? kv.slice(0,i) : kv);
      const v = decodeURIComponent(i>=0 ? kv.slice(i+1) : '');
      if (k) out[k] = v;
    });
    return out[name];
  }
  const API_NEXT = (function(){
    const cb = parseHashParam('cb');
    return cb && /^https?:\/\//i.test(cb) ? cb : 'http://127.0.0.1:8787/next';
  })();

  function buildBatchUrl(apiNext, size){
    try{
      const u = new URL(String(apiNext));
      u.pathname = u.pathname.replace(/\/next\/?$/i, "/batch");
      u.searchParams.set("size", String(size));
      return u.toString();
    }catch(_){
      const base = String(apiNext||"").replace("/next", "/batch");
      const sep = base.includes("?") ? "&" : "?";
      return base + sep + "size=" + encodeURIComponent(String(size));
    }
  }

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

  // 默认 appid（Wallpaper Engine）
  const DEFAULT_APPID = (parseHashParam('appid_default') || '').match(/^\d+$/)
    ? String(parseHashParam('appid_default'))
    : '431960';

  // 极速模式：不验证、不重试（只按接口返回判定成功/失败）
  // - 默认开启（fast=1）
  // - 想启用验证/重试：#bulk_unsub=1&fast=0
  const FAST_MODE = /^(1|true|yes)$/i.test(String(parseHashParam('fast') || '1'));

  function normalizeAppId(appid){
    const s = String(appid || '').trim();
    return /^\d+$/.test(s) ? s : DEFAULT_APPID;
  }

  // 同源 XHR（带 cookie），更接近你贴的“别人脚本”的行为
  function xhrText(method, url, body = null, timeoutMs = 10000){
    return new Promise((resolve, reject) => {
      try{
        const xhr = new XMLHttpRequest();
        xhr.open(method, url, true);
        xhr.withCredentials = true;
        xhr.timeout = timeoutMs;
        xhr.onload = () => resolve({status: xhr.status, text: xhr.responseText || ''});
        xhr.onerror = () => reject(new Error('xhr_network_error'));
        xhr.ontimeout = () => reject(new Error('xhr_timeout'));
        xhr.send(body);
      }catch(e){
        reject(e);
      }
    });
  }

  // 订阅状态检测：返回 'subscribed' | 'not_subscribed' | 'unknown'
  async function checkSubscribeState(id) {
    try{
      const {status, text: html} = await xhrText('GET', `https://steamcommunity.com/sharedfiles/filedetails/?id=${id}`, null, 12000);
      if (status !== 200) return 'unknown';

      const hasAdd = /SubscribeItemOptionAdd/i.test(html);
      const hasRemove = /SubscribeItemBtn[^>]*toggled|subscribeOption\s*\.?\s*remove|取消订阅|已订阅/i.test(html);

      // 错误/下架页经常没有按钮，这种情况下无法从详情页可靠判断
      const looksError = /查看物品时出现错误|处理您的请求时遇到错误|not\s+found|no\s+longer\s+available|has\s+been\s+removed/i.test(html);

      if (hasRemove && !hasAdd) return 'subscribed';
      if (hasAdd && !hasRemove) return 'not_subscribed';
      return looksError ? 'unknown' : 'unknown';
    }catch(_){
      return 'unknown';
    }
  }

  // 直接通过API取消订阅（用于错误页面）
  // 返回 { success: boolean, verified: boolean, reason?: string }
  async function unsubscribeByAPI(id, appid, retries = 3){
    const sessionid = get_sessionid();
    if (!sessionid) {
      console.warn('[API] 无法获取sessionid');
      return { success: false, verified: false, reason: 'no_sessionid' };
    }

    const appidFinal = normalizeAppId(appid);

    // 极速模式：只发一次请求，不做任何校验/重试（仅看接口返回）
    if (FAST_MODE) {
      try{
        const formData = new FormData();
        formData.append('id', String(id));
        formData.append('appid', String(appidFinal));
        formData.append('sessionid', String(sessionid));
        const {status, text} = await xhrText('POST', 'https://steamcommunity.com/sharedfiles/unsubscribe', formData, 15000);
        if (status !== 200) return { success: false, verified: false, reason: `http_${status}` };
        let ok = false;
        try{
          const j = JSON.parse(text || '{}');
          ok = (j && (j.success === 1 || j.success === true));
        }catch(_){
          ok = (text === '' || text === '1' || text === 'true');
        }
        return ok
          ? { success: true, verified: false, reason: 'fast' }
          : { success: false, verified: false, reason: 'bad_response' };
      }catch(e){
        return { success: false, verified: false, reason: String(e && e.message || e) };
      }
    }

    for (let attempt = 0; attempt < retries; attempt++) {
      try {
        // 1) 如果能判断“未订阅”，直接跳过（避免浪费请求）
        const state0 = await checkSubscribeState(id);
        if (state0 === 'not_subscribed') {
          console.log('[API] 该项目显示未订阅，跳过:', id);
          return { success: true, verified: true, reason: 'already_not_subscribed' };
        }

        // 2) 调用取消订阅：用 FormData + 同源 XHR（和你贴的脚本一致）
        const formData = new FormData();
        formData.append('id', String(id));
        formData.append('appid', String(appidFinal));
        formData.append('sessionid', String(sessionid));

        const {status, text} = await xhrText('POST', 'https://steamcommunity.com/sharedfiles/unsubscribe', formData, 15000);
        if (status !== 200) throw new Error(`http_${status}`);

        // 兼容多种返回：JSON / "1" / 空串
        let ok = false;
        try{
          const j = JSON.parse(text || '{}');
          ok = (j && (j.success === 1 || j.success === true));
        }catch(_){
          ok = (text === '' || text === '1' || text === 'true');
        }
        if (!ok) throw new Error('bad_response');

        // 3) 等待 Steam 侧写入（对已下架物品可能更慢）
        await new Promise(resolve => setTimeout(resolve, 2500));

        // 4) 验证：如果无法可靠判断（unknown），就重试到次数用尽
        const state1 = await checkSubscribeState(id);
        if (state1 === 'not_subscribed') {
          console.log('[API] ✓ 取消订阅成功并已验证:', id);
          return { success: true, verified: true };
        }
        if (state1 === 'unknown') {
          console.warn('[API] ⚠ 无法从详情页可靠验证（可能是错误/下架页）:', id, '(尝试', attempt + 1, '/', retries, ')');
          if (attempt < retries - 1) {
            await new Promise(resolve => setTimeout(resolve, 3000));
            continue;
          }
          // 最后一次仍无法验证：标记未验证（提醒你走“订阅列表页清理”）
          return { success: true, verified: false, reason: 'verify_unknown' };
        }

        // subscribed
        console.warn('[API] ⚠ API成功但仍显示已订阅:', id, '(尝试', attempt + 1, '/', retries, ')');
        if (attempt < retries - 1) {
          await new Promise(resolve => setTimeout(resolve, 3000));
          continue;
        }
        console.error('[API] ✗ 验证失败，该项目可能仍占用配额!', id);
        return { success: true, verified: false, reason: 'still_subscribed' };

      } catch(err) {
        console.warn(`[API] 重试 ${attempt + 1}/${retries} 失败:`, id, err.message);
        if (attempt < retries - 1) {
          await new Promise(resolve => setTimeout(resolve, 1000 * (attempt + 1)));
        } else {
          return { success: false, verified: false, reason: String(err && err.message || err) };
        }
      }
    }

    return { success: false, verified: false, reason: 'exhausted' };
  }

  // 全局变量：单页面模式标记
  let SINGLE_PAGE_MODE = false;
  let PROCESSING = false;

  // ========== WebUI 投递队列（postMessage） ==========
  // WebUI 会在「已订阅物品主页」postMessage {type:'bulk_unsub_add', ids:[...], reqId}
  const MSG_QUEUE = { seen:new Set(), ids:[], ok:0, fail:0, ui:null };
  // reqId -> { source, pending:Set<id>, ok:number, fail:number, okIds:string[], failIds:string[] }
  const REQS = new Map();
  const SEEN_REQIDS = new Set();
  function isLikelyId(x){ return /^\d+$/.test(String(x||'').trim()); }
  function enqueueIds(ids){
    const arr = Array.isArray(ids) ? ids : [];
    let added = 0;
    const addedIds = [];
    for (const raw of arr){
      const s = String(raw||'').trim();
      if (!isLikelyId(s)) continue;
      if (MSG_QUEUE.seen.has(s)) continue;
      MSG_QUEUE.seen.add(s);
      MSG_QUEUE.ids.push(s);
      addedIds.push(s);
      added++;
    }
    return { added, addedIds };
  }
  function ensureMsgUI(){
    if (MSG_QUEUE.ui) return MSG_QUEUE.ui;
    const div = document.createElement('div');
    div.id = 'bulk-unsub-msg-progress';
    div.style.cssText = 'position:fixed;top:10px;right:10px;padding:15px 20px;background:rgba(0,0,0,0.85);color:#fff;border-radius:8px;box-shadow:0 4px 12px rgba(0,0,0,0.3);z-index:99999;font-family:Arial,sans-serif;font-size:14px;min-width:260px;';
    div.innerHTML = '<div style="font-weight:bold;margin-bottom:8px;">🔄 订阅主页批量取消订阅</div><div id="bulk-unsub-msg-text">等待任务…</div>';
    try{ document.body.appendChild(div); }catch(_){}
    MSG_QUEUE.ui = div;
    return div;
  }
  function setMsgText(text){
    ensureMsgUI();
    const el = MSG_QUEUE.ui && MSG_QUEUE.ui.querySelector('#bulk-unsub-msg-text');
    if (el) el.textContent = String(text||'');
  }
  async function processMsgQueue(){
    if (PROCESSING) return;
    PROCESSING = true;
    try{
      ensureMsgUI();
      while (MSG_QUEUE.ids.length){
        const id = MSG_QUEUE.ids.shift();
        setMsgText(`处理中: ${id} (成功:${MSG_QUEUE.ok} 失败:${MSG_QUEUE.fail} 队列:${MSG_QUEUE.ids.length})`);
        const r = await unsubscribeByAPI(id, DEFAULT_APPID, FAST_MODE ? 1 : 3);
        const success = !!(r && r.success);
        if (success) MSG_QUEUE.ok++; else MSG_QUEUE.fail++;

        // 逐个 ID 更新各 reqId 的完成度，全部完成后给 opener 回执 bulk_unsub_done
        for (const [reqId, st] of REQS.entries()){
          if (!st || !st.pending || !st.pending.has(id)) continue;
          st.pending.delete(id);
          if (success){ st.ok++; st.okIds.push(id); } else { st.fail++; st.failIds.push(id); }
          if (st.pending.size === 0){
            try{
              if (st.source && typeof st.source.postMessage === 'function'){
                st.source.postMessage({ type:'bulk_unsub_done', reqId, ok: st.ok, fail: st.fail, okIds: st.okIds, failIds: st.failIds }, '*');
              }
            }catch(_){}
            REQS.delete(reqId);
          }
        }
        await new Promise(res=>setTimeout(res, 120));
      }
      setMsgText(`完成/等待新任务… (成功:${MSG_QUEUE.ok} 失败:${MSG_QUEUE.fail})`);
    } finally {
      PROCESSING = false;
    }
  }
  window.addEventListener('message', (ev)=>{
    const d = ev && ev.data;
    if (!d || d.type !== 'bulk_unsub_add') return;
    const reqId = String(d.reqId || '');

    // reqId 去重：WebUI 会在收到 ack 前重试 postMessage，这里只处理一次
    if (reqId && SEEN_REQIDS.has(reqId)){
      try{
        if (ev.source && typeof ev.source.postMessage === 'function'){
          ev.source.postMessage({ type:'bulk_unsub_ack', reqId: d.reqId, added: 0 }, '*');
        }
      }catch(_){}
      return;
    }

    const { added, addedIds } = enqueueIds(d.ids);
    if (reqId) SEEN_REQIDS.add(reqId);

    // 为该 reqId 建立 pending 追踪：只追踪“本次真正入队”的那些 id
    if (reqId){
      const pending = new Set(addedIds);
      REQS.set(reqId, { source: ev.source, pending, ok:0, fail:0, okIds:[], failIds:[] });
      if (pending.size === 0){
        // 没有新增任务：立刻回 done，避免 WebUI 等待
        try{
          if (ev.source && typeof ev.source.postMessage === 'function'){
            ev.source.postMessage({ type:'bulk_unsub_done', reqId, ok:0, fail:0, okIds:[], failIds:[] }, '*');
          }
        }catch(_){}
        REQS.delete(reqId);
      }
    }
    try{
      if (ev.source && typeof ev.source.postMessage === 'function'){
        ev.source.postMessage({ type:'bulk_unsub_ack', reqId: d.reqId, added }, '*');
      }
    }catch(_){}
    if (added > 0){
      setMsgText(`收到任务: +${added} (队列:${MSG_QUEUE.ids.length})`);
      processMsgQueue();
    } else {
      ensureMsgUI();
    }
  });

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
          unsubscribeByAPI(id, appid, FAST_MODE ? 1 : 3)
          .then((result) => {
            if (result.success) {
              console.log('[错误页面] ✓ 已请求取消订阅:', id);
              goNext('api_unsub_ok');
            } else {
              console.error('[错误页面] ✗ 取消订阅失败:', id, result.reason || '');
              goNext('api_unsub_fail');
            }
          })
          .catch((err) => {
            console.error('[错误页面] 异常:', err);
            goNext('api_unsub_error');
          });
      } else {
        console.warn('[错误页面] 无法获取id或appid');
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
          unsubscribeByAPI(id, appid, FAST_MODE ? 1 : 3)
            .then((result) => {
              if (result.success) {
                console.log('[错误页面-MO] ✓ 已请求取消订阅:', id);
                goNext('api_unsub_ok');
              } else {
                console.error('[错误页面-MO] ✗ 取消订阅失败:', id, result.reason || '');
                goNext('api_unsub_fail');
              }
            })
            .catch((err) => {
              console.error('[错误页面-MO] 异常:', err);
              goNext('api_unsub_error');
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
    // 极速模式只统计成功/失败

    if (currentId) {
      console.log('[单页面模式] 首先处理当前页面 ID:', currentId);
      updateProgress('处理当前页面: ' + currentId);
      const appid = getAppId();
      try {
        const result = await unsubscribeByAPI(currentId, appid, FAST_MODE ? 1 : 3);
        if (result.success) {
          processedCount++;
          console.log('[单页面模式] ✓ 成功:', currentId);
        } else {
          failedCount++;
          console.error('[单页面模式] ✗ 失败:', currentId, result.reason || '');
        }
        updateProgress('✓' + processedCount + ' | ✗' + failedCount);
        await new Promise(resolve => setTimeout(resolve, FAST_MODE ? 40 : 500));
      } catch(err) {
        failedCount++;
        console.error('[单页面模式] ✗ 异常:', currentId, err);
        updateProgress('✓' + processedCount + ' | ✗' + failedCount);
        await new Promise(resolve => setTimeout(resolve, FAST_MODE ? 120 : 500));
      }
    }

    // 然后批量获取队列中剩余的 ID
    // 极速模式：尽量一次多拿，减少 /batch 往返
    // 非极速模式：小批量，减少被限流概率
    const BATCH_SIZE = FAST_MODE ? 30 : 5;

    try {
      updateProgress('正在获取待处理列表...');
      const batchUrl = buildBatchUrl(API_NEXT, BATCH_SIZE);
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
          const batchUrl = buildBatchUrl(API_NEXT, BATCH_SIZE);
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
        console.log('[单页面模式] =====================================');
        console.log('[单页面模式] 所有项目处理完毕！');
        console.log('[单页面模式] ✓ 成功:', processedCount);
        console.log('[单页面模式] ✗ 完全失败:', failedCount);
        console.log('[单页面模式] =====================================');

        PROCESSING = false;

        // 更新进度显示为完成状态
        if (progressDiv) {
          progressDiv.style.background = processedCount > 0 ? 'rgba(76, 175, 80, 0.95)' : 'rgba(244, 67, 54, 0.95)';
          progressDiv.innerHTML = '<div style="font-weight:bold;margin-bottom:8px;">' +
            ('✓ 批量取消订阅完成') +
            '</div>' +
            '<div>✓ 成功: ' + processedCount + ' 个</div>' +
            '<div>✗ 失败: ' + failedCount + ' 个</div>' +
            '<div style="margin-top:10px;font-size:12px;opacity:0.9;">5秒后自动关闭</div>';
          setTimeout(() => {
            try { progressDiv.remove(); } catch(e) {}
          }, 5000);
        }
        return;
      }

      console.log('[单页面模式] 正在处理 ID:', currentId);
      updateProgress('处理: ' + currentId + ' (✓' + processedCount + ' | ✗' + failedCount + ')');

      // 获取当前的 appid（从页面中）
      const appid = getAppId();

      // 通过 API 取消订阅（极速模式：无验证无重试）
      try {
        const result = await unsubscribeByAPI(currentId, appid, FAST_MODE ? 1 : 3);
        if (result.success) {
          processedCount++;
          console.log('[单页面模式] ✓ 成功:', currentId);
        } else {
          failedCount++;
          console.error('[单页面模式] ✗ 失败:', currentId, result.reason || '');
        }
        updateProgress('✓' + processedCount + ' | ✗' + failedCount);
        await new Promise(resolve => setTimeout(resolve, FAST_MODE ? 40 : 500));
        processNextId();
      } catch(err) {
        failedCount++;
        console.error('[单页面模式] ✗ 完全失败:', currentId, err);
        updateProgress('✓' + processedCount + ' | ✗' + failedCount);
        await new Promise(resolve => setTimeout(resolve, FAST_MODE ? 120 : 2000));
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
  const IS_SUBS_PAGE = /\/myworkshopfiles/i.test(location.pathname || "");

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
      if (IS_SUBS_PAGE) {
        console.log('[初始化] 订阅主页未检测到 /batch，进入等待任务模式（postMessage）');
        ensureMsgUI(); setMsgText('等待任务…');
        return;
      }
      console.log('[初始化] 使用传统多页面模式');
      processMultiPageMode();
    },
    onerror: function() {
      // 请求失败，可能服务器不支持 /batch，使用传统模式
      if (IS_SUBS_PAGE) {
        console.log('[初始化] 订阅主页检测失败，进入等待任务模式（postMessage）');
        ensureMsgUI(); setMsgText('等待任务…');
        return;
      }
      console.log('[初始化] 检测失败，使用传统多页面模式');
      processMultiPageMode();
    },
    ontimeout: function() {
      // 超时，使用传统模式
      if (IS_SUBS_PAGE) {
        console.log('[初始化] 订阅主页检测超时，进入等待任务模式（postMessage）');
        ensureMsgUI(); setMsgText('等待任务…');
        return;
      }
      console.log('[初始化] 检测超时，使用传统多页面模式');
      processMultiPageMode();
    }
  });
})();

