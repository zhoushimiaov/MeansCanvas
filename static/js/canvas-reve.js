/**
 * CanvasReve - Reve-Style Instruction & Regional Image Editing Module 2.0
 * Part of MeansCanvas (原 Infinite-Canvas)
 */
(function(window) {
    'use strict';

    function escapeHtml(str) {
        return String(str == null ? '' : str).replace(/[&<>"']/g, s => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
        }[s]));
    }
    function escapeAttr(str) { return escapeHtml(str); }

    const CanvasReve = {
        activeNodeId: null,
        barEl: null,
        bboxActive: false,
        bboxOverlayEl: null,
        maskCanvasEl: null,
        maskCtx: null,
        boxRectEl: null,
        currentRegion: null,
        currentTool: 'box', // 'box' | 'brush' | 'erase'
        brushSize: 28,
        featherRadius: 12,
        preserveOutside: true,
        applyMode: 'new', // 'new' | 'inplace'
        referenceImageUrl: '',
        hasBrushStrokes: false,
        _overlayCleanup: null,

        resolveNodeTarget(nodeId) {
            const node = window.nodes?.find(n => String(n.id) === String(nodeId));
            if (!node) return null;
            if (node.type === 'image' && node.url) {
                return { node, url: node.url, kind: 'classic-image' };
            }
            if (node.type === 'output') {
                const url = node.selectedOutputUrl || (Array.isArray(node.outputs) && node.outputs[node.outputs.length - 1]?.url) || (Array.isArray(node.urls) && node.urls[node.urls.length - 1]) || node.url || '';
                if (url) return { node, url, kind: 'classic-output' };
            }
            if (node.type === 'smart-image' || Array.isArray(node.images)) {
                const url = node.images?.[node.selectedImageIndex || 0]?.url || node.images?.[0]?.url || node.url || '';
                if (url) return { node, url, kind: 'smart-image' };
            }
            return null;
        },

        init() {
            // Keyboard shortcut Shift+E
            window.addEventListener('keydown', (e) => {
                if (e.shiftKey && (e.key === 'E' || e.key === 'e')) {
                    if (['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) return;
                    const selId = Array.from(window.selected || [])[0];
                    if (selId) {
                        const target = this.resolveNodeTarget(selId);
                        if (target) {
                            e.preventDefault();
                            this.open(selId);
                        }
                    }
                } else if (e.key === 'Escape' && this.activeNodeId) {
                    this.close();
                }
            });

            // Close when clicking outside
            document.addEventListener('mousedown', (e) => {
                if (!this.activeNodeId) return;
                if (e.target.closest('#reveQuickBar, .reve-bbox-overlay, .node-reve-btn, [data-smart-node-action="reve"], [data-image-reve]')) return;
                this.close();
            });

            window.addEventListener('resize', () => {
                if (this.activeNodeId) this.updatePosition();
            });
            window.addEventListener('scroll', () => {
                if (this.activeNodeId) this.updatePosition();
            }, { passive: true });
        },

        open(nodeId) {
            const target = this.resolveNodeTarget(nodeId);
            if (!target) return;

            this.close();
            this.activeNodeId = nodeId;
            this.currentRegion = null;
            this.hasBrushStrokes = false;
            this.bboxActive = false;
            this.currentTool = 'box';

            this.renderBar(target.node);
            this.updatePosition();
            this.mountMaskOverlay();
        },

        close() {
            if (this.barEl) {
                this.barEl.remove();
                this.barEl = null;
            }
            this.removeBoxOverlay();
            this.activeNodeId = null;
            this.currentRegion = null;
            this.hasBrushStrokes = false;
            this.bboxActive = false;
            this.referenceImageUrl = '';
        },

        renderBar(node) {
            const bar = document.createElement('div');
            bar.id = 'reveQuickBar';
            bar.className = 'reve-quick-bar';

            const tr = window.tr || ((k, f) => f || k);
            const placeholder = tr('canvas.revePlaceholder', '输入修改指令（如：换成金黄晚霞背景、为人物戴上墨镜）...');
            const boxLabel = tr('canvas.reveBoxSelect', '选区框选');
            const brushLabel = tr('canvas.reveBrush', '画笔涂抹');
            const eraserLabel = tr('canvas.reveEraser', '橡皮擦');
            const clearLabel = tr('canvas.reveClearMask', '清空蒙版');
            const refLabel = tr('canvas.reveRefImage', '局部参考图');
            const modeLabel = this.applyMode === 'new' ? tr('canvas.reveNewNodeMode', '派生新节点') : tr('canvas.reveInplaceMode', '原位替换');
            const submitLabel = tr('canvas.reveGenerate', '修图');

            const models = this.getAvailableEditModels(node);
            const modelOptions = models.map(m => `<option value="${escapeAttr(m.id)}" data-provider="${escapeAttr(m.providerId)}">${escapeHtml(m.label)}</option>`).join('');

            bar.innerHTML = `
                <i data-lucide="wand-2" class="w-4 h-4" style="color:var(--accent,#c97b5a);flex-shrink:0;"></i>
                <input type="text" class="reve-bar-input" id="revePromptInput" placeholder="${escapeAttr(placeholder)}" />
                <button type="button" class="reve-bar-btn ${this.currentTool === 'box' ? 'active' : ''}" id="reveToolBox" title="${escapeAttr(boxLabel)}">
                    <i data-lucide="box-select" class="w-3.5 h-3.5"></i>
                </button>
                <button type="button" class="reve-bar-btn ${this.currentTool === 'brush' ? 'active' : ''}" id="reveToolBrush" title="${escapeAttr(brushLabel)}">
                    <i data-lucide="paintbrush" class="w-3.5 h-3.5"></i>
                </button>
                <button type="button" class="reve-bar-btn ${this.currentTool === 'erase' ? 'active' : ''}" id="reveToolEraser" title="${escapeAttr(eraserLabel)}">
                    <i data-lucide="eraser" class="w-3.5 h-3.5"></i>
                </button>
                <button type="button" class="reve-bar-btn" id="reveClearBtn" title="${escapeAttr(clearLabel)}">
                    <i data-lucide="rotate-ccw" class="w-3.5 h-3.5"></i>
                </button>
                <div class="reve-slider-pill" title="${escapeAttr(tr('canvas.brushSize', '笔刷大小'))}">
                    <i data-lucide="circle-dot" class="w-3 h-3"></i>
                    <input type="range" id="reveBrushSize" min="8" max="120" value="${this.brushSize}">
                </div>
                <div class="reve-slider-pill" title="${escapeAttr(tr('canvas.reveFeather', '羽化边缘'))}">
                    <i data-lucide="wind" class="w-3 h-3"></i>
                    <input type="range" id="reveFeatherSize" min="0" max="48" value="${this.featherRadius}">
                </div>
                <button type="button" class="reve-bar-btn" id="reveRefBtn" title="${escapeAttr(refLabel)}">
                    <i data-lucide="image-plus" class="w-3.5 h-3.5"></i>
                </button>
                <input type="file" id="reveRefInput" accept="image/*" style="display:none" />
                <div id="reveRefThumbSlot" style="display:flex;align-items:center;"></div>
                <button type="button" class="reve-bar-btn" id="reveModeToggle" title="${escapeAttr(modeLabel)}">
                    <i data-lucide="${this.applyMode === 'new' ? 'layers' : 'replace'}" class="w-3.5 h-3.5"></i>
                    <span>${escapeHtml(modeLabel)}</span>
                </button>
                <select class="reve-model-select" id="reveModelSelect">
                    ${modelOptions}
                </select>
                <button type="button" class="reve-bar-submit" id="reveSubmitBtn">
                    <i data-lucide="arrow-right" class="w-3.5 h-3.5"></i>
                    <span>${escapeHtml(submitLabel)}</span>
                </button>
            `;

            document.body.appendChild(bar);
            this.barEl = bar;

            // Default model to node's existing model & provider if available
            const selectEl = bar.querySelector('#reveModelSelect');
            const targetModel = node?.model || '';
            const targetProv = node?.apiProvider || node?.providerId || node?.provider || '';
            if (targetModel && selectEl) {
                const opt = Array.from(selectEl.options).find(o =>
                    o.value === targetModel && (!targetProv || o.dataset.provider === targetProv)
                ) || Array.from(selectEl.options).find(o => o.value === targetModel);
                if (opt) selectEl.value = opt.value;
            }

            const input = bar.querySelector('#revePromptInput');
            input.focus();
            input.addEventListener('input', () => {
                input.classList.remove('reve-input-empty');
            });
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    this.submit();
                }
            });

            bar.querySelector('#reveToolBox').addEventListener('click', () => this.setTool('box'));
            bar.querySelector('#reveToolBrush').addEventListener('click', () => this.setTool('brush'));
            bar.querySelector('#reveToolEraser').addEventListener('click', () => this.setTool('erase'));
            bar.querySelector('#reveClearBtn').addEventListener('click', () => this.clearMask());

            const brushSlider = bar.querySelector('#reveBrushSize');
            if (brushSlider) {
                brushSlider.addEventListener('input', (e) => {
                    this.brushSize = parseInt(e.target.value, 10) || 28;
                });
            }

            const featherSlider = bar.querySelector('#reveFeatherSize');
            if (featherSlider) {
                featherSlider.addEventListener('input', (e) => {
                    this.featherRadius = parseInt(e.target.value, 10) || 12;
                });
            }

            const refBtn = bar.querySelector('#reveRefBtn');
            const refInput = bar.querySelector('#reveRefInput');
            if (refBtn && refInput) {
                refBtn.addEventListener('click', () => refInput.click());
                refInput.addEventListener('change', () => {
                    const file = refInput.files?.[0];
                    if (!file) return;
                    const reader = new FileReader();
                    reader.onload = (ev) => {
                        this.referenceImageUrl = ev.target?.result || '';
                        this.updateRefThumb();
                    };
                    reader.readAsDataURL(file);
                });
            }

            const modeBtn = bar.querySelector('#reveModeToggle');
            if (modeBtn) {
                modeBtn.addEventListener('click', () => {
                    this.applyMode = this.applyMode === 'new' ? 'inplace' : 'new';
                    const newModeLabel = this.applyMode === 'new' ? tr('canvas.reveNewNodeMode', '派生新节点') : tr('canvas.reveInplaceMode', '原位替换');
                    modeBtn.title = newModeLabel;
                    modeBtn.innerHTML = `
                        <i data-lucide="${this.applyMode === 'new' ? 'layers' : 'replace'}" class="w-3.5 h-3.5"></i>
                        <span>${escapeHtml(newModeLabel)}</span>
                    `;
                    if (window.lucide?.createIcons) window.lucide.createIcons({ root: modeBtn });
                });
            }

            bar.querySelector('#reveSubmitBtn').addEventListener('click', () => {
                this.submit();
            });

            if (window.lucide && typeof window.lucide.createIcons === 'function') {
                window.lucide.createIcons({ root: bar });
            }
        },

        updatePosition() {
            if (!this.barEl || !this.activeNodeId) return;
            const nodeEl = document.querySelector(`.node[data-id="${this.activeNodeId}"], .image-node[data-id="${this.activeNodeId}"]`);
            if (!nodeEl) {
                this.close();
                return;
            }
            const rect = nodeEl.getBoundingClientRect();
            const top = rect.bottom + 8 < window.innerHeight - 60 ? rect.bottom + 8 : Math.max(10, rect.top - 46);
            const left = Math.max(10, Math.min(window.innerWidth - 680, rect.left));
            this.barEl.style.top = `${top}px`;
            this.barEl.style.left = `${left}px`;
        },

        updateToolButtons() {
            if (!this.barEl) return;
            this.barEl.querySelector('#reveToolBox')?.classList.toggle('active', this.currentTool === 'box');
            this.barEl.querySelector('#reveToolBrush')?.classList.toggle('active', this.currentTool === 'brush');
            this.barEl.querySelector('#reveToolEraser')?.classList.toggle('active', this.currentTool === 'erase');
            if (this.bboxOverlayEl) {
                this.bboxOverlayEl.style.cursor = this.currentTool === 'box' ? 'crosshair' : (this.currentTool === 'brush' ? 'crosshair' : 'default');
            }
        },

        updateRefThumb() {
            const slot = this.barEl?.querySelector('#reveRefThumbSlot');
            if (!slot) return;
            slot.innerHTML = '';
            if (!this.referenceImageUrl) return;
            const img = document.createElement('img');
            img.src = this.referenceImageUrl;
            img.className = 'reve-ref-thumb';
            img.title = '点击移除参考图';
            img.addEventListener('click', () => {
                this.referenceImageUrl = '';
                this.updateRefThumb();
            });
            slot.appendChild(img);
        },

        setTool(tool) {
            this.currentTool = tool;
            this.mountMaskOverlay();
            this.updateToolButtons();
        },

        clearMask() {
            if (this.maskCtx && this.maskCanvasEl) {
                this.maskCtx.clearRect(0, 0, this.maskCanvasEl.width, this.maskCanvasEl.height);
            }
            this.hasBrushStrokes = false;
            this.currentRegion = null;
            if (this.boxRectEl) {
                this.boxRectEl.style.display = 'none';
            }
        },

        mountMaskOverlay() {
            const nodeEl = document.querySelector(`.node[data-id="${this.activeNodeId}"], .image-node[data-id="${this.activeNodeId}"]`);
            if (!nodeEl) return;
            const wrap = nodeEl.querySelector('.image-preview-wrap, .image-node-body, .node-body') || nodeEl;
            const img = wrap.querySelector('img');

            if (!this.bboxOverlayEl) {
                const overlay = document.createElement('div');
                overlay.className = 'reve-bbox-overlay';

                const canvas = document.createElement('canvas');
                canvas.className = 'reve-mask-canvas';
                canvas.width = Math.max(32, wrap.clientWidth || 300);
                canvas.height = Math.max(32, wrap.clientHeight || 300);
                overlay.appendChild(canvas);
                this.maskCanvasEl = canvas;
                this.maskCtx = canvas.getContext('2d');

                const box = document.createElement('div');
                box.className = 'reve-bbox-rect';
                box.style.display = 'none';
                box.innerHTML = `
                    <div class="reve-resize-handle nw" data-handle="nw"></div>
                    <div class="reve-resize-handle ne" data-handle="ne"></div>
                    <div class="reve-resize-handle se" data-handle="se"></div>
                    <div class="reve-resize-handle sw" data-handle="sw"></div>
                `;
                overlay.appendChild(box);
                this.boxRectEl = box;

                wrap.appendChild(overlay);
                this.bboxOverlayEl = overlay;

                this._bindOverlayEvents(overlay, box, canvas, wrap, img);
            }
            this.updateToolButtons();
        },

        _bindOverlayEvents(overlay, box, canvas, wrap, img) {
            let isDrawingBox = false;
            let isDraggingBox = false;
            let isResizingBox = false;
            let resizeHandle = '';
            let isPainting = false;
            let startX = 0, startY = 0;
            let boxStartX = 0, boxStartY = 0, boxStartW = 0, boxStartH = 0;

            const getPos = (e) => {
                const rect = overlay.getBoundingClientRect();
                return {
                    x: Math.max(0, Math.min(rect.width, e.clientX - rect.left)),
                    y: Math.max(0, Math.min(rect.height, e.clientY - rect.top))
                };
            };

            const commitBoxRegion = () => {
                const targetImg = wrap.querySelector('img') || img;
                if (!targetImg) return;
                const imgRect = targetImg.getBoundingClientRect();
                const overRect = overlay.getBoundingClientRect();
                const nw = targetImg.naturalWidth || imgRect.width;
                const nh = targetImg.naturalHeight || imgRect.height;
                const curLeft = parseFloat(box.style.left) || 0;
                const curTop = parseFloat(box.style.top) || 0;
                const curW = parseFloat(box.style.width) || 0;
                const curH = parseFloat(box.style.height) || 0;

                const screenLeft = overRect.left + curLeft;
                const screenTop = overRect.top + curTop;
                const clampedLeft = Math.max(imgRect.left, Math.min(imgRect.right, screenLeft));
                const clampedTop = Math.max(imgRect.top, Math.min(imgRect.bottom, screenTop));
                const clampedRight = Math.max(imgRect.left, Math.min(imgRect.right, screenLeft + curW));
                const clampedBottom = Math.max(imgRect.top, Math.min(imgRect.bottom, screenTop + curH));
                const clampedW = clampedRight - clampedLeft;
                const clampedH = clampedBottom - clampedTop;

                if (clampedW > 4 && clampedH > 4) {
                    const scaleX = nw / (imgRect.width || 1);
                    const scaleY = nh / (imgRect.height || 1);
                    this.currentRegion = {
                        x: Math.round((clampedLeft - imgRect.left) * scaleX),
                        y: Math.round((clampedTop - imgRect.top) * scaleY),
                        width: Math.round(clampedW * scaleX),
                        height: Math.round(clampedH * scaleY),
                    };
                } else {
                    this.currentRegion = null;
                }
            };

            const onMouseMove = (e) => {
                const pos = getPos(e);
                if (isDrawingBox) {
                    const l = Math.min(startX, pos.x);
                    const t = Math.min(startY, pos.y);
                    const w = Math.abs(pos.x - startX);
                    const h = Math.abs(pos.y - startY);
                    box.style.left = `${l}px`;
                    box.style.top = `${t}px`;
                    box.style.width = `${w}px`;
                    box.style.height = `${h}px`;
                    box.style.display = 'block';
                } else if (isDraggingBox) {
                    const dx = pos.x - startX;
                    const dy = pos.y - startY;
                    box.style.left = `${Math.max(0, boxStartX + dx)}px`;
                    box.style.top = `${Math.max(0, boxStartY + dy)}px`;
                } else if (isResizingBox) {
                    const dx = pos.x - startX;
                    const dy = pos.y - startY;
                    if (resizeHandle === 'se') {
                        box.style.width = `${Math.max(16, boxStartW + dx)}px`;
                        box.style.height = `${Math.max(16, boxStartH + dy)}px`;
                    } else if (resizeHandle === 'sw') {
                        box.style.left = `${boxStartX + dx}px`;
                        box.style.width = `${Math.max(16, boxStartW - dx)}px`;
                        box.style.height = `${Math.max(16, boxStartH + dy)}px`;
                    } else if (resizeHandle === 'ne') {
                        box.style.top = `${boxStartY + dy}px`;
                        box.style.width = `${Math.max(16, boxStartW + dx)}px`;
                        box.style.height = `${Math.max(16, boxStartH - dy)}px`;
                    } else if (resizeHandle === 'nw') {
                        box.style.left = `${boxStartX + dx}px`;
                        box.style.top = `${boxStartY + dy}px`;
                        box.style.width = `${Math.max(16, boxStartW - dx)}px`;
                        box.style.height = `${Math.max(16, boxStartH - dy)}px`;
                    }
                } else if (isPainting && this.maskCtx) {
                    this.maskCtx.lineTo(pos.x, pos.y);
                    this.maskCtx.stroke();
                    this.hasBrushStrokes = true;
                }
            };

            const onMouseUp = () => {
                if (isDrawingBox || isDraggingBox || isResizingBox) {
                    commitBoxRegion();
                }
                isDrawingBox = false;
                isDraggingBox = false;
                isResizingBox = false;
                if (isPainting && this.maskCtx) {
                    isPainting = false;
                    this.maskCtx.closePath();
                }
                window.removeEventListener('mousemove', onMouseMove);
                window.removeEventListener('mouseup', onMouseUp);
            };

            const onMouseDown = (e) => {
                e.stopPropagation();
                const pos = getPos(e);
                const handleEl = e.target.closest('.reve-resize-handle');
                if (handleEl && this.currentTool === 'box') {
                    isResizingBox = true;
                    resizeHandle = handleEl.dataset.handle;
                    startX = pos.x;
                    startY = pos.y;
                    boxStartX = parseFloat(box.style.left) || 0;
                    boxStartY = parseFloat(box.style.top) || 0;
                    boxStartW = parseFloat(box.style.width) || 0;
                    boxStartH = parseFloat(box.style.height) || 0;
                } else if (e.target.closest('.reve-bbox-rect') && this.currentTool === 'box') {
                    isDraggingBox = true;
                    startX = pos.x;
                    startY = pos.y;
                    boxStartX = parseFloat(box.style.left) || 0;
                    boxStartY = parseFloat(box.style.top) || 0;
                } else if (this.currentTool === 'box') {
                    isDrawingBox = true;
                    startX = pos.x;
                    startY = pos.y;
                    box.style.left = `${startX}px`;
                    box.style.top = `${startY}px`;
                    box.style.width = '0px';
                    box.style.height = '0px';
                    box.style.display = 'block';
                } else if (this.currentTool === 'brush' || this.currentTool === 'erase') {
                    if (this.maskCtx) {
                        isPainting = true;
                        this.maskCtx.lineWidth = this.brushSize;
                        this.maskCtx.lineCap = 'round';
                        this.maskCtx.lineJoin = 'round';
                        if (this.currentTool === 'brush') {
                            this.maskCtx.globalCompositeOperation = 'source-over';
                            this.maskCtx.strokeStyle = 'rgba(201, 123, 90, 0.55)';
                        } else {
                            this.maskCtx.globalCompositeOperation = 'destination-out';
                        }
                        this.maskCtx.beginPath();
                        this.maskCtx.moveTo(pos.x, pos.y);
                        this.maskCtx.lineTo(pos.x, pos.y);
                        this.maskCtx.stroke();
                        this.hasBrushStrokes = true;
                    }
                }
                window.addEventListener('mousemove', onMouseMove);
                window.addEventListener('mouseup', onMouseUp);
            };

            overlay.addEventListener('mousedown', onMouseDown);
            this._overlayCleanup = () => {
                window.removeEventListener('mousemove', onMouseMove);
                window.removeEventListener('mouseup', onMouseUp);
            };
        },

        removeBoxOverlay() {
            if (this._overlayCleanup) {
                this._overlayCleanup();
                this._overlayCleanup = null;
            }
            if (this.bboxOverlayEl) {
                this.bboxOverlayEl.remove();
                this.bboxOverlayEl = null;
            }
            this.maskCanvasEl = null;
            this.maskCtx = null;
            this.boxRectEl = null;
        },

        exportMaskDataUrl(targetImg) {
            const hasBox = Boolean(this.currentRegion && this.currentRegion.width > 0 && this.currentRegion.height > 0);
            if (!this.hasBrushStrokes && !hasBox) {
                return '';
            }
            if (!targetImg && typeof document !== 'undefined') {
                const nodeEl = document.querySelector(`.node[data-id="${this.activeNodeId}"], .image-node[data-id="${this.activeNodeId}"]`);
                targetImg = nodeEl?.querySelector('img') || null;
            }
            const nw = targetImg?.naturalWidth || 1024;
            const nh = targetImg?.naturalHeight || 1024;
            if (typeof document === 'undefined') return '';
            const offscreen = document.createElement('canvas');
            offscreen.width = nw;
            offscreen.height = nh;
            const ctx = offscreen.getContext?.('2d');
            if (!ctx) return '';

            // Background black
            ctx.fillStyle = '#000000';
            ctx.fillRect(0, 0, nw, nh);

            // Box selection white
            if (hasBox && this.currentRegion) {
                ctx.fillStyle = '#ffffff';
                ctx.fillRect(this.currentRegion.x, this.currentRegion.y, this.currentRegion.width, this.currentRegion.height);
            }

            // Brush strokes white
            if (this.maskCanvasEl && this.hasBrushStrokes) {
                const tmp = document.createElement('canvas');
                tmp.width = this.maskCanvasEl.width;
                tmp.height = this.maskCanvasEl.height;
                const tctx = tmp.getContext?.('2d');
                if (tctx) {
                    tctx.drawImage(this.maskCanvasEl, 0, 0);
                    tctx.globalCompositeOperation = 'source-in';
                    tctx.fillStyle = '#ffffff';
                    tctx.fillRect(0, 0, tmp.width, tmp.height);
                    ctx.drawImage(tmp, 0, 0, nw, nh);
                }
            }
            return offscreen.toDataURL ? offscreen.toDataURL('image/png') : '';
        },

        getAvailableEditModels(node) {
            const list = [];
            const seen = new Set();
            const add = (id, label, providerId) => {
                if (!id) return;
                const key = `${id}@@${providerId || ''}`;
                if (seen.has(key)) return;
                seen.add(key);
                list.push({ id, label: label || id, providerId: providerId || '' });
            };

            const nodeModel = node?.model || '';
            const nodeProvider = node?.apiProvider || node?.providerId || node?.provider || '';
            const providers = Array.isArray(window.apiProviders) ? window.apiProviders : [];

            if (nodeModel) {
                const provObj = providers.find(p => p.id === nodeProvider);
                const provName = provObj?.name || nodeProvider;
                add(nodeModel, provName ? `${nodeModel} (${provName})` : nodeModel, nodeProvider);
            }

            for (const p of providers) {
                if (!p || p.enabled === false) continue;
                const models = Array.isArray(p.image_models) ? p.image_models : [];
                for (const m of models) {
                    add(m, `${m} (${p.name || p.id})`, p.id);
                }
            }

            if (list.length === 0) {
                return [
                    { id: 'qwen-image-edit', label: '通义万相修图', providerId: 'siliconflow-cn' },
                    { id: 'flux-pro-1.0-fill', label: 'FLUX.1 Fill', providerId: 'bfl' },
                    { id: 'gpt-image-2', label: 'GPT Image 2', providerId: 'openai' },
                    { id: 'black-forest-labs/FLUX.2-klein-9B', label: 'FLUX Klein', providerId: 'modelscope' }
                ];
            }

            return list;
        },

        async submit() {
            if (!this.activeNodeId) return;
            const target = this.resolveNodeTarget(this.activeNodeId);
            if (!target || !target.url) return;

            const input = this.barEl?.querySelector('#revePromptInput');
            const prompt = (input?.value || '').trim();
            if (!prompt) {
                const tr = window.tr || ((k, f) => f || k);
                const tip = tr('canvas.revePromptRequired', '请输入修图指令');
                if (typeof window.showToast === 'function') window.showToast(tip, 'warning');
                else if (typeof window.toast === 'function') window.toast(tip);
                else if (typeof window.showQuickNotification === 'function') window.showQuickNotification(tip);

                if (input) {
                    input.classList.add('reve-input-empty');
                    input.focus();
                    setTimeout(() => input.classList.remove('reve-input-empty'), 1500);
                }
                return;
            }

            const select = this.barEl?.querySelector('#reveModelSelect');
            const model = select?.value || target.node?.model || 'gpt-image-2';
            const selectedOpt = select?.selectedOptions?.[0];
            const providerId = selectedOpt?.dataset?.provider || target.node?.apiProvider || target.node?.providerId || '';
            const region = this.currentRegion;

            const sourceNodeId = this.activeNodeId;
            const sourceUrl = target.url;
            const maskDataUrl = (typeof this.exportMaskDataUrl === 'function') ? this.exportMaskDataUrl() : '';
            const refImageUrl = this.referenceImageUrl || '';
            const featherRadius = Number(this.featherRadius ?? 12);
            const preserveOutside = Boolean(this.preserveOutside ?? true);

            const isNewMode = this.applyMode === 'new';
            let newNode = null;

            if (isNewMode) {
                // Spawn derived node right next to original
                const newNodeId = 'node_' + Date.now() + '_' + Math.random().toString(36).substring(2, 6);
                newNode = {
                    id: newNodeId,
                    type: 'image',
                    name: (target.node.name || 'image') + ' (Reve Edit)',
                    x: (target.node.x || 0) + (target.node.w || 300) + 60,
                    y: target.node.y || 0,
                    w: target.node.w || 300,
                    h: target.node.h || 300,
                    url: '',
                    prompt: prompt,
                    modelLabel: model,
                    model: model,
                    running: true,
                    runStatus: 'running',
                };
                if (window.nodes) {
                    if (typeof window.pushUndo === 'function') window.pushUndo();
                    window.nodes.push(newNode);
                    const connList = Array.isArray(window.connections) ? window.connections : (Array.isArray(window.links) ? window.links : null);
                    if (connList) {
                        connList.push({
                            id: 'conn_' + Date.now(),
                            from: sourceNodeId,
                            to: newNodeId,
                        });
                    }
                    if (typeof window.render === 'function') window.render();
                    if (typeof window.scheduleSave === 'function') window.scheduleSave();
                }
            } else {
                // In-place mode
                if (typeof window.pushUndo === 'function') window.pushUndo();
                target.node.running = true;
                target.node.runStatus = 'running';
                if (typeof window.render === 'function') window.render();
            }

            this.close();

            try {
                const fetchFn = window.fetch || fetch;
                const resp = await fetchFn('/api/canvas-edit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        source_image_url: sourceUrl,
                        prompt: prompt,
                        model: model,
                        provider_id: providerId,
                        region: region,
                        mask_data_url: maskDataUrl || undefined,
                        reference_image_url: refImageUrl || undefined,
                        feather_radius: featherRadius,
                        preserve_outside: preserveOutside,
                    }),
                });

                if (!resp.ok) {
                    const errData = await resp.json().catch(() => ({}));
                    throw new Error(errData.detail || `请求失败 (${resp.status})`);
                }

                const data = await resp.json();
                const outUrl = data.output_url || '';

                if (isNewMode && newNode) {
                    newNode.url = outUrl;
                    newNode.running = false;
                    newNode.runStatus = 'done';
                } else {
                    if (target.kind === 'classic-image') {
                        target.node.url = outUrl;
                    } else if (target.kind === 'classic-output') {
                        target.node.selectedOutputUrl = outUrl;
                        if (Array.isArray(target.node.outputs)) target.node.outputs.push({ url: outUrl });
                    } else if (target.kind === 'smart-image') {
                        if (Array.isArray(target.node.images) && target.node.images.length > 0) {
                            target.node.images[target.node.selectedImageIndex || 0].url = outUrl;
                        } else {
                            target.node.url = outUrl;
                        }
                    }
                    target.node.running = false;
                    target.node.runStatus = 'done';
                }

                if (typeof window.render === 'function') window.render();
                if (typeof window.scheduleSave === 'function') window.scheduleSave();
            } catch (err) {
                if (isNewMode && newNode) {
                    newNode.running = false;
                    newNode.runStatus = 'failed';
                    newNode.runError = err.message || String(err);
                } else {
                    target.node.running = false;
                    target.node.runStatus = 'failed';
                    target.node.runError = err.message || String(err);
                }
                if (typeof window.render === 'function') window.render();
                if (window.showErrorModal) {
                    window.showErrorModal(err.message || String(err), 'Reve 修图失败');
                }
            }
        }
    };

    window.CanvasReve = CanvasReve;
    if (typeof document !== 'undefined') {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => CanvasReve.init());
        } else {
            CanvasReve.init();
        }
    }
})(typeof window !== 'undefined' ? window : globalThis);
