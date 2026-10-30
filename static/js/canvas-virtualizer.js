/**
 * CanvasVirtualizer - Spatial Frustum Culling & Off-screen Subtree Hibernation
 * Part of MeansCanvas
 */
(function (window) {
    'use strict';

    let _rafId = 0;
    let _lastStats = { total: 0, visible: 0, culled: 0 };

    function getNodeWorldBounds(node, el) {
        const x = Number(node?.x) || 0;
        const y = Number(node?.y) || 0;
        const w = Number(node?.w) || Number(el?.offsetWidth) || 320;
        const h = Number(node?.h) || Number(el?.offsetHeight) || 260;
        return { x, y, w: Math.max(40, w), h: Math.max(40, h) };
    }

    function rectsIntersect(a, b) {
        return (
            a.x < b.x + b.w &&
            a.x + a.w > b.x &&
            a.y < b.y + b.h &&
            a.y + a.h > b.y
        );
    }

    function computeWorldFrustum(viewport, boardRect, marginPx) {
        const scale = Math.max(0.05, Number(viewport?.scale) || 1);
        const vx = Number(viewport?.x) || 0;
        const vy = Number(viewport?.y) || 0;
        const bw = Math.max(300, Number(boardRect?.width) || (typeof window !== 'undefined' && window.innerWidth) || 1280);
        const bh = Math.max(240, Number(boardRect?.height) || (typeof window !== 'undefined' && window.innerHeight) || 800);
        const pad = (Number(marginPx) || 640) / scale;
        return {
            x: -vx / scale - pad,
            y: -vy / scale - pad,
            w: bw / scale + pad * 2,
            h: bh / scale + pad * 2,
        };
    }

    function shouldAlwaysKeepNode(node, selectedSet, activeReveId) {
        if (!node) return false;
        const id = String(node.id || '');
        if (selectedSet && selectedSet.has(id)) return true;
        if (activeReveId && id === activeReveId) return true;
        if (node.running || node.pending || node.queued) return true;
        if (node.type === 'frame_group' || node.type === 'smart-group' || node.type === 'group') return true;
        return false;
    }

    function computeVisibleNodeIds(opts) {
        const nodes = Array.isArray(opts?.nodes) ? opts.nodes : [];
        const minNodesToCull = Number(opts?.minNodesToCull ?? 36);
        const selectedSet = opts?.selectedIds instanceof Set
            ? opts?.selectedIds
            : new Set(Array.isArray(opts?.selectedIds) ? opts.selectedIds : []);
        const activeReveId = (typeof window !== 'undefined' && window.CanvasReve?.activeNodeId) || null;

        if (nodes.length < minNodesToCull) {
            return new Set(nodes.map(n => String(n.id)));
        }

        const frustum = computeWorldFrustum(opts?.viewport, opts?.boardRect, opts?.marginPx ?? 640);
        const visible = new Set();
        for (let i = 0; i < nodes.length; i++) {
            const node = nodes[i];
            if (!node || !node.id) continue;
            if (shouldAlwaysKeepNode(node, selectedSet, activeReveId)) {
                visible.add(String(node.id));
                continue;
            }
            const bounds = getNodeWorldBounds(node, null);
            if (rectsIntersect(bounds, frustum)) {
                visible.add(String(node.id));
            }
        }
        return visible;
    }

    function syncDomVirtualization(opts) {
        const containerEl = opts?.containerEl;
        if (!containerEl) return _lastStats;
        const nodeSelector = opts?.nodeSelector || '.node, .image-node';
        const nodes = Array.isArray(opts?.nodes) ? opts.nodes : [];
        const minNodesToCull = Number(opts?.minNodesToCull ?? 36);
        const boardEl = opts?.boardEl || containerEl.parentElement || (typeof document !== 'undefined' && document.body);
        const boardRect = boardEl?.getBoundingClientRect ? boardEl.getBoundingClientRect() : { width: 1280, height: 800 };

        const nodeEls = containerEl.querySelectorAll(nodeSelector);
        if (nodes.length < minNodesToCull) {
            nodeEls.forEach(el => {
                if (el.classList.contains('cv-offscreen')) {
                    el.classList.remove('cv-offscreen');
                }
            });
            _lastStats = { total: nodes.length, visible: nodes.length, culled: 0 };
            return _lastStats;
        }

        const visibleIds = computeVisibleNodeIds({
            nodes,
            viewport: opts?.viewport,
            boardRect,
            marginPx: opts?.marginPx ?? 640,
            selectedIds: opts?.selectedIds,
            minNodesToCull,
        });

        let culledCount = 0;
        let visibleCount = 0;
        nodeEls.forEach(el => {
            const id = String(el.dataset?.id || '');
            const isVis = visibleIds.has(id);
            if (isVis) {
                visibleCount++;
                if (el.classList.contains('cv-offscreen')) {
                    el.classList.remove('cv-offscreen');
                }
            } else {
                culledCount++;
                if (!el.classList.contains('cv-offscreen')) {
                    el.classList.add('cv-offscreen');
                    // Pause off-screen inline video playback to save decode threads
                    const vid = el.querySelector?.('video');
                    if (vid && !vid.paused && typeof vid.pause === 'function') {
                        try { vid.pause(); } catch (_) {}
                    }
                }
            }
        });

        _lastStats = { total: nodes.length, visible: visibleCount, culled: culledCount };
        return _lastStats;
    }

    function scheduleSync(opts) {
        if (_rafId && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(_rafId);
        const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame : (cb) => setTimeout(cb, 16);
        _rafId = raf(() => {
            _rafId = 0;
            syncDomVirtualization(opts);
        });
    }

    function isConnectionVisible(conn, visibleIds) {
        if (!conn || !visibleIds) return true;
        const fromId = String(conn.from || conn.source || '');
        const toId = String(conn.to || conn.target || '');
        return visibleIds.has(fromId) || visibleIds.has(toId);
    }

    window.CanvasVirtualizer = {
        computeVisibleNodeIds,
        syncDomVirtualization,
        scheduleSync,
        isConnectionVisible,
        getStats: () => ({ ..._lastStats }),
    };
})(typeof window !== 'undefined' ? window : globalThis);
