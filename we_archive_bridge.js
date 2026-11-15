/* Local archive integration for Wallpaper Engine 2.8's Angular browser. */
(function (root, factory) {
    "use strict";
    if (typeof module === "object" && module.exports) {
        module.exports = factory;
    } else {
        factory(root);
    }
}(typeof window === "object" ? window : this, function installArchiveBridge(window) {
    "use strict";

    if (window.weArchiveBridge) return window.weArchiveBridge;

    const archives = new Map();
    const originals = new WeakMap();
    const archiveFields = ["file", "project", "type", "filesize", "filesizelabel",
        "local", "localAndExternal", "status", "weArchive"];
    let controller;
    let host;
    let dialogs;
    let q;
    let revision = 0;
    let lastLoggedCount = -1;
    let activeSelection;

    function isWorkshop() {
        return Boolean(controller && controller.source === "workshop");
    }

    function normalizedPath(file) {
        return String(file || "").replace(/\\/g, "/").toLowerCase();
    }

    function archiveId(item) {
        if (!item || item.workshopid || !item.local) return null;
        const match = normalizedPath(item.project || item.file)
            .match(/\/projects\/(?:myprojects|backup)\/(\d+)\//);
        return match && match[1];
    }

    function lookup(id) {
        const candidates = archives.get(String(id));
        if (!candidates) return undefined;
        // Prefer the current archive when a backup of the same ID also exists.
        return Array.from(candidates.values()).find(function (item) {
            return normalizedPath(item.file).includes("/projects/myprojects/");
        }) || candidates.values().next().value;
    }

    function removeFile(file) {
        const key = normalizedPath(file);
        const match = key.match(/\/projects\/(?:myprojects|backup)\/(\d+)\//);
        const candidates = match && archives.get(match[1]);
        if (!candidates || !candidates.delete(key)) return;
        if (!candidates.size) archives.delete(match[1]);
        revision += 1;
    }

    function indexArchives(items, verified = true) {
        (items || []).forEach(function (item) {
            const id = archiveId(item);
            if (!id || !item.file) return;
            if (item.status === "unsubscribed") {
                removeFile(item.file);
                return;
            }
            if (item.status !== "installed" || String(item.type).toLowerCase() !== "video") return;
            if (!archives.has(id)) archives.set(id, new Map());
            archives.get(id).set(normalizedPath(item.file), Object.assign({}, item, { archiveVerified: verified }));
            revision += 1;
        });
    }

    function restore(item) {
        const original = originals.get(item);
        if (!original) return;
        archiveFields.forEach(function (field) {
            if (Object.prototype.hasOwnProperty.call(original, field)) item[field] = original[field];
            else delete item[field];
        });
        originals.delete(item);
    }

    function apply(item) {
        if (!item || !item.workshopid || item.ispreset) return item;
        const archive = lookup(item.workshopid);
        if (!archive || !archive.archiveVerified) {
            restore(item);
            return item;
        }
        if (!originals.has(item)) {
            const original = {};
            archiveFields.forEach(function (field) {
                if (Object.prototype.hasOwnProperty.call(item, field)) original[field] = item[field];
            });
            originals.set(item, original);
        }
        ["file", "project", "filesize", "filesizelabel"].forEach(function (field) {
            if (Object.prototype.hasOwnProperty.call(archive, field)) item[field] = archive[field];
        });
        item.type = "Video";
        item.status = "installed";
        item.local = false;
        item.localAndExternal = false;
        item.weArchive = true;
        return item;
    }

    function applyResults(result) {
        if (isWorkshop() && result && Array.isArray(result.wallpapers)) result.wallpapers.forEach(apply);
        return result;
    }

    function resolveResults(result) {
        if (!result || !Array.isArray(result.wallpapers)) {
            return applyResults(result);
        }
        const candidates = Array.from(new Set(result.wallpapers.map(function (item) {
            return lookup(item.workshopid);
        }).filter(Boolean)));
        if (!candidates.length) return applyResults(result);
        return host.callDeferredSerialized("browseWallpaperObject", "getLocalFileSizes",
            candidates.map(function (item) { return item.file; })).then(function (sizes) {
            const validSizes = new Map(Object.keys(sizes || {}).map(function (file) {
                return [normalizedPath(file), Number(sizes[file])];
            }));
            let removed = false;
            candidates.forEach(function (item) {
                const size = validSizes.get(normalizedPath(item.file));
                if (size > 0) {
                    item.filesize = size;
                    item.archiveVerified = true;
                } else {
                    removeFile(item.file);
                    removed = true;
                }
            });
            // Validate a backup candidate if the preferred copy was unavailable.
            return removed ? resolveResults(result) : applyResults(result);
        }, function () {
            return applyResults(result);
        });
    }

    function refresh() {
        if (!isWorkshop()) return;
        const copies = new Map();
        function viewItem(item) {
            // WE replaces query results with installed records by ID. Keep the
            // archive overlay on a query copy, preserving the installed record.
            if (item && lookup(item.workshopid) && controller.wallpapers.includes(item)) {
                if (!copies.has(item)) copies.set(item, Object.assign({}, item));
                item = copies.get(item);
            }
            return apply(item);
        }
        [controller.queryWallpapers, controller.sortedWallpapers].forEach(function (items) {
            if (!items) return;
            items.forEach(function (item, index) { items[index] = viewItem(item); });
        });
        controller.currentSelection = viewItem(controller.currentSelection);
        // The thumbnail directive observes array identity when it redraws badges.
        controller.sortedWallpapers = (controller.sortedWallpapers || []).slice();
    }

    function logStats() {
        if (archives.size === lastLoggedCount || !window.ui || !window.ui.logError) return;
        lastLoggedCount = archives.size;
        window.ui.logError("[WEArchiveBridge] " + JSON.stringify(api.getStats()));
    }

    function verifyArchive(item) {
        const archive = lookup(item.workshopid);
        if (!archive) return q.when(true);
        const file = archive.file;
        return host.callDeferredSerialized("browseWallpaperObject", "getLocalFileSizes", [file])
            .then(function (sizes) {
                const found = Object.keys(sizes || {}).find(function (key) {
                    return normalizedPath(key) === normalizedPath(file);
                });
                if (found && Number(sizes[found]) > 0) {
                    archive.archiveVerified = true;
                    return true;
                }
                removeFile(file);
                refresh();
                // A backup may still be available after the preferred copy was removed.
                if (lookup(item.workshopid)) return verifyArchive(item);
                restore(item);
                item.status = "downloadable";
                dialogs.open("genericConfirm", {
                    data: {
                        title: "归档文件不可用",
                        message: "请检查归档磁盘或网络共享是否已连接。\n" + file,
                        okVisible: true
                    }
                });
                return false;
            });
    }

    function attach(value) {
        controller = value;
        if (!value || value.weArchiveBridgeAttached) return;
        value.weArchiveBridgeAttached = true;
        const update = value.updateWallpapers;
        value.updateWallpapers = function (items) {
            // Native records retain their identity so folder membership keeps using file paths.
            indexArchives(items);
            const result = update.apply(this, arguments);
            refresh();
            logStats();
            return result;
        };
        const select = value.callbackWallpaperSelected;
        let pendingSelection = 0;
        value.callbackWallpaperSelected = function (item) {
            const ticket = ++pendingSelection;
            if (activeSelection) activeSelection.done.resolve(false);
            activeSelection = undefined;
            const context = this;
            if (!isWorkshop() || !item || !lookup(item.workshopid)) return select.apply(context, arguments);
            const selection = { done: q.defer() };
            activeSelection = selection;
            return verifyArchive(item).then(function (available) {
                if (ticket !== pendingSelection) return false;
                if (!available) {
                    selection.done.resolve(false);
                    return select.call(context, item);
                }
                if (controller.wallpapers.includes(item)) item = Object.assign({}, item);
                apply(item);
                selection.file = item.file;
                if (controller.currentSelection === item) {
                    const current = controller.selectedMonitor && controller.selectedMonitor.wallpaper;
                    if (current && normalizedPath(current.file) === normalizedPath(item.file)) {
                        selection.done.resolve(true);
                    } else controller.currentSelection = undefined;
                }
                return select.call(context, item);
            }, function (error) {
                selection.done.resolve(false);
                return q.reject(error);
            });
        };
        const accept = value.callbackOk;
        value.callbackOk = function (applyAndClose) {
            const context = this;
            const args = arguments;
            if (applyAndClose && isWorkshop() && activeSelection) {
                return activeSelection.done.promise.then(function (loaded) {
                    if (loaded) {
                        // Submit native apply-and-close after the archive selection
                        // has left its file-size callback and Angular continuation.
                        const submitted = q.defer();
                        window.setTimeout(function () {
                            try {
                                accept.apply(context, args);
                                submitted.resolve();
                            } catch (error) { submitted.reject(error); }
                        }, 50);
                        return submitted.promise;
                    }
                });
            }
            return accept.apply(context, args);
        };
        // A query may finish after the first native archive batch or use a backup service.
        value.$watchCollection("queryWallpapers", refresh);
    }

    const api = {
        version: "1.0.4",
        getStats: function () {
            return { version: api.version, archives: archives.size, revision: revision,
                attached: Boolean(controller), source: controller && controller.source };
        },
        lookup: lookup
    };
    window.weArchiveBridge = api;
    const installDirectory = String(window.weArchiveBridgeInstallDir || "").replace(/\\/g, "/").replace(/\/$/, "");
    indexArchives((window.weArchiveBridgeArchiveFiles || []).filter(function (entry) {
        return Array.isArray(entry) && /^\d+$/.test(String(entry[0])) &&
            /^\/projects\/(?:myprojects|backup)\/\d+\//.test(entry[1]) &&
            !entry[1].split("/").includes("..");
    }).map(function (entry) {
        const file = installDirectory + entry[1];
        return { file: file, project: file.slice(0, file.lastIndexOf("/") + 1) + "project.json",
            local: true, status: "installed", type: "Video" };
    }), false);

    Object.defineProperty(window, "browseWallpapersCtrl", {
        configurable: true,
        get: function () { return controller; },
        set: attach
    });

    const app = window.angular.module("wallpaperbrowserApp");
    app.config(["$provide", function ($provide) {
        $provide.decorator("host", ["$delegate", function ($delegate) {
            host = $delegate;
            ["callDeferred", "callDeferredSuperseding", "callDeferredSerialized"].forEach(function (name) {
                const original = host[name];
                host[name] = function (object, method) {
                    const args = Array.from(arguments);
                    if (isWorkshop() && object === "ui" && method === "subscribeWorkshopItem" && Array.isArray(args[2])) {
                        args[2] = args[2].filter(function (id) { return !lookup(id); });
                        if (!args[2].length) {
                            refresh();
                            return q.when();
                        }
                    }
                    if (isWorkshop() && object === "browseWallpaperObject" && method === "selectWallpaper") {
                        const match = normalizedPath(args[2]).match(/\/workshop\/content\/431960\/(\d+)\//);
                        const archive = match && lookup(match[1]);
                        if (archive) args[2] = archive.file;
                    }
                    const result = original.apply(this, args);
                    if (isWorkshop() && object === "browseWallpaperObject" && method === "selectWallpaper" &&
                        activeSelection && normalizedPath(activeSelection.file) === normalizedPath(args[2])) {
                        // This native command is fire-and-forget. WE never invokes
                        // selectWallpaperCallback, so closing must follow submission.
                        activeSelection.done.resolve(true);
                    }
                    if (isWorkshop() && object === "browseWallpaperObject" &&
                        (method === "queryWorkshop" || method === "convertWorkshopBackup")) {
                        return result.then(resolveResults);
                    }
                    return result;
                };
            });
            return host;
        }]);
    }]);
    app.run(["$q", "modals", "$templateCache", function ($q, modals, templates) {
        q = $q;
        dialogs = modals;
        const key = "views/views/browsewallpapers.html";
        const template = templates.get(key);
        if (typeof template !== "string") return;
        const action = /<a ng-if="currentSelection\.status != 'downloadable'" ng-click="callbackUnsubscribeOrRemove\(currentSelection\)"[\s\S]*?<\/a>/;
        const updated = template.replace(action, function (button) {
            return button.replace("currentSelection.status != 'downloadable'",
                "currentSelection.status != 'downloadable' && !(source === 'workshop' && currentSelection.weArchive)") +
                '<span ng-if="source === \'workshop\' && currentSelection.weArchive" class="btn btn-default" ' +
                'title="{{currentSelection.file}}"><span class="fas fa-check" aria-hidden="true"></span> ' +
                "{{'ui_browse_details_btn_subscribed' | translate}}</span>";
        });
        if (updated === template) {
            if (window.ui && window.ui.logError) {
                window.ui.logError("[WEArchiveBridge] Subscription template changed in this WE version.");
            }
        } else templates.put(key, updated);
    }]);
    return api;
}));
