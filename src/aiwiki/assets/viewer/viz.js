(function () {
  const bundle = window.BUNDLE;
  const bundleName = window.BUNDLE_NAME;
  const palette = bundle.palette || {};
  const counts = bundle.counts || {};

  document.title = `${bundleName} — OKF Viewer`;
  document.getElementById("bundle-name").textContent = bundleName;

  // ── Stats ──────────────────────────────────────────────
  document.getElementById("stats").innerHTML =
    `<span><b>${bundle.nodes.length}</b> concepts</span>` +
    `<span class="dot">·</span>` +
    `<span><b>${bundle.edges.length}</b> links</span>` +
    `<span class="dot">·</span>` +
    `<span><b>${bundle.types.length}</b> types</span>`;

  // ── Type filter dropdown ───────────────────────────────
  const typeSelect = document.getElementById("filter-type");
  for (const t of bundle.types) {
    const opt = document.createElement("option");
    opt.value = t;
    opt.textContent = t;
    typeSelect.appendChild(opt);
  }

  // ── Indexes ────────────────────────────────────────────
  const nodeIndex = {};
  for (const n of bundle.nodes) nodeIndex[n.data.id] = n.data;
  const backlinks = {};
  for (const edge of bundle.edges) {
    const { source, target } = edge.data;
    (backlinks[target] ||= []).push(source);
  }

  // ── Color helpers ──────────────────────────────────────
  function hexToRgba(hex, a) {
    const h = (hex || "#94a3b8").replace("#", "");
    const r = parseInt(h.slice(0, 2), 16);
    const g = parseInt(h.slice(2, 4), 16);
    const b = parseInt(h.slice(4, 6), 16);
    return `rgba(${r}, ${g}, ${b}, ${a})`;
  }

  // ── Legend ─────────────────────────────────────────────
  const legend = document.getElementById("legend");
  const titleEl = document.createElement("div");
  titleEl.className = "legend-title";
  titleEl.textContent = "Concept types";
  legend.appendChild(titleEl);
  // Legend click focuses a type (emphasize its nodes, dim the rest) rather than
  // hiding it — the legend reads as a color key, so "click a color → show me
  // those" is the intuitive gesture. Multiple types can be focused at once;
  // click again to drop one. (The type dropdown still does single-type isolate.)
  const focusTypes = new Set();
  const legendItems = [];
  for (const t of bundle.types) {
    const color = palette[t] || "#94a3b8";
    const item = document.createElement("div");
    item.className = "legend-item";
    item.dataset.type = t;
    item.innerHTML =
      `<span class="legend-swatch" style="background:${color};color:${color}"></span>` +
      `<span class="legend-label">${t}</span>` +
      `<span class="legend-count">${counts[t] ?? ""}</span>`;
    item.addEventListener("click", () => {
      if (focusTypes.has(t)) focusTypes.delete(t);
      else focusTypes.add(t);
      applyTypeFocus();
    });
    legendItems.push(item);
    legend.appendChild(item);
  }

  // ── Cytoscape ──────────────────────────────────────────
  const cy = cytoscape({
    container: document.getElementById("graph"),
    elements: [...bundle.nodes, ...bundle.edges],
    minZoom: 0.2,
    maxZoom: 3,
    wheelSensitivity: 0.22,
    style: [
      {
        selector: "node",
        style: {
          "background-color": "data(color)",
          "label": "data(label)",
          "color": "#cdd4e0",
          "font-family": "Hanken Grotesk, PingFang SC, sans-serif",
          "font-size": 10.5,
          "font-weight": 500,
          "text-valign": "bottom",
          "text-margin-y": 6,
          "text-wrap": "wrap",
          "text-max-width": 118,
          "width": "data(size)",
          "height": "data(size)",
          "border-width": 1.5,
          "border-color": "#090d14",
          "underlay-color": "data(color)",
          "underlay-opacity": 0.18,
          "underlay-padding": 6,
          "text-outline-width": 2,
          "text-outline-color": "#0a0e16",
          "text-outline-opacity": 0.85,
          "transition-property": "underlay-opacity underlay-padding border-width opacity text-opacity",
          "transition-duration": "160ms",
        },
      },
      { selector: "node.hover", style: { "underlay-opacity": 0.34, "underlay-padding": 10 } },
      {
        selector: "node:selected",
        style: {
          "border-width": 3,
          "border-color": "#f3b14a",
          "underlay-color": "#f3b14a",
          "underlay-opacity": 0.4,
          "underlay-padding": 11,
          "color": "#ffffff",
          "font-weight": 600,
          "z-index": 99,
        },
      },
      {
        selector: "edge",
        style: {
          "width": 1.2,
          "line-color": "#3a4456",
          "line-opacity": 0.6,
          "target-arrow-color": "#3a4456",
          "target-arrow-shape": "triangle",
          "curve-style": "bezier",
          "arrow-scale": 0.85,
          "transition-property": "line-color line-opacity width",
          "transition-duration": "160ms",
        },
      },
      {
        selector: "edge.lit",
        style: {
          "line-color": "#f3b14a",
          "target-arrow-color": "#f3b14a",
          "line-opacity": 0.95,
          "width": 2,
          "z-index": 90,
        },
      },
      { selector: ".dim", style: { "opacity": 0.12 } },
      { selector: ".faded", style: { "opacity": 0.16 } },
      { selector: ".type-muted", style: { "opacity": 0.1 } },
      { selector: "node.nolabel", style: { "text-opacity": 0 } },
      { selector: ".hidden", style: { "display": "none" } },
    ],
    layout: coseLayout(),
  });

  function coseLayout() {
    // Spread larger graphs out so 60+ nodes don't pack into a hairball.
    const s = Math.sqrt(Math.max(1, bundle.nodes.length / 30));
    return {
      name: "cose",
      animate: false,
      padding: 50,
      nodeRepulsion: Math.round(9000 * s),
      idealEdgeLength: Math.round(110 * s),
      edgeElasticity: 120,
      gravity: 0.3,
      componentSpacing: Math.round(110 * s),
      nestingFactor: 0.9,
      randomize: false,
    };
  }

  cy.ready(() => cy.animate({ fit: { padding: 55 } }, { duration: 600, easing: "ease-out" }));

  // ── Level-of-detail labels ─────────────────────────────
  // On large graphs (>40 nodes), when zoomed out keep labels only on hub nodes
  // and the selected/hovered node, so the graph reads as a clean constellation;
  // zoom past LABEL_ZOOM (or open a concept) to reveal all labels. Small graphs
  // always show every label (unchanged behavior).
  const _outDeg = {};
  for (const e of bundle.edges) _outDeg[e.data.source] = (_outDeg[e.data.source] || 0) + 1;
  const _deg = (id) => (backlinks[id]?.length || 0) + (_outDeg[id] || 0);
  const _sorted = bundle.nodes.map((n) => _deg(n.data.id)).sort((a, b) => b - a);
  const HUB_DEG = bundle.nodes.length > 40 ? (_sorted[Math.floor(_sorted.length * 0.15)] || 99) : 0;
  const LABEL_ZOOM = 0.6;
  function updateLOD() {
    const showAll = bundle.nodes.length <= 40 || cy.zoom() >= LABEL_ZOOM;
    cy.batch(() => cy.nodes().forEach((n) => {
      const keep = showAll || n.selected() || n.hasClass("hover") ||
        (HUB_DEG > 0 && _deg(n.id()) >= HUB_DEG);
      n.toggleClass("nolabel", !keep);
    }));
  }
  cy.on("zoom", updateLOD);

  // ── Interactions ───────────────────────────────────────
  cy.on("tap", "node", (evt) => showDetail(evt.target.id()));
  cy.on("tap", (evt) => { if (evt.target === cy) clearFocus(); });
  cy.on("mouseover", "node", (evt) => { evt.target.addClass("hover"); updateLOD(); document.body.style.cursor = "pointer"; });
  cy.on("mouseout", "node", (evt) => { evt.target.removeClass("hover"); updateLOD(); document.body.style.cursor = ""; });

  let hintHidden = false;
  function fadeHint() {
    if (hintHidden) return;
    hintHidden = true;
    const h = document.getElementById("graph-hint");
    if (h) h.style.opacity = "0";
  }
  cy.on("tap zoom pan", fadeHint);

  document.getElementById("layout").addEventListener("change", (e) => {
    const name = e.target.value;
    const opts = name === "cose" ? coseLayout() : { name, animate: true, animationDuration: 500, padding: 50 };
    cy.layout(opts).run();
  });

  document.getElementById("reset").addEventListener("click", () => {
    document.getElementById("search").value = "";
    typeSelect.value = "";
    focusTypes.clear();
    legendItems.forEach((el) => el.classList.remove("active", "muted"));
    cy.elements().removeClass("dim faded lit hidden type-muted").unselect();
    document.getElementById("detail-content").hidden = true;
    document.getElementById("detail-empty").hidden = false;
    cy.animate({ fit: { padding: 55 } }, { duration: 500 });
  });

  document.getElementById("search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) { cy.elements().removeClass("dim"); return; }
    cy.nodes().forEach((n) => {
      const d = n.data();
      const hay = (d.label || "").toLowerCase() + " " + d.id.toLowerCase() + " " +
        (d.tags || []).join(" ").toLowerCase() + " " + (d.type || "").toLowerCase();
      n.toggleClass("dim", !hay.includes(q));
    });
    cy.edges().forEach((edge) => {
      edge.toggleClass("dim", edge.source().hasClass("dim") || edge.target().hasClass("dim"));
    });
  });

  typeSelect.addEventListener("change", (e) => {
    const t = e.target.value;
    if (!t) { cy.elements().removeClass("dim"); return; }
    cy.nodes().forEach((n) => n.toggleClass("dim", n.data("type") !== t));
    cy.edges().forEach((edge) => edge.toggleClass("dim", edge.source().hasClass("dim") || edge.target().hasClass("dim")));
  });

  function applyTypeFocus() {
    const active = focusTypes.size > 0;
    cy.nodes().forEach((n) => n.toggleClass("type-muted", active && !focusTypes.has(n.data("type"))));
    cy.edges().forEach((edge) => edge.toggleClass("type-muted", edge.source().hasClass("type-muted") || edge.target().hasClass("type-muted")));
    for (const item of legendItems) {
      item.classList.toggle("active", active && focusTypes.has(item.dataset.type));
      item.classList.toggle("muted", active && !focusTypes.has(item.dataset.type));
    }
  }

  function highlightNeighborhood(node) {
    const hood = node.closedNeighborhood();
    cy.elements().addClass("faded");
    hood.removeClass("faded");
    cy.edges().removeClass("lit");
    node.connectedEdges().removeClass("faded").addClass("lit");
  }

  // Clicking empty space lifts the neighborhood focus but keeps the dossier open.
  function clearFocus() {
    cy.elements().unselect().removeClass("faded lit");
  }

  // ── Detail dossier ─────────────────────────────────────
  function showDetail(conceptId, opts) {
    const data = nodeIndex[conceptId];
    if (!data) return;
    const color = palette[data.type] || data.color || "#94a3b8";
    const highlight = !opts || opts.highlight !== false;

    cy.elements().unselect();
    const node = cy.getElementById(conceptId);
    if (node) {
      node.select();
      if (highlight) highlightNeighborhood(node);
      else cy.elements().removeClass("faded lit");
      updateLOD();
    }

    const content = document.getElementById("detail-content");
    content.hidden = false;
    document.getElementById("detail-empty").hidden = true;
    // re-trigger fade-in
    content.style.animation = "none";
    void content.offsetWidth;
    content.style.animation = "";

    const chip = document.getElementById("detail-type");
    chip.textContent = data.type;
    chip.style.color = color;
    chip.style.background = hexToRgba(color, 0.13);
    chip.style.borderColor = hexToRgba(color, 0.32);

    // status + confidence badges
    const badges = document.getElementById("detail-badges");
    badges.innerHTML = "";
    if (data.status) {
      const b = document.createElement("span");
      b.className = "badge status-" + String(data.status).toLowerCase();
      b.textContent = data.status;
      badges.appendChild(b);
    }
    if (data.confidence) {
      const b = document.createElement("span");
      b.className = "badge conf-" + String(data.confidence).toLowerCase();
      b.textContent = data.confidence + " confidence";
      badges.appendChild(b);
    }

    document.getElementById("detail-title").textContent = data.label;
    document.getElementById("detail-id").textContent = conceptId;
    document.getElementById("detail-description").textContent = data.description || "—";

    const resourceEl = document.getElementById("detail-resource");
    resourceEl.innerHTML = "";
    const src = data.resource || data.source_ref || "";
    if (src && /^https?:\/\//.test(src)) {
      const a = document.createElement("a");
      a.href = src; a.textContent = src; a.target = "_blank"; a.rel = "noopener";
      resourceEl.appendChild(a);
    } else {
      resourceEl.textContent = src || "—";
    }

    const tagsEl = document.getElementById("detail-tags");
    tagsEl.innerHTML = "";
    if (data.tags && data.tags.length) {
      for (const t of data.tags) {
        const span = document.createElement("span");
        span.className = "tag";
        span.textContent = t;
        tagsEl.appendChild(span);
      }
    } else {
      tagsEl.textContent = "—";
    }

    const body = bundle.bodies[conceptId] || "";
    const bodyEl = document.getElementById("detail-body");
    bodyEl.innerHTML = marked.parse(body, { breaks: false, gfm: true });
    bodyEl.querySelectorAll("h1,h2,h3,h4,h5,h6").forEach((h) => {
      h.dataset.anchor = slugify(h.textContent);
    });
    rewriteInternalLinks(bodyEl, conceptId);
    if (window.hljs) {
      bodyEl.querySelectorAll("pre code").forEach((el) => {
        try { window.hljs.highlightElement(el); } catch (_) {}
      });
    }

    const bl = backlinks[conceptId] || [];
    const blSection = document.getElementById("detail-backlinks");
    const blList = document.getElementById("backlinks-list");
    blList.innerHTML = "";
    if (bl.length) {
      blSection.hidden = false;
      for (const s of bl) {
        const li = document.createElement("li");
        li.addEventListener("click", () => showDetail(s));
        const a = document.createElement("a");
        a.textContent = nodeIndex[s]?.label || s;
        li.appendChild(a);
        const muted = document.createElement("span");
        muted.className = "muted";
        muted.textContent = s;
        li.appendChild(muted);
        blList.appendChild(li);
      }
    } else {
      blSection.hidden = true;
    }

    document.getElementById("detail").scrollTop = 0;
    if (node) cy.animate({ center: { eles: node }, zoom: Math.max(cy.zoom(), 1.1) }, { duration: 260 });
  }

  // GitHub-style heading slug: lowercase, keep word chars + CJK, spaces -> dash.
  function slugify(s) {
    return String(s).trim().toLowerCase()
      .replace(/[^\w一-鿿 \-]/g, "")
      .replace(/\s+/g, "-");
  }

  // Resolve a relative .md href (relative to baseId's dir) to a concept id.
  function resolveRel(baseId, href) {
    const clean = href.split("#")[0].split("?")[0];
    if (!clean.endsWith(".md")) return null;
    const dir = baseId.split("/").slice(0, -1);
    const parts = clean.replace(/\.md$/, "").split("/");
    const stack = baseId.includes("/") ? [...dir] : [];
    for (const p of parts) {
      if (p === "." || p === "") continue;
      if (p === "..") stack.pop();
      else stack.push(p);
    }
    return stack.join("/");
  }

  function rewriteInternalLinks(root, baseId) {
    root.querySelectorAll("a[href]").forEach((a) => {
      const href = a.getAttribute("href");
      if (!href) return;
      if (!/^https?:\/\//.test(href) && href.endsWith(".md")) {
        const target = resolveRel(baseId, href);
        if (target && nodeIndex[target]) {
          a.className = "internal";
          a.setAttribute("href", "javascript:void(0)");
          a.addEventListener("click", (e) => { e.preventDefault(); showDetail(target); });
          return;
        }
      }
      // In-page anchor (e.g. a doc's table-of-contents): scroll within the
      // dossier instead of opening a useless new tab.
      if (href.startsWith("#")) {
        a.className = "internal";
        a.addEventListener("click", (e) => {
          e.preventDefault();
          const slug = slugify(decodeURIComponent(href.slice(1)));
          const tgt = slug && root.querySelector('[data-anchor="' + slug + '"]');
          if (tgt) tgt.scrollIntoView({ behavior: "smooth", block: "start" });
        });
        return;
      }
      a.className = "external";
      a.setAttribute("target", "_blank");
      a.setAttribute("rel", "noopener");
    });
  }

  // Auto-open a meaningful landing concept so the viewer looks alive on load:
  // prefer an overview, else the most-connected real concept (skip raw source
  // snapshots / untyped nodes). highlight:false = show the dossier + select the
  // node WITHOUT dimming the rest of the graph. The #detail-empty state remains
  // the fallback after Reset.
  const outDegree = {};
  for (const e of bundle.edges) outDegree[e.data.source] = (outDegree[e.data.source] || 0) + 1;
  const isReal = (n) =>
    !n.data.id.startsWith("sources/") && (n.data.type || "Unknown") !== "Unknown";
  const candidates = bundle.nodes.filter(isReal);
  const pool = candidates.length ? candidates : bundle.nodes;
  let initial = pool[0];
  let best = -1;
  for (const n of pool) {
    const deg = (backlinks[n.data.id]?.length || 0) + (outDegree[n.data.id] || 0);
    if (deg > best) { best = deg; initial = n; }
  }
  // An overview node wins if present, even if not the single most-connected.
  initial = pool.find((n) => /overview/i.test(n.data.type)) || initial;
  if (initial) showDetail(initial.data.id, { highlight: false });
})();
