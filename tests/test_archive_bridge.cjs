const assert = require('node:assert/strict');
const test = require('node:test');
const install = require('../we_archive_bridge.js');

const archiveFile = 'E:/WE/projects/myprojects/123/中文 视频.mp4';
const template = '<a ng-if="currentSelection.status != \'downloadable\'" ' +
    'ng-click="callbackUnsubscribeOrRemove(currentSelection)" class="btn">Subscribed</a>';

function setup(options = {}) {
    let configure;
    let run;
    const calls = [];
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
            return response;
        };
    }
    let patchedTemplate = template;
    const window = {
        setTimeout,
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
        { get: () => patchedTemplate, put: (key, value) => { patchedTemplate = value; } });
    const selected = [];
    const ctrl = {
        source: 'workshop', wallpapers: [], sortedWallpapers: [], queryWallpapers: [],
        sortedExploreQueries: [],
        $watchCollection() {},
        updateWallpapers(items) {
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
    return { window, api, ctrl, host, sizes, calls, messages, selected,
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
    s.ctrl.updateWallpapers([archive()]);
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123']);
    assert.equal(s.calls.length, 0);
    await s.host.callDeferred('ui', 'subscribeWorkshopItem', ['123', '999']);
    assert.deepEqual(s.calls.at(-1).args[0], ['999']);
});

test('routes native Workshop selection paths through the archive mapping', async () => {
    const s = setup();
    s.ctrl.updateWallpapers([archive()]);
    await s.host.callDeferred('browseWallpaperObject', 'selectWallpaper',
        'E:/SteamLibrary/steamapps/workshop/content/431960/123/original.mp4', 'monitor');
    assert.equal(s.calls.at(-1).args[0], archiveFile);
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
