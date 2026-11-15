const assert = require('node:assert/strict');
const test = require('node:test');
const install = require('../we_archive_bridge.js');

const archiveFile = 'E:/WE/projects/myprojects/123/中文 视频.mp4';
const nativeProgress = '<radial-progress class="inner radialProgressContainer" rp-percent="wallpaper.downloadpercent" rp-label="wallpaper.downloadlabel"></radial-progress>';
const template = '<a ng-if="currentSelection.status != \'downloadable\'" ' +
    'ng-click="callbackUnsubscribeOrRemove(currentSelection)" class="btn">' +
    "{{'ui_browse_details_btn_subscribed' | translate}}</a>" +
    '<a ng-if="currentSelection.status == \'downloadable\'" ng-click="subscribeWorkshopItem(currentSelection)">Subscribe</a>' + nativeProgress;

function setup(options = {}) {
    let configure;
    let run;
    const calls = [];
    const updates = [];
    const progressEvents = [];
    const messages = [];
    const sizes = new Map([[archiveFile.toLowerCase(), 100]]);
    let response = { wallpapers: [], token: 7, pagecount: 3 };
    const host = {};
    for (const name of ['callDeferred', 'callDeferredSuperseding', 'callDeferredSerialized']) {
        host[name] = async function (object, method, ...args) {
            calls.push({ object, method, args });
            if (method === 'getLocalFileSizes') {
                if (options.sizeRequest) return options.sizeRequest(args[0], sizes);
                return Object.fromEntries(args[0].map(file => [file, sizes.get(file.toLowerCase()) || 0]));
            }
            if (method === 'selectWallpaper' && options.selectRequest) return options.selectRequest();
            if (method === 'subscribeWorkshopItem' && options.subscribeRequest) return options.subscribeRequest();
            return response;
        };
    }
    let patchedTemplate = template;
    const window = {
        setTimeout(callback, delay) {
            const timer = setTimeout(callback, delay);
            if (delay >= 500) timer.unref();
            return timer;
        },
        clearTimeout,
        weArchiveBridgeInstallDir: options.installDirectory,
        weArchiveBridgeArchiveFiles: options.manifest,
        angular: { module: () => ({
            config: definition => { configure = definition.at(-1); },
            run: definition => { run = definition.at(-1); }
        }) },
        ui: { logError() {}, minimize() { calls.push({ method: 'minimize' }); } }
    };
    const api = install(window);
    configure({ decorator: (name, definition) => definition.at(-1)(host) });
    run({ when: Promise.resolve.bind(Promise), all: Promise.all.bind(Promise),
        reject: Promise.reject.bind(Promise), defer() {
            let resolve, reject;
            const promise = new Promise((ok, fail) => { resolve = ok; reject = fail; });
            return { promise, resolve, reject };
        } },
        { open: (name, data) => messages.push(data) },
        { get: () => patchedTemplate, put: (key, value) => { patchedTemplate = value; } },
        { $broadcast: (name, items) => progressEvents.push({ name, items }) });
    const selected = [];
    const watchers = {};
    const ctrl = {
        source: 'workshop', wallpapers: [], sortedWallpapers: [], queryWallpapers: [],
        sortedExploreQueries: [],
        $watchCollection() {},
        $watch(name, callback) { watchers[name] = callback; },
        updateWallpapers(items) {
            updates.push({ items, args: Array.from(arguments), context: this });
            if (options.updateWallpapers) return options.updateWallpapers.apply(this, arguments);
            for (const item of items) {
                if (!item.workshopid) continue;
                for (const existing of [...this.queryWallpapers, ...this.sortedWallpapers]) {
                    if (existing.workshopid === item.workshopid) Object.assign(existing, item);
                }
            }
            this.wallpapers.push(...items.filter(item => item.status === 'installed'));
        },
        callbackWallpaperSelected(item) {
            this.currentSelection = item;
            if (item.status === 'installed') {
                selected.push(item.file);
                host.callDeferred('browseWallpaperObject', 'selectWallpaper', item.file, 'monitor');
            }
        },
        callbackOk(applyAndClose) {
            if (applyAndClose && this.currentSelection?.status === 'installed') {
                return host.callDeferred('browseWallpaperObject', 'acceptAndClose');
            }
        }
    };
    window.browseWallpapersCtrl = ctrl;
    return { window, api, ctrl, host, sizes, calls, messages, selected, updates, progressEvents,
        setSource(source) {
            ctrl.source = source;
            watchers.source(source);
        },
        setResponse: value => { response = value; }, getTemplate: () => patchedTemplate };
}

function archive(file = archiveFile) {
    return { file, project: file.slice(0, file.lastIndexOf('/') + 1) + 'project.json',
        status: 'installed', local: true, type: 'Video', title: '归档', filesize: 100 };
}

function workshop(id = '123') {
    return { workshopid: id, title: '工坊', type: 'Video', status: 'downloadable',
        preview: 'remote.jpg', tags: 'Video,Workshop', local: false };
}

function pendingDownloads(count = 100) {
    return Array.from({ length: count }, (_, i) => ({
        ...workshop(String(1000 + i)), status: 'pending', downloadpercent: 0, downloadlabel: '0%'
    }));
}

test('unsubscribed Workshop items keep their page and author selection during unrelated download progress', async () => {
    const s = setup({ updateWallpapers(items) {
        for (const item of items) {
            const existing = this.wallpapers.find(record => record.workshopid === item.workshopid);
            if (existing) Object.assign(existing, item);
        }
        this.sortedWallpapers = this.sortedWallpapers.slice();
    } });
    const queued = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(queued);
    const item = { ...workshop('unsubscribed'), authorsteamid: 'author-1' };
    const visible = [item, workshop('another-unsubscribed')];
    s.ctrl.queryWallpapers = visible;
    s.ctrl.sortedWallpapers = visible;
    await s.ctrl.callbackWallpaperSelected(item);
    const selected = s.ctrl.currentSelection;
    for (let percent = 1; percent <= 5; percent++) {
        const items = structuredClone(queued);
        Object.assign(items[0], { status: 'downloading', downloadpercent: percent });
        s.ctrl.updateWallpapers(items);
        assert.equal(s.ctrl.sortedWallpapers, visible);
        assert.equal(s.ctrl.currentSelection, selected);
        assert.equal(s.ctrl.wallpapers[0].downloadpercent, percent);
    }
    assert.equal(item.status, 'downloadable');
    assert.equal(item.weArchive, undefined);
    assert.equal(s.api.getStats().archives, 0);
    assert.equal(s.api.getStats().pageRedrawsAvoided, 5);
    assert.equal(s.calls.length, 0);
    const ids = ['unsubscribed'];
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ids);
    assert.equal(s.calls.at(-1).args[0], ids);
});

test('visible downloads keep the native thumbnail redraw and progress update', () => {
    const s = setup({ updateWallpapers(items) {
        for (const item of items) {
            const existing = this.wallpapers.find(record => record.workshopid === item.workshopid);
            if (existing) Object.assign(existing, item);
        }
        this.sortedWallpapers = this.sortedWallpapers.slice();
    } });
    const queued = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(queued);
    s.ctrl.queryWallpapers = [s.ctrl.wallpapers[0]];
    s.ctrl.sortedWallpapers = s.ctrl.queryWallpapers;
    const previous = s.ctrl.sortedWallpapers;
    Object.assign(queued[0], { status: 'downloading', downloadpercent: 42 });
    s.ctrl.updateWallpapers(queued);
    assert.notEqual(s.ctrl.sortedWallpapers, previous);
    assert.equal(s.ctrl.sortedWallpapers[0].downloadpercent, 42);
    assert.equal(s.api.getStats().pageRedrawsAvoided, 0);
});

test('background download optimization preserves native changes to the page content', () => {
    const replacement = workshop('replacement');
    const s = setup({ updateWallpapers() { this.sortedWallpapers = [replacement]; } });
    s.ctrl.queryWallpapers = [workshop('unsubscribed')];
    s.ctrl.sortedWallpapers = s.ctrl.queryWallpapers;
    s.ctrl.updateWallpapers([{ ...workshop('background'), status: 'installed' }]);
    assert.equal(s.ctrl.sortedWallpapers[0], replacement);
    assert.equal(s.api.getStats().pageRedrawsAvoided, 0);
});

test('unchanged bulk download ticks keep page and selection identities while preserving progress events', () => {
    const s = setup();
    const items = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(items);
    s.ctrl.queryWallpapers = [s.ctrl.wallpapers[0]];
    s.ctrl.sortedWallpapers = s.ctrl.queryWallpapers;
    s.ctrl.currentSelection = s.ctrl.queryWallpapers[0];
    const sorted = s.ctrl.sortedWallpapers;
    const selected = s.ctrl.currentSelection;
    s.ctrl.updateWallpapers(items);
    assert.equal(s.updates.length, 0);
    assert.equal(s.ctrl.sortedWallpapers, sorted);
    assert.equal(s.ctrl.currentSelection, selected);
    assert.equal(s.progressEvents[0].name, 'onSteamItemsUpdated');
    assert.equal(s.progressEvents[0].items, items);
    assert.deepEqual(s.api.getStats().downloadUpdates, { received: 100, unchanged: 100 });
});

test('bulk progress, completion, errors, removal and new subscriptions still reach the native update', () => {
    const s = setup();
    const items = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(items);
    items[0] = { ...items[0], status: 'downloading', downloadpercent: 42, downloadlabel: '42%' };
    items[1] = { ...items[1], status: 'installed', file: 'native.mp4' };
    items[2] = { ...items[2], status: 'error' };
    items[3] = { ...items[3], status: 'unsubscribed' };
    items[4] = { ...items[4], workshopid: 'new-subscription' };
    s.ctrl.updateWallpapers(items);
    assert.deepEqual(s.updates[0].items, items.slice(0, 5));
    assert.equal(s.progressEvents[0].items, items);
});

test('partial updates compare every supplied field and preserve native nested merge semantics', () => {
    const s = setup();
    const items = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(items);
    s.ctrl.wallpapers[0].properties = { monitor: { volume: 50, muted: false }, saved: true };
    items[0] = { workshopid: items[0].workshopid, status: 'pending', properties: { monitor: { volume: 50 } } };
    items[1].author = 'updated author';
    items[2].favorite = true;
    s.ctrl.updateWallpapers(items);
    assert.deepEqual(s.updates[0].items, [items[1], items[2]]);
    items[0].properties.monitor.volume = 75;
    s.ctrl.updateWallpapers(items);
    assert(s.updates.at(-1).items.includes(items[0]));
});

test('repeated IDs in one batch retain transition order', () => {
    const s = setup();
    const items = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(items);
    items.unshift({ ...items[0], status: 'downloading', downloadpercent: 50 });
    s.ctrl.updateWallpapers(items);
    assert.deepEqual(s.updates[0].items, items.slice(0, 2));
});

test('bulk filtering keeps local scans, presets, same-object updates and native initialization intact', () => {
    const s = setup();
    const items = pendingDownloads();
    s.ctrl.wallpapers = structuredClone(items);
    items[0] = archive();
    items[1].ispreset = true;
    items[2] = s.ctrl.wallpapers[2];
    s.ctrl.updateWallpapers(items);
    assert.deepEqual(s.updates[0].items, items.slice(0, 3));
    assert.equal(s.api.getStats().archives, 1);
    s.ctrl.updateWallpapers(items, true);
    assert.equal(s.updates.at(-1).items, items);
    assert.equal(s.updates.at(-1).args[1], true);
    assert.equal(s.updates.at(-1).context, s.ctrl);
});

test('discovery pages keep complete native bulk download updates', () => {
    for (const source of ['explore']) {
        const s = setup();
        s.setSource(source);
        const items = pendingDownloads();
        s.ctrl.wallpapers = structuredClone(items);
        s.ctrl.updateWallpapers(items);
        assert.equal(s.updates[0].items, items);
        assert.equal(s.progressEvents.length, 0);
    }
});

test('real two-argument native download ticks are incremental on installed and Workshop pages', () => {
    for (const source of ['installed', 'workshop']) {
        const s = setup();
        s.setSource(source);
        const items = pendingDownloads(1700);
        s.ctrl.wallpapers = structuredClone(items);
        const previousList = s.ctrl.sortedWallpapers;
        s.ctrl.updateWallpapers(items, false);
        assert.equal(s.updates.length, 0);
        assert.equal(s.ctrl.sortedWallpapers, previousList);
        assert.equal(s.progressEvents[0].items, items);
        items[0] = { ...items[0], status: 'downloading', downloadpercent: 42 };
        s.ctrl.updateWallpapers(items, false);
        assert.deepEqual(s.updates[0].items, [items[0]]);
        assert.equal(s.updates[0].args.length, 2);
        assert.equal(s.updates[0].args[1], false);
        assert.equal(s.updates[0].context, s.ctrl);
    }
});

test('real installed-page download ticks preserve unrelated folder thumbnails', () => {
    const s = setup({ updateWallpapers() { this.sortedWallpapers = this.sortedWallpapers.slice(); } });
    s.setSource('installed');
    const local = archive();
    const visible = [local];
    s.ctrl.wallpapers = [local];
    s.ctrl.sortedWallpapers = visible;
    s.ctrl.currentSelection = local;
    s.ctrl.updateWallpapers([{ ...workshop('background'), status: 'downloading', downloadpercent: 5 }], false);
    assert.equal(s.ctrl.sortedWallpapers, visible);
    assert.equal(s.ctrl.currentSelection, local);
    assert.equal(s.api.getStats().pageRedrawsAvoided, 1);
});

test('archive query views keep their identity across native alias reconciliation', () => {
    const s = setup({ updateWallpapers() {} });
    const installed = { ...workshop(), status: 'installed', file: 'workshop.mp4' };
    s.ctrl.wallpapers = [installed];
    s.ctrl.queryWallpapers = [installed];
    s.ctrl.sortedWallpapers = s.ctrl.queryWallpapers;
    s.ctrl.updateWallpapers([archive()]);
    const view = s.ctrl.queryWallpapers[0];
    s.ctrl.currentSelection = view;
    installed.title = 'Native metadata changed';
    s.ctrl.queryWallpapers = [installed];
    s.ctrl.sortedWallpapers = s.ctrl.queryWallpapers;
    s.ctrl.updateWallpapers([]);
    assert.equal(s.ctrl.queryWallpapers[0], view);
    assert.equal(s.ctrl.currentSelection, view);
    assert.equal(view.title, installed.title);
    assert.equal(installed.file, 'workshop.mp4');
    assert.equal(view.file, archiveFile);
});

test('native reconciliation of a selected archive avoids repeated file checks and selection callbacks', async () => {
    let native;
    const s = setup({ updateWallpapers() {
        if (native) this.callbackWallpaperSelected(native);
    } });
    s.ctrl.updateWallpapers([archive()]);
    await s.ctrl.callbackWallpaperSelected(workshop());
    native = { ...workshop(), status: 'installed', file: 'workshop.mp4' };
    const current = s.ctrl.currentSelection;
    s.calls.length = 0;
    s.ctrl.updateWallpapers([]);
    assert.equal(s.ctrl.currentSelection, current);
    assert.equal(s.calls.length, 0);
    await s.ctrl.callbackWallpaperSelected(native);
    assert.equal(s.calls[0].method, 'getLocalFileSizes');
});

test('maps Workshop records while preserving local folder identity and query metadata', async () => {
    const s = setup();
    const local = archive();
    s.ctrl.updateWallpapers([local]);
    const item = workshop();
    s.setResponse({ wallpapers: [item], token: 42, pagecount: 9 });
    const result = await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop', {});
    assert.equal(result.token, 42);
    assert.equal(result.pagecount, 9);
    assert.equal(item.status, 'installed');
    assert.equal(item.file, archiveFile);
    assert.equal(item.workshopid, '123');
    assert.equal(item.preview, 'remote.jpg');
    assert.equal(item.weArchive, true);
    assert.equal(local.local, true);
    assert.equal(local.workshopid, undefined);
    assert.equal(local.weArchive, undefined);
});

test('refreshes a Workshop page opened before the native archive scan finishes', () => {
    const s = setup();
    const item = workshop();
    s.ctrl.queryWallpapers = [item];
    s.ctrl.sortedWallpapers = [item];
    s.ctrl.currentSelection = item;
    s.ctrl.updateWallpapers([archive()]);
    assert.equal(item.status, 'installed');
    assert.equal(item.file, archiveFile);
    assert.equal(s.api.getStats().archives, 1);
});

test('uses the native selection entry point after validating the archived Chinese path', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive()]);
    await s.ctrl.callbackWallpaperSelected(workshop());
    assert.deepEqual(s.selected, [archiveFile]);
    assert.equal(s.calls[0].method, 'getLocalFileSizes');
    assert.equal(s.calls[1].method, 'selectWallpaper');
    assert.equal(s.calls.some(call => call.method === 'subscribeWorkshopItem'), false);
});

test('missing archives restore the query status and offer a recoverable error', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive()]);
    const item = workshop();
    s.ctrl.queryWallpapers = [item];
    s.setResponse({ wallpapers: [item] });
    await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop', {});
    s.sizes.clear();
    await s.ctrl.callbackWallpaperSelected(item);
    assert.equal(item.status, 'downloadable');
    assert.equal(item.weArchive, undefined);
    assert.equal(Object.hasOwn(item, 'file'), false);
    assert.equal(s.selected.length, 0);
    assert.equal(s.messages.length, 1);
    // A later native rescan can restore the reconnected archive.
    s.ctrl.updateWallpapers([archive()]);
    assert.equal(item.weArchive, true);
});

test('prefers myprojects and falls back to a usable backup', async () => {
    const s = setup();
    const backup = 'E:/WE/projects/backup/123/old.mp4';
    s.ctrl.updateWallpapers([archive(backup), archive()]);
    assert.equal(s.api.lookup('123').file, archiveFile);
    s.sizes.clear();
    s.sizes.set(backup.toLowerCase(), 200);
    await s.ctrl.callbackWallpaperSelected(workshop());
    assert.deepEqual(s.selected, [backup]);
    assert.equal(s.messages.length, 0);
});

test('restores a page when the native archive is removed', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive()]);
    const item = workshop();
    s.ctrl.queryWallpapers = [item];
    s.setResponse({ wallpapers: [item] });
    await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop', {});
    s.ctrl.updateWallpapers([{ ...archive(), status: 'unsubscribed' }]);
    assert.equal(item.status, 'downloadable');
    assert.equal(item.weArchive, undefined);
});

test('leaves unrelated records, scenes and Workshop presets on their normal path', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive(), { ...archive('E:/WE/projects/myprojects/456/scene.pkg'), type: 'Scene' }]);
    const preset = { ...workshop(), ispreset: true };
    const scene = { ...workshop('456'), type: 'Scene' };
    const other = workshop('999');
    s.setResponse({ wallpapers: [preset, scene, other] });
    await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop');
    assert.deepEqual([preset.status, scene.status, other.status], Array(3).fill('downloadable'));
    await s.ctrl.callbackWallpaperSelected({ file: 'normal.mp4', status: 'installed' });
    assert.deepEqual(s.selected, ['normal.mp4']);
});

test('latest click wins when archive validation requests overlap', async () => {
    const pending = [];
    const s = setup({ sizeRequest: files => new Promise(resolve => pending.push(() => resolve({ [files[0]]: 100 }))) });
    s.ctrl.updateWallpapers([archive()]);
    const first = s.ctrl.callbackWallpaperSelected(workshop());
    const second = s.ctrl.callbackWallpaperSelected(workshop());
    pending[1]();
    await second;
    pending[0]();
    await first;
    assert.deepEqual(s.selected, [archiveFile]);
});

test('uses the local manifest before the native full-library scan without webpage file probes', async () => {
    const s = setup({ installDirectory: 'E:/WE',
        manifest: [['123', '/projects/myprojects/123/中文 视频.mp4']] });
    const item = workshop();
    s.setResponse({ wallpapers: [item, ...Array.from({ length: 20 }, (_, i) => workshop(String(1000 + i)))] });
    await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop');
    assert.equal(item.status, 'installed');
    assert.equal(item.file, archiveFile);
    assert.deepEqual(s.calls.map(call => call.method), ['queryWorkshop', 'getLocalFileSizes']);
    assert.equal(s.api.getStats().archives, 1);
});

test('rejects missing videos and unsafe paths from the local manifest', async () => {
    for (const file of ['../outside.mp4', 'C:/outside.mp4', '/outside.mp4',
        '/projects/myprojects/123/../outside.mp4', '/projects/myprojects/123/empty.mp4']) {
        const s = setup({ installDirectory: 'E:/WE', manifest: [['123', file]] });
        const item = workshop();
        s.setResponse({ wallpapers: [item] });
        await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop');
        assert.equal(item.status, 'downloadable');
        assert.equal(item.weArchive, undefined);
    }
});

test('adds an explicit archive status to the browser subscription action', () => {
    const s = setup();
    assert(s.getTemplate().includes("{{'ui_browse_details_btn_subscribed' | translate}}"));
    assert(s.getTemplate().includes("source === 'workshop' && currentSelection.weArchive"));
    assert(s.getTemplate().includes('title="{{currentSelection.file}}"'));
    assert.equal(install(s.window), s.api);
});

test('blocks repeat subscriptions for archived IDs and preserves mixed batches', async () => {
    const s = setup();
    s.ctrl.queryWallpapers = [workshop()];
    s.ctrl.updateWallpapers([archive()]);
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123']);
    assert.equal(s.calls.length, 0);
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123', '999']);
    assert.deepEqual(s.calls.at(-1).args[0], ['999']);
});

test('unverified archive index entries allow native subscriptions and native selection paths', async () => {
    const s = setup({ installDirectory: 'E:/WE', manifest: [['123', '/projects/myprojects/123/中文 视频.mp4']] });
    const item = workshop();
    s.ctrl.queryWallpapers = [item];
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123']);
    assert.deepEqual(s.calls[0].args[0], ['123']);
    assert.equal(item.weArchive, undefined);
    assert.equal(item.weSubscriptionRequested, undefined);
    const original = 'E:/SteamLibrary/steamapps/workshop/content/431960/123/original.mp4';
    await s.host.callDeferred('browseWallpaperObject', 'selectWallpaper', original, 'monitor');
    assert.equal(s.calls.at(-1).args[0], original);
});

test('verified archive candidates outside the active archive view allow native subscriptions', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive()]);
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123']);
    assert.deepEqual(s.calls[0].args[0], ['123']);
});

test('pending downloads and native errors retain their original state despite an archive candidate', async () => {
    for (const status of ['pending', 'downloading', 'error']) {
        const s = setup();
        s.ctrl.updateWallpapers([archive()]);
        const item = { ...workshop(), status, downloadpercent: 42 };
        s.ctrl.queryWallpapers = [item];
        s.setResponse({ wallpapers: [item] });
        await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop');
        assert.equal(item.status, status);
        assert.equal(item.downloadpercent, 42);
        assert.equal(item.weArchive, undefined);
        await s.ctrl.callbackWallpaperSelected(item);
        assert.equal(s.calls.some(call => call.method === 'getLocalFileSizes'), false);
        assert.equal(s.selected.length, 0);
        await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123']);
        assert.deepEqual(s.calls.at(-1).args[0], ['123']);
    }
});

test('ordinary subscription requests retain native arguments and allow repeated native calls', async () => {
    const s = setup();
    const timers = [];
    s.window.setTimeout = (...args) => timers.push(args);
    const item = workshop('999');
    const before = structuredClone(item);
    const items = [item];
    s.ctrl.queryWallpapers = items;
    s.ctrl.sortedWallpapers = items;
    s.ctrl.currentSelection = item;
    const ids = ['999'];
    const response = { accepted: true };
    s.setResponse(response);
    assert.equal(await s.host.callDeferred('ui', 'subscribeWorkshopItem', ids), response);
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ids);
    const calls = s.calls.filter(call => call.method === 'subscribeWorkshopItem');
    assert.equal(calls.length, 2);
    assert.equal(calls[0].args[0], ids);
    assert.equal(calls[1].args[0], ids);
    assert.deepEqual(item, before);
    assert.equal(s.ctrl.sortedWallpapers, items);
    assert.equal(timers.length, 0);
    assert.equal(s.calls.some(call => call.method === 'queryWorkshopIds'), false);
});

test('ordinary download states, progress fields and list identity remain native', () => {
    const s = setup();
    const item = workshop('999');
    const items = [item];
    s.ctrl.queryWallpapers = items;
    s.ctrl.sortedWallpapers = items;
    s.ctrl.currentSelection = item;
    for (const status of ['pending', 'downloading', 'installed']) {
        const native = { ...workshop('999'), status, downloadpercent: 42, downloadlabel: '42%' };
        s.ctrl.updateWallpapers([native]);
        assert.deepEqual(item, native);
        assert.equal(s.ctrl.sortedWallpapers, items);
        assert.equal(s.ctrl.currentSelection, item);
    }
});

test('ordinary subscription errors are returned through the native promise', async () => {
    const error = new Error('Native Steam error');
    const s = setup({ subscribeRequest: () => Promise.reject(error) });
    const item = workshop('999');
    s.ctrl.queryWallpapers = [item];
    await assert.rejects(s.host.callDeferred('ui', 'subscribeWorkshopItem', ['999']), value => value === error);
    assert.deepEqual(item, workshop('999'));
});

test('ordinary subscribe markup and native circular progress markup are preserved', () => {
    const s = setup();
    const originalButton = '<a ng-if="currentSelection.status == \'downloadable\'" ng-click="subscribeWorkshopItem(currentSelection)">Subscribe</a>';
    assert(s.getTemplate().includes(originalButton));
    assert(s.getTemplate().includes(nativeProgress));
    assert.equal(s.getTemplate().includes('weSubscriptionRequested'), false);
    assert.equal(s.getTemplate().includes('订阅请求已提交'), false);
    assert.equal(s.getTemplate().includes('等待下载'), false);
});

test('routes native Workshop selection paths through the archive mapping', async () => {
    const s = setup();
    s.ctrl.queryWallpapers = [workshop()];
    s.ctrl.updateWallpapers([archive()]);
    await s.host.callDeferred('browseWallpaperObject', 'selectWallpaper',
        'E:/SteamLibrary/steamapps/workshop/content/431960/123/original.mp4', 'monitor');
    assert.equal(s.calls.at(-1).args[0], archiveFile);
});

test('native downloading selections keep their Workshop path despite a verified archive', async () => {
    for (const status of ['pending', 'downloading', 'error']) {
        const s = setup();
        s.ctrl.updateWallpapers([archive()]);
        s.ctrl.queryWallpapers = [{ ...workshop(), status }];
        const file = 'E:/SteamLibrary/steamapps/workshop/content/431960/123/original.mp4';
        await s.host.callDeferred('browseWallpaperObject', 'selectWallpaper', file, 'monitor');
        assert.equal(s.calls.at(-1).args[0], file);
    }
});

test('switching pages cancels archive validation and pending double click', async () => {
    let validate;
    const s = setup({ sizeRequest: files => new Promise(resolve => {
        validate = () => resolve({ [files[0]]: 100 });
    }) });
    s.ctrl.updateWallpapers([archive()]);
    const click = s.ctrl.callbackWallpaperSelected(workshop());
    const close = s.ctrl.callbackOk(true);
    s.setSource('installed');
    s.setSource('workshop');
    validate();
    await click;
    await close;
    assert.deepEqual(s.selected, []);
    assert.equal(s.calls.some(call => call.method === 'acceptAndClose'), false);
});

test('a delayed archive double click cannot close a later ordinary selection', async () => {
    const timers = [];
    const s = setup();
    s.window.setTimeout = callback => timers.push(callback);
    s.ctrl.updateWallpapers([archive()]);
    await s.ctrl.callbackWallpaperSelected(workshop());
    const close = s.ctrl.callbackOk(true);
    await new Promise(resolve => setImmediate(resolve));
    await s.ctrl.callbackWallpaperSelected({ ...workshop('999'), status: 'installed', file: 'native.mp4' });
    timers[0]();
    await close;
    assert.equal(s.calls.some(call => call.method === 'acceptAndClose'), false);
    assert.equal(s.selected.at(-1), 'native.mp4');
});

test('cancelled archive validation cannot show a missing-file dialog on another page', async () => {
    let validate;
    const s = setup({ sizeRequest: () => new Promise(resolve => { validate = () => resolve({}); }) });
    s.ctrl.updateWallpapers([archive()]);
    const click = s.ctrl.callbackWallpaperSelected(workshop());
    s.setSource('installed');
    validate();
    await click;
    assert.equal(s.messages.length, 0);
    assert.equal(s.api.getStats().archives, 1);
});

test('a Workshop query completing on the installed page leaves its result native', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive()]);
    const item = workshop();
    s.setResponse({ wallpapers: [item] });
    const request = s.host.callDeferred('browseWallpaperObject', 'queryWorkshop');
    s.setSource('installed');
    await request;
    assert.equal(item.weArchive, undefined);
    assert.equal(item.status, 'downloadable');
    assert.equal(s.calls.some(call => call.method === 'getLocalFileSizes'), false);
});

test('applies and closes on double click after archive validation and loading', async () => {
    let completeValidation;
    const s = setup({ sizeRequest: files => new Promise(resolve => {
        completeValidation = () => resolve({ [files[0]]: 100 });
    }) });
    s.ctrl.updateWallpapers([archive()]);
    const click = s.ctrl.callbackWallpaperSelected(workshop());
    const doubleClick = s.ctrl.callbackOk(true);
    assert.equal(s.calls.some(call => call.method === 'acceptAndClose'), false);
    completeValidation();
    await click;
    await doubleClick;
    assert.deepEqual(s.calls.map(call => call.method), ['getLocalFileSizes', 'selectWallpaper', 'acceptAndClose']);
});

test('preserves installed records when WE aliases a Workshop result to the installed list', () => {
    const s = setup();
    const installed = { ...workshop(), status: 'installed', file: 'original.mp4' };
    s.ctrl.wallpapers = [installed];
    s.ctrl.queryWallpapers = [installed];
    s.ctrl.sortedWallpapers = s.ctrl.queryWallpapers;
    s.ctrl.updateWallpapers([archive()]);
    assert.equal(installed.file, 'original.mp4');
    assert.equal(installed.weArchive, undefined);
    assert.notEqual(s.ctrl.queryWallpapers[0], installed);
    assert.equal(s.ctrl.queryWallpapers[0].file, archiveFile);
});

test('keeps the installed and discovery pages on their original behavior', async () => {
    for (const source of ['installed', 'explore']) {
        const s = setup();
        s.ctrl.source = source;
        const item = { ...workshop(), status: 'installed', file: 'original.mp4' };
        s.ctrl.queryWallpapers = [item];
        s.ctrl.sortedWallpapers = [item];
        s.ctrl.updateWallpapers([archive()]);
        s.setResponse({ wallpapers: [item] });
        await s.host.callDeferred('browseWallpaperObject', 'queryWorkshop');
        await s.ctrl.callbackWallpaperSelected(item);
        assert.deepEqual(s.selected, ['original.mp4']);
        assert.equal(item.weArchive, undefined);
        await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123']);
        assert.deepEqual(s.calls.at(-1).args[0], ['123']);
    }
});

test('double click closes when the native select command has no callback', async () => {
    const s = setup({ selectRequest: () => new Promise(() => {}) });
    s.ctrl.updateWallpapers([archive()]);
    await s.ctrl.callbackWallpaperSelected(workshop());
    await Promise.race([
        s.ctrl.callbackOk(true),
        new Promise((resolve, reject) => setTimeout(() => reject(new Error('Double click hung')), 500))
    ]);
    assert.equal(s.calls.at(-1).method, 'acceptAndClose');
});

test('defers native closing until the file-size callback and Angular continuation have returned', async () => {
    const timers = [];
    const s = setup();
    s.window.setTimeout = (callback, delay) => { timers.push({ callback, delay }); };
    s.ctrl.updateWallpapers([archive()]);
    await s.ctrl.callbackWallpaperSelected(workshop());
    const close = s.ctrl.callbackOk(true);
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(s.calls.some(call => call.method === 'acceptAndClose'), false);
    assert.equal(timers.length, 1);
    timers[0].callback();
    await close;
    assert.equal(s.calls.at(-1).method, 'acceptAndClose');
});
