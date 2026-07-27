class FlowBuilder {
    constructor(canvasId, paletteId) {
        this.canvas = document.getElementById(canvasId);
        this.palette = document.getElementById(paletteId);
        this.nodes = [];
        this.links = [];
        this.draggingNode = null;
        this.dragOffset = { x: 0, y: 0 };
        this.activeFlowId = null;
        
        // Linking state
        this.linkingStartNode = null;
        this.mouseX = 0;
        this.mouseY = 0;

        this.initPalette();
        this.initCanvas();
        this.render();
    }

    initPalette() {
        const tiles = this.palette.querySelectorAll('.flow-tile');
        tiles.forEach(tile => {
            tile.setAttribute('draggable', 'true');
            tile.addEventListener('dragstart', (e) => {
                e.dataTransfer.setData('tileType', tile.dataset.type);
                e.dataTransfer.setData('tileLabel', tile.dataset.label);
                e.dataTransfer.setData('tileCategory', tile.dataset.category);
            });
        });
    }

    initCanvas() {
        this.canvas.addEventListener('dragover', (e) => e.preventDefault());
        this.canvas.addEventListener('drop', (e) => {
            e.preventDefault();
            const type = e.dataTransfer.getData('tileType');
            const label = e.dataTransfer.getData('tileLabel');
            const category = e.dataTransfer.getData('tileCategory');
            
            const rect = this.canvas.getBoundingClientRect();
            const x = e.clientX - rect.left;
            const y = e.clientY - rect.top;

            this.addNode(type, label, category, x, y);
        });

        window.addEventListener('mousemove', (e) => {
            const rect = this.canvas.getBoundingClientRect();
            this.mouseX = e.clientX - rect.left;
            this.mouseY = e.clientY - rect.top;

            if (this.draggingNode) {
                this.draggingNode.x = this.mouseX - this.dragOffset.x;
                this.draggingNode.y = this.mouseY - this.dragOffset.y;
                this.render();
            }
            if (this.linkingStartNode) {
                this.render(); // Re-render for temporary line
            }
        });

        window.addEventListener('mouseup', () => {
            this.draggingNode = null;
        });

        // Click on canvas to cancel linking
        this.canvas.addEventListener('mousedown', (e) => {
            if (e.target === this.canvas || e.target.tagName === 'svg') {
                this.linkingStartNode = null;
                this.render();
            }
        });
    }

    addNode(type, label, category, x, y) {
        const id = 'node_' + Date.now();
        this.nodes.push({ id, type, label, category, x, y, data: this.defaultDataForType(type) });
        this.render();
    }

    removeNode(id) {
        this.nodes = this.nodes.filter(n => n.id !== id);
        this.links = this.links.filter(l => l.from !== id && l.to !== id);
        this.render();
    }

    render() {
        // Clear canvas but keep nodes separate from SVG
        this.canvas.innerHTML = ''; 
        
        // Render Nodes
        this.nodes.forEach(node => {
            const el = document.createElement('div');
            el.id = `node-el-${node.id}`;
            el.className = `flow-node node-${node.category}`;
            el.style.left = `${node.x}px`;
            el.style.top = `${node.y}px`;
            el.innerHTML = `
                <div class="node-header">
                    <span class="node-icon">${this.getIcon(node.type)}</span>
                    <span class="node-label">${node.label}</span>
                    <button class="node-delete" onclick="builder.removeNode('${node.id}')">×</button>
                </div>
                <div class="node-content">
                    ${this.renderNodeFields(node)}
                </div>
                <div class="node-port port-out" title="Connect to next node"></div>
                <div class="node-port port-in" title="Connection input"></div>
            `;

            el.addEventListener('mousedown', (e) => {
                if (e.target.classList.contains('port-out')) {
                    this.linkingStartNode = node;
                    e.stopPropagation();
                    return;
                }
                if (e.target.classList.contains('port-in')) {
                    if (this.linkingStartNode && this.linkingStartNode.id !== node.id) {
                        this.addLink(this.linkingStartNode.id, node.id);
                        this.linkingStartNode = null;
                        this.render();
                    }
                    e.stopPropagation();
                    return;
                }
                if (e.target.tagName === 'INPUT' || e.target.tagName === 'SELECT') return;
                
                this.draggingNode = node;
                this.dragOffset.x = e.clientX - el.getBoundingClientRect().left;
                this.dragOffset.y = e.clientY - el.getBoundingClientRect().top;
            });

            this.canvas.appendChild(el);
        });

        // Create SVG Layer for lines
        const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        svg.style.position = 'absolute';
        svg.style.width = '100%';
        svg.style.height = '100%';
        svg.style.top = '0';
        svg.style.left = '0';
        svg.style.pointerEvents = 'none';
        
        // Insert SVG behind nodes
        this.canvas.insertBefore(svg, this.canvas.firstChild);
        this.renderLinks(svg);
    }

    defaultDataForType(type) {
        if (type === 'message') return { keywords: '' };
        if (type === 'schedule') return { cron: '' };
        if (type === 'rss') return { feed_url: '' };
        if (type === 'limit') return { per_day: 1 };
        if (type === 'filter') return { contains: '' };
        if (type === 'post') return { message: '', channel_id: '' };
        if (type === 'genimg') return { prompt: '' };
        if (type === 'search') return { query: '' };
        if (type === 'llm_transform') return { prompt: '' };
        if (type === 'random_choice') return {};
        return {};
    }

    updateNodeData(nodeId, key, value) {
        const node = this.nodes.find(n => n.id === nodeId);
        if (!node) return;
        node.data = node.data || {};
        node.data[key] = value;
    }

    addLink(fromId, toId) {
        // Only one link between two specific nodes
        if (!this.links.find(l => l.from === fromId && l.to === toId)) {
            this.links.push({ from: fromId, to: toId });
        }
    }

    renderLinks(svg) {
        // Actual Links
        this.links.forEach(link => {
            const from = this.nodes.find(n => n.id === link.from);
            const to = this.nodes.find(n => n.id === link.to);
            if (from && to) {
                const fromEl = document.getElementById(`node-el-${from.id}`);
                const toEl = document.getElementById(`node-el-${to.id}`);
                const fromYOff = fromEl ? (fromEl.offsetHeight / 2) : 40;
                const toYOff = toEl ? (toEl.offsetHeight / 2) : 40;
                this.drawConnection(svg, from.x + 180, from.y + fromYOff, to.x, to.y + toYOff);
            }
        });

        // Temporary Link (while dragging from port)
        if (this.linkingStartNode) {
            const fromEl = document.getElementById(`node-el-${this.linkingStartNode.id}`);
            const fromYOff = fromEl ? (fromEl.offsetHeight / 2) : 40;
            this.drawConnection(svg, this.linkingStartNode.x + 180, this.linkingStartNode.y + fromYOff, this.mouseX, this.mouseY, true);
        }
    }

    drawConnection(svg, x1, y1, x2, y2, isTemp = false) {
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        const dx = Math.abs(x1 - x2) * 0.5;
        const d = `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
        path.setAttribute("d", d);
        path.setAttribute("stroke", isTemp ? "#5865F2" : "#80848e");
        path.setAttribute("stroke-width", "3");
        path.setAttribute("fill", "transparent");
        if (isTemp) path.setAttribute("stroke-dasharray", "5,5");
        svg.appendChild(path);
    }

    getIcon(type) {
        const icons = {
            'message': '💬', 'schedule': '⏰', 'rss': '📡',
            'limit': '⏳', 'filter': '🔍', 'post': '📤',
            'genimg': '🎨', 'search': '🌐', 'llm_transform': '🧠', 'random_choice': '🎲'
        };
        return icons[type] || '⚙️';
    }

    renderNodeFields(node) {
        const escape = (value) => this.escapeHtml(value ?? '');
        if (node.type === 'limit') {
            const v = node.data?.per_day ?? 1;
            return `<label>Per day</label>
                <input type="number" min="1" value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','per_day', this.value)">`;
        }
        if (node.type === 'message') {
            const v = node.data?.keywords ?? '';
            return `<label>Keywords</label>
                <input type="text" placeholder="Keywords..." value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','keywords', this.value)">
                <small style="font-size: 0.65rem; color: #888;">Vars: {author}, &lt;@{author_id}&gt;, {trigger_text}</small>`;
        }
        if (node.type === 'schedule') {
            const v = node.data?.cron ?? '';
            return `<label>Cron</label>
                <input type="text" placeholder="0 9 * * 1-5" value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','cron', this.value)">`;
        }
        if (node.type === 'rss') {
            const v = node.data?.feed_url ?? '';
            return `<label>Feed URL</label>
                <input type="text" placeholder="https://..." value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','feed_url', this.value)">`;
        }
        if (node.type === 'filter') {
            const v = node.data?.contains ?? '';
            return `<label>Contains</label>
                <input type="text" placeholder="filter text" value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','contains', this.value)">`;
        }
        if (node.type === 'post') {
            const msg = node.data?.message ?? '';
            const ch = node.data?.channel_id ?? '';
            let options = '<option value="">— select channel —</option>';
            if (window.guildChannels) {
                window.guildChannels.forEach(c => {
                    const sel = (c.id === ch) ? 'selected' : '';
                    options += `<option value="${c.id}" ${sel}># ${c.name}</option>`;
                });
            }
            return `<label>Message</label>
                <input type="text" placeholder="Post text..." value="${escape(msg)}"
                    oninput="builder.updateNodeData('${node.id}','message', this.value)">
                <small style="font-size: 0.65rem; color: #888;">Use e.g. {author}, &lt;@{author_id}&gt;, {last_output}</small>
                <label>Channel</label>
                <select style="width:100%;" onchange="builder.updateNodeData('${node.id}','channel_id', this.value)">
                    ${options}
                </select>`;
        }
        if (node.type === 'genimg') {
            const v = node.data?.prompt ?? '';
            return `<label>Prompt</label>
                <input type="text" placeholder="Image prompt..." value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','prompt', this.value)">`;
        }
        if (node.type === 'search') {
            const v = node.data?.query ?? '';
            return `<label>Query</label>
                <input type="text" placeholder="Search query..." value="${escape(v)}"
                    oninput="builder.updateNodeData('${node.id}','query', this.value)">`;
        }
        if (node.type === 'llm_transform') {
            const v = node.data?.prompt ?? '';
            return `<label>LLM Prompt</label>
                <textarea rows="3" placeholder="Summarize {last_output}..." 
                    oninput="builder.updateNodeData('${node.id}','prompt', this.value)">${escape(v)}</textarea>
                <small style="font-size: 0.65rem; color: #888;">Use e.g. {author}, {trigger_text}, {search_result}, {last_output}</small>`;
        }
        if (node.type === 'random_choice') {
            return `<label>50/50 Chance Route</label>
                <small style="font-size: 0.65rem; color: #888;">Flow randomly proceeds or halts here.</small>`;
        }
        return ``;
    }

    escapeHtml(value) {
        return String(value)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    serialize() {
        return {
            nodes: this.nodes,
            links: this.links
        };
    }

    load(flow) {
        if (!flow || !flow.nodes || !flow.links) return;
        this.nodes = flow.nodes;
        this.links = flow.links;
        this.render();
    }
}
