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
    const archiveViews = new WeakMap();
    const archiveFields = ["file", "project", "type", "filesize", "filesizelabel",
        "local", "localAndExternal", "status", "weArchive"];
    let controller;
    let host;
    let dialogs;
    let q;
    let revision = 0;
    let lastLoggedCount = -1;
    let activeSelection;
    let updatingWallpapers = false;
    let rootScope;
    const downloadUpdates = { received: 0, unchanged: 0 };
    let pageRedrawsAvoided = 0;
    let authorReturnPage;
    let pendingAuthorReturn;

    function hasNativeTransfer(item) {
        return item && ["pending", "downloading", "error"].includes(item.status);
    }

    function containsUpdate(record, update) {
        // Native updates merge supplied fields, preserving omitted fields and array entries.
        if (update === undefined || record === update) return true;
        if (!record || !update || typeof record !== "object" || typeof update !== "object" ||
            Array.isArray(record) !== Array.isArray(update)) return false;
        return Object.keys(update).every(function (key) {
            return containsUpdate(record[key], update[key]);
        });
    }

    function changedDownloadUpdates(items) {
        if (!controller || !["installed", "workshop"].includes(controller.source) || !rootScope || items.length < 50) return items;
        const seenIds = new Set();
        const duplicateIds = new Set();
        items.forEach(function (item) {
            if (!item || !item.workshopid) return;
            if (seenIds.has(item.workshopid)) duplicateIds.add(item.workshopid);
            seenIds.add(item.workshopid);
        });
        const records = new Map();
        controller.wallpapers.forEach(function (item) {
            if (!item.workshopid) return;
            if (!records.has(item.workshopid)) records.set(item.workshopid, []);
            records.get(item.workshopid).push(item);
        });
        const changed = items.filter(function (item) {
            if (!item || !item.workshopid || item.local || item.ispreset || duplicateIds.has(item.workshopid) ||
                !["pending", "downloading"].includes(item.status)) return true;
            const existing = records.get(item.workshopid);
            return !existing || existing.some(function (record) {
                return record === item || record.ispreset || !containsUpdate(record, item);
            });
        });
        downloadUpdates.received += items.length;
        downloadUpdates.unchanged += items.length - changed.length;
        if (changed.length === items.length) return items;
        // Preserve the full native progress event, including unchanged queued downloads.
        rootScope.$broadcast("onSteamItemsUpdated", items);
        return changed;
    }

    function updatesOutsideCurrentPage(items) {
        if (!["installed", "workshop"].includes(controller.source) || !items.length) return false;
        const visibleIds = new Set([controller.currentSelection].concat(
            controller.queryWallpapers || [], controller.sortedWallpapers || []
        ).filter(Boolean).map(function (item) { return item.workshopid; }));
        return items.every(function (item) {
            return item && item.workshopid && !item.local && !item.ispreset &&
                ["pending", "downloading", "installed", "error", "unsubscribed"].includes(item.status) &&
                !visibleIds.has(item.workshopid);
        });
    }

    function protectedArchive(id) {
        const archive = lookup(id);
        if (!archive || !archive.archiveVerified) return false;
        const visible = [controller.currentSelection].concat(controller.queryWallpapers || []);
        return visible.some(function (item) {
            return item && String(item.workshopid) === String(id) && item.weArchive &&
                item.status === "installed" && normalizedPath(item.file) === normalizedPath(archive.file);
        });
    }

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
        if (hasNativeTransfer(item)) {
            const nativeStatus = item.status;
            restore(item);
            item.status = nativeStatus;
            return item;
        }
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
        if (!isWorkshop()) return result;
        if (!result || !Array.isArray(result.wallpapers)) {
            return applyResults(result);
        }
        const candidates = Array.from(new Set(result.wallpapers.map(function (item) {
            return hasNativeTransfer(item) ? undefined : lookup(item.workshopid);
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
        const installedRecords = new Set(controller.wallpapers);
        let changed = false;
        function viewItem(item) {
            if (!item || (!lookup(item.workshopid) && !originals.has(item))) return item;
            const previous = item;
            const before = archiveFields.map(function (field) { return item[field]; });
            // WE replaces query results with installed records by ID. Keep the
            // archive overlay on a query copy, preserving the installed record.
            if (lookup(item.workshopid) && !hasNativeTransfer(item) && installedRecords.has(item)) {
                if (!archiveViews.has(item)) archiveViews.set(item, Object.assign({}, item));
                const view = archiveViews.get(item);
                restore(view);
                Object.assign(view, item);
                item = view;
            }
            apply(item);
            changed = changed || previous !== item || archiveFields.some(function (field, index) {
                return item[field] !== before[index];
            });
            return item;
        }
        [controller.queryWallpapers, controller.sortedWallpapers].forEach(function (items) {
            if (!items) return;
            items.forEach(function (item, index) { items[index] = viewItem(item); });
        });
        controller.currentSelection = viewItem(controller.currentSelection);
        // The thumbnail directive observes array identity when it redraws badges.
        if (changed) controller.sortedWallpapers = (controller.sortedWallpapers || []).slice();
    }

    function logStats() {
        if (archives.size === lastLoggedCount || !window.ui || !window.ui.logError) return;
        lastLoggedCount = archives.size;
        window.ui.logError("[WEArchiveBridge] " + JSON.stringify(api.getStats()));
    }

    function verifyArchive(item, isCurrent) {
        const archive = lookup(item.workshopid);
        if (!archive) return q.when(true);
        const file = archive.file;
        return host.callDeferredSerialized("browseWallpaperObject", "getLocalFileSizes", [file])
            .then(function (sizes) {
                if (!isCurrent()) return false;
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
                if (lookup(item.workshopid)) return verifyArchive(item, isCurrent);
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
        authorReturnPage = undefined;
        pendingAuthorReturn = undefined;
        const browseAuthor = value.callbackBrowseAuthor;
        const clearAuthor = value.callbackClearAuthorFilter;
        if (typeof browseAuthor === "function" && typeof clearAuthor === "function") {
            value.callbackBrowseAuthor = function (id) {
                const previousPage = authorReturnPage;
                const previousReturn = pendingAuthorReturn;
                let returnPage = isWorkshop() ? authorReturnPage || pendingAuthorReturn : undefined;
                if (!returnPage && isWorkshop() && !value.filterAuthor.authorSteamId &&
                    Number.isInteger(value.pagination.current) && value.pagination.current > 0) {
                    returnPage = { page: value.pagination.current, pagination: value.pagination };
                }
                pendingAuthorReturn = undefined;
                const result = browseAuthor.apply(this, arguments);
                if (isWorkshop() && value.filterAuthor.authorSteamId === id) {
                    authorReturnPage = returnPage;
                } else {
                    authorReturnPage = previousPage;
                    pendingAuthorReturn = previousReturn;
                }
                return result;
            };
            value.callbackClearAuthorFilter = function () {
                if (isWorkshop() && value.filterAuthor.authorSteamId && authorReturnPage) {
                    pendingAuthorReturn = {
                        page: authorReturnPage.page, pagination: authorReturnPage.pagination,
                        filter: JSON.stringify(value.filter), preset: value.filterPreset.workshopid
                    };
                    authorReturnPage = undefined;
                }
                return clearAuthor.apply(this, arguments);
            };
        }
        const update = value.updateWallpapers;
        value.updateWallpapers = function (items) {
            const args = Array.from(arguments);
            const previousList = controller.sortedWallpapers;
            const previousQuery = controller.queryWallpapers;
            const previousSelection = controller.currentSelection;
            const previousSource = controller.source;
            // WE sends recurring download ticks as updateWallpapers(items, false).
            // The true flag remains a full synchronous native initialization update.
            const progressUpdate = args.length === 1 || (args.length === 2 && args[1] === false);
            const backgroundOnly = progressUpdate && Array.isArray(items) && updatesOutsideCurrentPage(items);
            if (progressUpdate && Array.isArray(items)) {
                args[0] = changedDownloadUpdates(items);
                if (!args[0].length && items.length) return;
            }
            // Native records retain their identity so folder membership keeps using file paths.
            indexArchives(args[0]);
            let result;
            const previousUpdating = updatingWallpapers;
            updatingWallpapers = true;
            try {
                result = update.apply(this, args);
            } finally { updatingWallpapers = previousUpdating; }
            refresh();
            // Native download ticks copy the thumbnail array even when every visible
            // item is unrelated. Preserve it so menus and tiles stay stable.
            if (backgroundOnly && controller.source === previousSource && controller.queryWallpapers === previousQuery &&
                controller.currentSelection === previousSelection && Array.isArray(previousList) &&
                previousList !== controller.sortedWallpapers &&
                previousList.length === controller.sortedWallpapers.length &&
                previousList.every(function (item, index) { return item === controller.sortedWallpapers[index]; })) {
                controller.sortedWallpapers = previousList;
                pageRedrawsAvoided += 1;
            }
            logStats();
            return result;
        };
        const select = value.callbackWallpaperSelected;
        let pendingSelection = 0;
        value.callbackWallpaperSelected = function (item) {
            const current = controller.currentSelection;
            const archive = item && lookup(item.workshopid);
            // Native list reconciliation reselects aliased records on every download tick.
            // Keep an already selected archive instead of reloading it and its author details.
            if (updatingWallpapers && isWorkshop() && current && current.weArchive &&
                item && !item.ispreset && !hasNativeTransfer(item) && archive &&
                current.workshopid === item.workshopid &&
                normalizedPath(current.file) === normalizedPath(archive.file)) return;
            const ticket = ++pendingSelection;
            if (activeSelection) activeSelection.done.resolve(false);
            activeSelection = undefined;
            const context = this;
            if (!isWorkshop() || !item || item.ispreset || hasNativeTransfer(item) ||
                String(item.type).toLowerCase() !== "video" || !lookup(item.workshopid)) {
                return select.apply(context, arguments);
            }
            const selection = { done: q.defer() };
            activeSelection = selection;
            function isCurrent() {
                return ticket === pendingSelection && isWorkshop();
            }
            return verifyArchive(item, isCurrent).then(function (available) {
                if (!isCurrent()) {
                    selection.done.resolve(false);
                    return false;
                }
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
                const selection = activeSelection;
                function isCurrentArchive() {
                    const current = controller.currentSelection;
                    return activeSelection === selection && isWorkshop() && current && current.weArchive &&
                        normalizedPath(current.file) === normalizedPath(selection.file);
                }
                return selection.done.promise.then(function (loaded) {
                    if (loaded && isCurrentArchive()) {
                        // Submit native apply-and-close after the archive selection
                        // has left its file-size callback and Angular continuation.
                        const submitted = q.defer();
                        window.setTimeout(function () {
                            try {
                                if (isCurrentArchive()) accept.apply(context, args);
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
        value.$watch("source", function (source) {
            if (source === "workshop") return;
            authorReturnPage = undefined;
            pendingAuthorReturn = undefined;
            pendingSelection += 1;
            if (activeSelection) activeSelection.done.resolve(false);
            activeSelection = undefined;
        });
    }

    const api = {
        version: "1.0.11",
        getStats: function () {
            return { version: api.version, archives: archives.size, revision: revision,
                attached: Boolean(controller), source: controller && controller.source,
                downloadUpdates: Object.assign({}, downloadUpdates), pageRedrawsAvoided: pageRedrawsAvoided };
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
                    const query = args[2];
                    if (pendingAuthorReturn && isWorkshop() && object === "browseWallpaperObject" &&
                        method === "queryWorkshop" && query && !query.totalOnly && !query.callback &&
                        Object.prototype.hasOwnProperty.call(query, "filterTitle")) {
                        const saved = pendingAuthorReturn;
                        pendingAuthorReturn = undefined;
                        if (!query.itemid && !controller.filterAuthor.authorSteamId &&
                            controller.pagination === saved.pagination && query.preset === saved.preset &&
                            JSON.stringify(controller.filter) === saved.filter) {
                            // The native filter watcher resets the page before building this query.
                            // Update the same query object used by its response/pagination callback.
                            query.page = saved.page;
                            controller.pagination.current = saved.page;
                        }
                    }
                    if (isWorkshop() && object === "ui" && method === "subscribeWorkshopItem" && Array.isArray(args[2])) {
                        const allowed = args[2].filter(function (id) { return !protectedArchive(id); });
                        if (allowed.length !== args[2].length) {
                            if (!allowed.length) return q.when();
                            args[2] = allowed;
                        }
                    }
                    if (isWorkshop() && object === "browseWallpaperObject" && method === "selectWallpaper") {
                        const match = normalizedPath(args[2]).match(/\/workshop\/content\/431960\/(\d+)\//);
                        const archive = match && lookup(match[1]);
                        if (archive && protectedArchive(match[1])) args[2] = archive.file;
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
    app.run(["$q", "modals", "$templateCache", "$rootScope", function ($q, modals, templates, $rootScope) {
        q = $q;
        rootScope = $rootScope;
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
