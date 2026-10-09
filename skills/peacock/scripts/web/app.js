/* Peacock 🦚 — 3D codebase map
   Renders the analyzer's graph as a glowing, navigable nebula using three.js
   (r128, vendored). Nodes are GPU points with a soft additive glow; edges are
   translucent line segments. Everything below is plain ES5-ish JS so it runs
   from file:// or the bundled local server with zero build step. */
(function () {
  "use strict";

  // ---- palette -------------------------------------------------------------
  var KIND_COLOR = {
    Module:   "#ffb454",   // amber   — directories (warm accent, few)
    File:     "#3a86ff",   // blue    — files (hero structure)
    Class:    "#c07cff",   // violet  — types
    Function: "#22e08a",   // green   — functions (distinct from File blue)
    Library:  "#ff5d8f"    // pink    — external deps
  };
  var KIND_ORDER = ["Module", "File", "Class", "Function", "Library"];
  // per-kind visual weighting: functions are a faint mist, structural nodes are stars
  var KIND_BRIGHT = { Module: 1.2, File: 1.05, Class: 1.05, Function: 0.92, Library: 1.1 };
  var KIND_SIZE   = { Module: 1.8, File: 1.35, Class: 1.25, Function: 1.15, Library: 1.4 };
  var EDGE_COLOR = { contains: "#7d8ae6", imports: "#66b8ff", calls: "#1fe6cf" };
  var HL_COLOR = "#ff4d4d";   // edges of the selected node: one hot red, not per-kind
  // faint ADDITIVE weights: sparse edges stay subtle, dense regions glow into haze
  var EDGE_ALPHA = { contains: 0.022, imports: 0.06, calls: 0.014 };
  // bold, traceable alphas for the sparse regime (small repos)
  var SPARSE_EDGE_ALPHA = { contains: 0.34, imports: 0.62, calls: 0.42 };

  // ---- atlas (flat 2D) constants ------------------------------------------
  var CELL_COLOR   = { 0: "#39456e", 1: "#252e4c" };   // district / module borders
  var ARC_SEGMENTS = 16;      // tessellation of one bezier arc
  var ARC_A0 = 0.12, ARC_A1 = 1.0;    // alpha at the source / at the target
  var INTRA_CAP = 1500;       // above this, same-module edges aggregate too
  var LOD_CELL_PX = 46;       // cell must be this wide on screen to be labelled
  var LOD_FILE_PX = 62;       // file pitch (26u) must be this wide to be labelled

  var METRIC_META = [
    ["navigability",        "AI Navigability"],
    ["context_efficiency",  "Context Efficiency"],
    ["modularity",          "Modularity & Coupling"],
    ["complexity",          "Complexity & Maintainability"],
    ["self_description",    "Self-Description"]
  ];

  // ---- state ---------------------------------------------------------------
  var DATA = null;
  var nodes = [], edges = [];
  var nodeById = {};
  var adjacency = {};              // id -> [ids]
  var hiddenKinds = {};
  // edge filters are per-view: the atlas encodes containment in its layout, so
  // `contains` starts off there, while the nebula still draws it.
  var hiddenEdgesByView = { nebula: {}, atlas: { contains: true } };
  var hiddenEdges = hiddenEdgesByView.nebula;
  var selectedId = null, hoverId = null;
  var pulseId = null, pulseStart = 0;
  var pinnedLabels = false;
  var SPARSE = false;   // small graphs: crisp stars + bold edges, not nebula haze
  var vscodeEnabled = false, vscodeAvailable = false;

  // ---- view mode -----------------------------------------------------------
  //  "nebula" — the original 3D glowing graph (perspective, additive, fog)
  //  "atlas"  — a flat orthographic map: directories are rectangles, files are
  //             a grid inside them, and every arc visibly starts and ends.
  var VIEW = "nebula";
  var atlasCells = [];             // packed rectangles from engine/layout.py
  var modKeyOf = {};               // node id -> module key
  var cellByKey = {};              // module key -> cell rect
  var expandedFile = null;         // file id whose symbols are fanned out

  var scene, camera, camPersp, camOrtho, renderer, controls, raycaster;
  var mouse = new THREE.Vector2(-2, -2);
  var pointGeo, pointMat, points, lineGeo, lineMat, lineSeg, hlGeo, hlSeg, starField;
  var cellSeg, cellGeo, labelLayer;

  // ==========================================================================
  //  Boot
  // ==========================================================================
  fetch("data.json").then(function (r) { return r.json(); }).then(function (d) {
    DATA = d;
    nodes = d.graph.nodes;
    edges = d.graph.edges;
    atlasCells = (d.graph.atlas && d.graph.atlas.cells) || [];
    SPARSE = nodes.length <= 900;   // regime switch: crisp vs nebula
    indexGraph();
    indexAtlas();
    buildUI();
    initThree();
    setView(initialView(), true);
    animate();
    setTimeout(function () {
      var el = document.getElementById("loading");
      if (el) { el.classList.add("gone"); setTimeout(function () { el.style.display = "none"; }, 600); }
      applyDemoParam();
      checkVSCode();
    }, 400);
  }).catch(function (e) {
    document.getElementById("loading").innerHTML =
      '<div class="loader-text">Could not load data.json — run peacock.py first.</div>';
    console.error(e);
  });

  function indexGraph() {
    nodeById = {}; adjacency = {};
    nodes.forEach(function (n) { nodeById[n.id] = n; adjacency[n.id] = []; });
    edges.forEach(function (e) {
      if (adjacency[e.source]) adjacency[e.source].push(e.target);
      if (adjacency[e.target]) adjacency[e.target].push(e.source);
    });
  }

  // ==========================================================================
  //  Atlas helpers
  // ==========================================================================
  function indexAtlas() {
    cellByKey = {};
    atlasCells.forEach(function (c) { if (c.depth === 1) cellByKey[c.key] = c; });
    modKeyOf = {};
    nodes.forEach(function (n) { modKeyOf[n.id] = moduleKey(n); });
  }

  /** Same rule as engine/layout.py `_module_key`: the directory a node lives in. */
  function moduleKey(n) {
    if (n.kind === "Library") return "__lib";
    if (n.kind === "Module") return n.path || ".";
    var p = n.path;
    if (p == null) return ".";
    var i = p.lastIndexOf("/");
    return i < 0 ? "." : p.slice(0, i);
  }

  /** Position accessors — the single place either layout is read from. */
  function nx(n) { return VIEW === "atlas" ? (n._ax != null ? n._ax : (n.ax || 0)) : n.x; }
  function ny(n) { return VIEW === "atlas" ? (n._ay != null ? n._ay : (n.ay || 0)) : n.y; }
  function nz(n) { return VIEW === "atlas" ? 0 : n.z; }
  function nvec(n) { return new THREE.Vector3(nx(n), ny(n), nz(n)); }

  function isSymbol(n) { return n.kind === "Class" || n.kind === "Function"; }

  /** In the atlas, symbols stay folded into their file until it is expanded. */
  function isFolded(n) {
    if (VIEW !== "atlas" || !isSymbol(n)) return false;
    if (!expandedFile) return true;
    var f = nodeById[expandedFile];
    return !f || n.path !== f.path;
  }

  /** The visible node an edge endpoint should attach to (a folded symbol
   *  hands its wires to the file that contains it). */
  function anchorOf(n) {
    if (!isFolded(n)) return n;
    return nodeById["file:" + (n.path || "")] || null;
  }

  function isHidden(n) { return !!hiddenKinds[n.kind] || isFolded(n); }

  // ==========================================================================
  //  UI panels
  // ==========================================================================
  function buildUI() {
    document.getElementById("repo-name").textContent = DATA.meta.name;
    document.title = "🦚 " + DATA.meta.name + " — Peacock map";

    // score ring
    var sc = DATA.scores;
    var ring = document.getElementById("score-ring");
    document.getElementById("score-num").textContent = Math.round(sc.overall);
    document.getElementById("score-grade").textContent = sc.grade;
    var col = scoreColor(sc.overall);
    ring.style.setProperty("--p", sc.overall);
    ring.style.background =
      "radial-gradient(closest-side, var(--panel-solid) 70%, transparent 71%)," +
      "conic-gradient(" + col + " " + sc.overall + "%, rgba(255,255,255,.08) 0)";
    document.getElementById("score-grade").style.color = col;

    buildMetrics();
    buildLegend();
    buildEdgeLegend();
    buildOverview();
    buildFileList();
    wireControls();
    renderInspector(null);   // populate the default summary card
  }

  function buildOverview() {
    var s = DATA.stats;
    var host = document.getElementById("overview");
    if (!host) return;
    var cells =
      cell("Files", fmt(s.files)) + cell("Lines", fmt(s.total_loc)) +
      cell("Functions", fmt(s.functions)) + cell("Classes", fmt(s.classes));
    var langs = Object.keys(s.languages || {});
    var max = Math.max.apply(null, langs.map(function (l) { return s.languages[l]; }).concat([1]));
    var bars = langs.slice(0, 6).map(function (l) {
      var w = (s.languages[l] / max) * 100;
      return '<div class="lang-row"><span class="lang-name">' + escapeHtml(l) + '</span>' +
        '<span class="lang-bar"><span style="width:' + w + '%"></span></span>' +
        '<span class="lang-n">' + s.languages[l] + '</span></div>';
    }).join("");
    host.innerHTML = '<div class="insp-grid">' + cells + '</div>' +
      '<div class="ov-langs">' + bars + '</div>';
  }

  function scoreColor(v) {
    // continuous red(0) → amber(55) → green(100) so every score reads distinctly
    var h = v < 55 ? (v / 55) * 45 : 45 + ((v - 55) / 45) * 95;
    return "hsl(" + Math.round(Math.max(0, Math.min(140, h))) + ", 78%, 56%)";
  }

  function buildMetrics() {
    var host = document.getElementById("metrics");
    host.innerHTML = "";
    METRIC_META.forEach(function (m) {
      var key = m[0], label = m[1], data = DATA.scores.metrics[key];
      var v = data.score, col = scoreColor(v);
      var el = document.createElement("div");
      el.className = "metric";
      var factors = Object.keys(data.factors).map(function (f) {
        return '<span class="factor-chip">' + f.replace(/_/g, " ") + " " +
          Math.round(data.factors[f]) + "</span>";
      }).join("");
      var notes = data.notes.map(function (n) { return "• " + n; }).join("<br>");
      el.innerHTML =
        '<div class="metric-head"><span class="metric-name">' + label + "</span>" +
        '<span class="metric-val" style="color:' + col + '">' + v.toFixed(0) + "</span></div>" +
        '<div class="metric-bar"><div class="metric-fill" style="background:' + col +
          ';width:' + v + '%"></div></div>' +
        '<div class="metric-factors">' + factors + "</div>" +
        (notes ? '<div class="metric-notes">' + notes + "</div>" : "");
      el.querySelector(".metric-head").addEventListener("click", function () {
        el.classList.toggle("open");
      });
      host.appendChild(el);
    });
  }

  function buildLegend() {
    var counts = {};
    nodes.forEach(function (n) { counts[n.kind] = (counts[n.kind] || 0) + 1; });
    var tot = DATA.stats.total_nodes;
    document.getElementById("node-total").textContent =
      nodes.length < tot ? "top " + fmt(nodes.length) + " of " + fmt(tot) : fmt(nodes.length);
    var host = document.getElementById("legend");
    host.innerHTML = "";
    KIND_ORDER.forEach(function (k) {
      if (!counts[k]) return;
      var row = document.createElement("div");
      row.className = "legend-row";
      row.innerHTML =
        '<span class="legend-dot" style="color:' + KIND_COLOR[k] + ';background:' + KIND_COLOR[k] + '"></span>' +
        '<span class="legend-name">' + k + "</span>" +
        '<span class="legend-count">' + counts[k] + "</span>";
      row.addEventListener("click", function () {
        hiddenKinds[k] = !hiddenKinds[k];
        row.classList.toggle("off", hiddenKinds[k]);
        rebuildVisibility();
        buildEdges();   // drop edges touching now-hidden node kinds
      });
      host.appendChild(row);
    });
  }

  function buildEdgeLegend() {
    var counts = DATA.stats.edge_kinds || {};
    var host = document.getElementById("edge-legend");
    host.innerHTML = "";
    Object.keys(EDGE_COLOR).forEach(function (k) {
      if (!counts[k]) return;
      var row = document.createElement("div");
      row.className = "edge-row";
      var off = !!hiddenEdges[k];
      row.classList.toggle("off", off);
      row.innerHTML =
        '<span class="edge-line" style="border-color:' + EDGE_COLOR[k] + '"></span>' +
        '<span class="legend-name">' + k + "</span>" +
        '<span class="legend-count">' + counts[k] +
        (off && VIEW === "atlas" ? " · in cells" : "") + "</span>";
      row.addEventListener("click", function () {
        hiddenEdges[k] = !hiddenEdges[k];
        row.classList.toggle("off", hiddenEdges[k]);
        buildEdges();
      });
      host.appendChild(row);
    });
  }

  function buildFileList() {
    var dl = document.getElementById("file-list");
    dl.innerHTML = "";
    nodes.filter(function (n) { return n.kind === "File"; })
      .slice(0, 800)
      .forEach(function (n) {
        var o = document.createElement("option");
        o.value = n.path || n.label;
        dl.appendChild(o);
      });
  }

  // ==========================================================================
  //  three.js scene
  // ==========================================================================
  function initThree() {
    var stage = document.getElementById("stage");
    var w = stage.clientWidth, h = stage.clientHeight;
    scene = new THREE.Scene();
    scene.fog = new THREE.FogExp2(0x04050a, 0.0016);

    camPersp = new THREE.PerspectiveCamera(58, w / h, 0.5, 4000);
    camPersp.position.set(0, 40, 320);
    // The atlas camera is orthographic on purpose: with no perspective there is
    // no depth to misread, so an edge can never look like it leaves for infinity.
    // The frustum is sized in pixels, so `zoom` is literally pixels-per-unit.
    camOrtho = new THREE.OrthographicCamera(-w / 2, w / 2, h / 2, -h / 2, -4000, 4000);
    camOrtho.position.set(0, 0, 800);
    camera = camPersp;

    renderer = new THREE.WebGLRenderer({
      canvas: document.getElementById("scene"), antialias: true, alpha: true
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(w, h);

    makeControls();

    raycaster = new THREE.Raycaster();
    raycaster.params.Points.threshold = 3;

    starField = makeStars();
    scene.add(starField);

    labelLayer = document.createElement("div");
    labelLayer.style.cssText = "position:absolute;inset:0;pointer-events:none;z-index:3;overflow:hidden";
    stage.appendChild(labelLayer);

    window.addEventListener("resize", onResize);
    var cv = renderer.domElement;
    cv.addEventListener("mousemove", onMove);
    cv.addEventListener("click", onClick);
    cv.addEventListener("mouseleave", function () { mouse.set(-2, -2); setHover(null); });
    ["pointerdown", "wheel"].forEach(function (ev) {
      cv.addEventListener(ev, function () { controls.autoRotate = false; });
    });
  }

  /** (Re)build OrbitControls for the current camera. The atlas locks rotation
   *  down to pan + zoom — a flat map you can tumble is a 3D map again. */
  function makeControls() {
    var tgt = controls ? controls.target.clone() : new THREE.Vector3();
    if (controls) controls.dispose();
    controls = new THREE.OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.target.copy(tgt);
    if (VIEW === "atlas") {
      controls.enableRotate = false;
      controls.autoRotate = false;
      controls.screenSpacePanning = true;
      controls.mouseButtons = { LEFT: THREE.MOUSE.PAN, MIDDLE: THREE.MOUSE.DOLLY,
                                RIGHT: THREE.MOUSE.PAN };
      controls.touches = { ONE: THREE.TOUCH.PAN, TWO: THREE.TOUCH.DOLLY_PAN };
    } else {
      controls.rotateSpeed = 0.7;
      controls.autoRotate = true;
      controls.autoRotateSpeed = 0.28;
    }
    controls.update();
  }

  function makeStars() {
    var g = new THREE.BufferGeometry(), n = 1400, pos = new Float32Array(n * 3);
    for (var i = 0; i < n; i++) {
      var r = 900 + Math.random() * 1600;
      var th = Math.random() * Math.PI * 2, ph = Math.acos(2 * Math.random() - 1);
      pos[i*3] = r * Math.sin(ph) * Math.cos(th);
      pos[i*3+1] = r * Math.sin(ph) * Math.sin(th);
      pos[i*3+2] = r * Math.cos(ph);
    }
    g.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    var m = new THREE.PointsMaterial({ color: 0x8899cc, size: 1.4, sizeAttenuation: false,
      transparent: true, opacity: 0.55 });
    return new THREE.Points(g, m);
  }

  // ---- node points with glow shader ---------------------------------------
  function buildPoints() {
    if (points) { scene.remove(points); pointGeo.dispose(); pointMat.dispose(); }
    var n = nodes.length;
    var pos = new Float32Array(n * 3);
    var col = new Float32Array(n * 3);
    var siz = new Float32Array(n);
    var alp = new Float32Array(n);
    var atlas = VIEW === "atlas";
    nodes.forEach(function (nd, i) {
      pos[i*3] = nx(nd); pos[i*3+1] = ny(nd); pos[i*3+2] = nz(nd);
      var c = new THREE.Color(KIND_COLOR[nd.kind] || "#ffffff");
      col[i*3] = c.r; col[i*3+1] = c.g; col[i*3+2] = c.b;
      var kb = KIND_BRIGHT[nd.kind] || 0.8, ks = KIND_SIZE[nd.kind] || 1;
      // atlas sizes are world units (gl_PointSize = asize * pixels-per-unit),
      // nebula sizes are the old screen-space falloff
      siz[i] = atlas
        ? (2.6 + Math.min(9, (nd.size || 2)) * 0.62) * ks
        : Math.min(9, (nd.size || 2)) * (SPARSE ? 2.7 : 4.2) * ks;
      nd._b = atlas ? 1 : kb;
      alp[i] = nd._b;
      nd._i = i;
    });
    pointGeo = new THREE.BufferGeometry();
    pointGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    pointGeo.setAttribute("acolor", new THREE.BufferAttribute(col, 3));
    pointGeo.setAttribute("asize", new THREE.BufferAttribute(siz, 1));
    pointGeo.setAttribute("aalpha", new THREE.BufferAttribute(alp, 1));

    pointMat = new THREE.ShaderMaterial({
      uniforms: {
        uScale: { value: renderer.domElement.height * 0.5 },
        uNear:  { value: 120 }, uFar: { value: 520 },
        uMax:   { value: atlas ? 130.0 : (SPARSE ? 26.0 : 60.0) },  // cap so close nodes aren't blobs
        uHaloC: { value: SPARSE ? 0.07 : 0.17 },          // halo colour weight
        uHaloA: { value: SPARSE ? 0.04 : 0.09 },          // halo alpha weight
        uFlat:  { value: atlas ? 1.0 : 0.0 },             // solid disc + rim, no glow
        uPix:   { value: 1.0 }                            // pixels per world unit (ortho)
      },
      vertexShader:
        "attribute vec3 acolor; attribute float asize; attribute float aalpha;" +
        "varying vec3 vC; varying float vA; varying float vDepth;" +
        "uniform float uScale; uniform float uMax; uniform float uFlat; uniform float uPix;" +
        "void main(){ vC=acolor; vA=aalpha;" +
        " vec4 mv = modelViewMatrix * vec4(position,1.0);" +
        " vDepth = -mv.z;" +
        // flat/orthographic: size in world units so dots grow as you zoom in
        " float ps = (uFlat > 0.5) ? asize * uPix : asize * uScale / -mv.z;" +
        " gl_PointSize = clamp(ps, 2.0, uMax);" +
        " gl_Position = projectionMatrix * mv; }",
      fragmentShader:
        "varying vec3 vC; varying float vA; varying float vDepth;" +
        "uniform float uNear; uniform float uFar; uniform float uHaloC; uniform float uHaloA;" +
        "uniform float uFlat;" +
        "void main(){ vec2 uv = gl_PointCoord - 0.5; float d = length(uv);" +
        " if(d>0.5) discard;" +
        " if(uFlat > 0.5){" +
        // atlas: an opaque disc with a bright rim. No halo, no depth ramp —
        // a node is a thing at a place, not a smudge of light.
        "   float fill = smoothstep(0.44, 0.38, d);" +
        "   float rim  = smoothstep(0.50, 0.43, d) - fill;" +
        "   vec3 c = vC * fill + mix(vC, vec3(1.0), 0.45) * rim;" +
        "   float a = clamp(vA, 0.0, 1.0) * (fill * 0.98 + rim * 0.92);" +
        "   gl_FragColor = vec4(c, a); return; }" +
        " float halo = smoothstep(0.5, 0.0, d);" +        // wide soft glow
        " float core = smoothstep(0.15, 0.0, d);" +       // bright pinpoint (no white)
        " vec3 c = vC * (halo*uHaloC + core*1.0);" +
        " float a = (halo*uHaloA + core*0.62) * vA;" +
        // depth cue: nearer = brighter, farther = dimmer (real 3D read)
        " float df = clamp((uFar - vDepth) / (uFar - uNear), 0.32, 1.0);" +
        " a *= df; c *= (0.5 + 0.5*df);" +
        // soft Reinhard so per-fragment colour never hard-clips to white
        " c = c / (0.75 + c);" +
        " gl_FragColor = vec4(c, a); }",
      transparent: true, depthWrite: false,
      blending: atlas ? THREE.NormalBlending : THREE.AdditiveBlending
    });
    points = new THREE.Points(pointGeo, pointMat);
    points.frustumCulled = false;
    points.renderOrder = 2;
    scene.add(points);
    rebuildVisibility();
    if (!camera.userData.fitted) { fitCamera(false); camera.userData.fitted = true; }
  }

  function fitCamera(animated) {
    var box = new THREE.Box3();
    var any = false;
    nodes.forEach(function (n) {
      if (isHidden(n)) return;
      box.expandByPoint(nvec(n)); any = true;
    });
    if (VIEW === "atlas") {
      // the cells are part of the map, so fit to them too
      atlasCells.forEach(function (cl) {
        box.expandByPoint(new THREE.Vector3(cl.x, cl.y, 0));
        box.expandByPoint(new THREE.Vector3(cl.x + cl.w, cl.y + cl.h, 0));
        any = true;
      });
    }
    if (!any) return;
    var c = box.getCenter(new THREE.Vector3());
    var size = box.getSize(new THREE.Vector3());

    if (camera.isOrthographicCamera) {
      var el = renderer.domElement;
      var z = Math.min(el.clientWidth / (size.x + 90), el.clientHeight / (size.y + 90));
      if (!isFinite(z) || z <= 0) z = 1;
      var end = new THREE.Vector3(c.x, c.y, camera.position.z);
      if (animated) flyToVec(new THREE.Vector3(c.x, c.y, 0), end, z);
      else {
        camera.position.copy(end); camera.zoom = z; camera.updateProjectionMatrix();
        controls.target.set(c.x, c.y, 0); controls.update();
      }
      return;
    }

    var radius = Math.max(size.x, size.y, size.z) * 0.5 || 120;
    var dist = radius / Math.tan(THREE.MathUtils.degToRad(camera.fov * 0.5)) * 1.15 + 40;
    var camPos = c.clone().add(new THREE.Vector3(radius * 0.05, radius * 0.12, dist));
    if (animated) flyToVec(c, camPos);
    else { camera.position.copy(camPos); controls.target.copy(c); controls.update(); }
  }

  // ---- edges ---------------------------------------------------------------
  function buildEdges() {
    if (lineSeg) { scene.remove(lineSeg); lineGeo.dispose(); lineMat.dispose(); }
    if (VIEW === "atlas") { buildAtlasEdges(); return; }
    var vis = edges.filter(function (e) {
      var a = nodeById[e.source], b = nodeById[e.target];
      return !hiddenEdges[e.kind] && a && b &&
        !hiddenKinds[a.kind] && !hiddenKinds[b.kind];
    });
    // Edge brightness is density-aware. Sparse graphs get bold, clearly-traceable
    // lines (every kind, incl. long-range `contains`, above a visible floor);
    // huge graphs keep the faint additive haze that avoids white-out.
    var edgeScale = Math.max(1, Math.min(24, 4200 / Math.max(vis.length, 1)));
    var pos = new Float32Array(vis.length * 6);
    var col = new Float32Array(vis.length * 6);
    vis.forEach(function (e, i) {
      var a = nodeById[e.source], b = nodeById[e.target];
      pos[i*6] = a.x; pos[i*6+1] = a.y; pos[i*6+2] = a.z;
      pos[i*6+3] = b.x; pos[i*6+4] = b.y; pos[i*6+5] = b.z;
      var c = new THREE.Color(EDGE_COLOR[e.kind] || "#556");
      var al = SPARSE
        ? (SPARSE_EDGE_ALPHA[e.kind] || 0.4)
        : Math.min(0.95, (EDGE_ALPHA[e.kind] || 0.03) * edgeScale);
      for (var k = 0; k < 2; k++) {
        col[i*6+k*3] = c.r * al; col[i*6+k*3+1] = c.g * al; col[i*6+k*3+2] = c.b * al;
      }
    });
    lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    lineGeo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    lineMat = new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 1.0,
      blending: THREE.AdditiveBlending, depthWrite: false
    });
    lineSeg = new THREE.LineSegments(lineGeo, lineMat);
    lineSeg.frustumCulled = false;
    lineSeg.renderOrder = 1;
    scene.add(lineSeg);
  }

  // ---- atlas edges ---------------------------------------------------------
  // The rules that make the map readable, in order:
  //   `contains`  — never drawn; the cell a node sits in *is* the containment.
  //   library     — drawn only for the selection; `lib:java` alone has a
  //                 four-figure fan-in and would bury everything else.
  //   near        — same module, or touching the selection: exact node-to-node
  //                 arcs, so you can trace a specific wire.
  //   far         — everything else collapses to ONE arc per module pair,
  //                 weighted by how many edges it stands for.
  // Every arc is a bezier that brightens toward its target, so it visibly
  // arrives somewhere instead of fading out mid-flight.
  function buildAtlasEdges() {
    var focus = focusModules();
    var detail = [], agg = {};
    var intra = 0;
    edges.forEach(function (e) {
      if (hiddenEdges[e.kind]) return;
      var sa = nodeById[e.source], sb = nodeById[e.target];
      if (!sa || !sb || hiddenKinds[sa.kind] || hiddenKinds[sb.kind]) return;
      var touchesSel = selectedId &&
        (e.source === selectedId || e.target === selectedId);
      if ((sa.kind === "Library" || sb.kind === "Library") && !sa._live && !sb._live) {
        if (!touchesSel) return;              // libraries only wire up on demand
      }
      var a = anchorOf(sa), b = anchorOf(sb);
      if (!a || !b || a === b) return;
      var ma = modKeyOf[a.id], mb = modKeyOf[b.id];
      var near = touchesSel || (focus && (focus[ma] || focus[mb]));
      if (ma === mb) {
        intra++;
        if (near || !focus) { detail.push([a, b, e.kind, 1]); return; }
        return;                               // same-cell noise while focused elsewhere
      }
      if (near) { detail.push([a, b, e.kind, 1]); return; }
      var key = ma < mb ? ma + "\u0000" + mb + "\u0000" + e.kind
                        : mb + "\u0000" + ma + "\u0000" + e.kind;
      var g = agg[key];
      if (g) { g[3]++; }
      else { agg[key] = [a, b, e.kind, 1, ma, mb]; }
    });
    // a huge repo's intra-module chatter is still a hairball — fold it away
    if (intra > INTRA_CAP && !focus) {
      detail = detail.filter(function (d) { return modKeyOf[d[0].id] !== modKeyOf[d[1].id]; });
    }

    var arcs = [];
    detail.forEach(function (d) {
      arcs.push({ a: nvec(d[0]), b: nvec(d[1]), kind: d[2], w: 1, bulge: 0.13 });
    });
    Object.keys(agg).forEach(function (k) {
      var g = agg[k];
      var pa = cellAnchor(g[4], g[5]), pb = cellAnchor(g[5], g[4]);
      arcs.push({ a: pa || nvec(g[0]), b: pb || nvec(g[1]), kind: g[2],
                  w: g[3], bulge: 0.2 });
    });

    if (!arcs.length) { lineGeo = null; lineMat = null; lineSeg = null; return; }

    // a heavy route is drawn as a bundle of parallel strands, so how much
    // traffic it carries is legible as thickness, not just brightness
    var segs = ARC_SEGMENTS, strands = 0;
    arcs.forEach(function (a) {
      a.strands = Math.max(1, Math.min(4, Math.round(1 + Math.log(a.w) / Math.LN2 / 2)));
      strands += a.strands;
    });
    var verts = strands * segs * 2;
    var pos = new Float32Array(verts * 3);
    var col = new Float32Array(verts * 3);
    var alp = new Float32Array(verts);
    var o = 0;
    var mid = new THREE.Vector3();
    arcs.forEach(function (arc) {
      var c = new THREE.Color(EDGE_COLOR[arc.kind] || "#8899bb");
      // weight of an aggregated route: log-scaled so one busy pair does not
      // drown out ten quiet ones
      var wf = Math.min(1, 0.55 + Math.log(1 + arc.w) * 0.16);
      var dx = arc.b.x - arc.a.x, dy = arc.b.y - arc.a.y;
      var len = Math.sqrt(dx * dx + dy * dy) || 1;
      var px = -dy / len, py = dx / len;      // unit normal
      for (var st = 0; st < arc.strands; st++) {
        var off = (st - (arc.strands - 1) / 2) * 0.85;
        var bul = len * arc.bulge + off;
        mid.set(arc.a.x + dx * 0.5 + px * bul, arc.a.y + dy * 0.5 + py * bul, 0);
        var ax = arc.a.x + px * off, ay = arc.a.y + py * off;
        var bx = arc.b.x + px * off, by = arc.b.y + py * off;
        for (var s = 0; s < segs; s++) {
          var t0 = s / segs, t1 = (s + 1) / segs;
          for (var k = 0; k < 2; k++) {
            var t = k ? t1 : t0, u = 1 - t;
            pos[o*3]   = u*u*ax + 2*u*t*mid.x + t*t*bx;
            pos[o*3+1] = u*u*ay + 2*u*t*mid.y + t*t*by;
            pos[o*3+2] = 0.0;
            col[o*3] = c.r; col[o*3+1] = c.g; col[o*3+2] = c.b;
            // brightest where it ARRIVES — this is what kills the "ray to
            // infinity" read of the 3D view
            alp[o] = (ARC_A0 + (ARC_A1 - ARC_A0) * t * t) * wf;
            o++;
          }
        }
      }
    });

    lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    lineGeo.setAttribute("acolor", new THREE.BufferAttribute(col, 3));
    lineGeo.setAttribute("aalpha", new THREE.BufferAttribute(alp, 1));
    lineMat = arcMaterial();
    lineSeg = new THREE.LineSegments(lineGeo, lineMat);
    lineSeg.frustumCulled = false;
    lineSeg.renderOrder = 1;
    scene.add(lineSeg);
  }

  /** Per-vertex alpha needs a real attribute: with NormalBlending (which is
   *  what stops dense regions blowing out to white) vertex colour alone
   *  cannot express transparency. */
  function arcMaterial(boost) {
    return new THREE.ShaderMaterial({
      uniforms: { uBoost: { value: boost || 1.0 } },
      vertexShader:
        "attribute vec3 acolor; attribute float aalpha;" +
        "varying vec3 vC; varying float vA;" +
        "void main(){ vC=acolor; vA=aalpha;" +
        " gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }",
      fragmentShader:
        "varying vec3 vC; varying float vA; uniform float uBoost;" +
        "void main(){ gl_FragColor = vec4(vC, clamp(vA*uBoost, 0.0, 1.0)); }",
      transparent: true, depthWrite: false, blending: THREE.NormalBlending
    });
  }

  /** Where an aggregated route leaves cell `from` heading for cell `to`:
   *  the point on `from`'s border along the line joining the two centres, so
   *  routes hug the outside of cells instead of cutting through their files. */
  function cellAnchor(from, to) {
    var A = cellByKey[from], B = cellByKey[to];
    if (!A || !B) return null;
    var ax = A.x + A.w / 2, ay = A.y + A.h / 2;
    var bx = B.x + B.w / 2, by = B.y + B.h / 2;
    var dx = bx - ax, dy = by - ay;
    if (!dx && !dy) return new THREE.Vector3(ax, ay, 0);
    var sx = dx ? (A.w / 2) / Math.abs(dx) : Infinity;
    var sy = dy ? (A.h / 2) / Math.abs(dy) : Infinity;
    var s = Math.min(sx, sy);
    return new THREE.Vector3(ax + dx * s, ay + dy * s, 0);
  }

  /** Module keys the current selection touches (null when nothing selected). */
  function focusModules() {
    if (!selectedId || !nodeById[selectedId]) return null;
    var f = {};
    f[modKeyOf[selectedId]] = 1;
    (adjacency[selectedId] || []).forEach(function (id) {
      if (modKeyOf[id] != null) f[modKeyOf[id]] = 1;
    });
    return f;
  }

  // ---- atlas cell chrome ---------------------------------------------------
  function buildCells() {
    if (cellSeg) { scene.remove(cellSeg); cellGeo.dispose(); cellSeg.material.dispose(); }
    cellSeg = null;
    if (VIEW !== "atlas" || !atlasCells.length) return;
    var pos = new Float32Array(atlasCells.length * 8 * 3);
    var col = new Float32Array(atlasCells.length * 8 * 3);
    var o = 0;
    atlasCells.forEach(function (cl) {
      var c = new THREE.Color(CELL_COLOR[cl.depth] || CELL_COLOR[1]);
      var x0 = cl.x, y0 = cl.y, x1 = cl.x + cl.w, y1 = cl.y + cl.h;
      var pts = [[x0,y0,x1,y0],[x1,y0,x1,y1],[x1,y1,x0,y1],[x0,y1,x0,y0]];
      pts.forEach(function (p) {
        pos[o*3]=p[0]; pos[o*3+1]=p[1]; pos[o*3+2]=-1;
        col[o*3]=c.r; col[o*3+1]=c.g; col[o*3+2]=c.b; o++;
        pos[o*3]=p[2]; pos[o*3+1]=p[3]; pos[o*3+2]=-1;
        col[o*3]=c.r; col[o*3+1]=c.g; col[o*3+2]=c.b; o++;
      });
    });
    cellGeo = new THREE.BufferGeometry();
    cellGeo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    cellGeo.setAttribute("color", new THREE.BufferAttribute(col, 3));
    cellSeg = new THREE.LineSegments(cellGeo, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.9, depthWrite: false
    }));
    cellSeg.frustumCulled = false;
    cellSeg.renderOrder = 0;
    scene.add(cellSeg);
  }

  function rebuildVisibility() {
    if (!pointGeo) return;
    var alp = pointGeo.getAttribute("aalpha");
    var nb = selectedId ? neighbourSet(selectedId) : null;
    var dim = VIEW === "atlas" ? 0.22 : 0.12;
    nodes.forEach(function (nd, i) {
      var b = nd._b == null ? 1 : nd._b;
      if (isHidden(nd)) { alp.array[i] = 0; return; }
      if (selectedId) {
        alp.array[i] = nd.id === selectedId ? 1.35
          : nb[nd.id] ? Math.max(b, 0.95) : b * dim;
      } else {
        alp.array[i] = b;
      }
    });
    alp.needsUpdate = true;
  }

  function neighbourSet(id) {
    var s = {}; (adjacency[id] || []).forEach(function (x) { s[x] = 1; }); s[id] = 1;
    return s;
  }

  // ==========================================================================
  //  Interaction
  // ==========================================================================
  function onResize() {
    var stage = document.getElementById("stage");
    var w = stage.clientWidth, h = stage.clientHeight;
    camPersp.aspect = w / h; camPersp.updateProjectionMatrix();
    camOrtho.left = -w / 2; camOrtho.right = w / 2;
    camOrtho.top = h / 2; camOrtho.bottom = -h / 2;
    camOrtho.updateProjectionMatrix();
    renderer.setSize(w, h);
    if (pointMat) pointMat.uniforms.uScale.value = renderer.domElement.height * 0.5;
  }

  function onMove(e) {
    var r = renderer.domElement.getBoundingClientRect();
    mouse.x = ((e.clientX - r.left) / r.width) * 2 - 1;
    mouse.y = -((e.clientY - r.top) / r.height) * 2 + 1;
    moveHoverLabel(e.clientX - r.left, e.clientY - r.top);
  }

  function pick() {
    if (!points) return null;
    raycaster.params.Points.threshold = camera.isOrthographicCamera
      ? 7 / (camera.zoom || 1)
      : camera.position.distanceTo(controls.target) * 0.012 + 1.5;
    raycaster.setFromCamera(mouse, camera);
    var hits = raycaster.intersectObject(points);
    for (var i = 0; i < hits.length; i++) {
      var nd = nodes[hits[i].index];
      if (nd && !isHidden(nd)) return nd;
    }
    return null;
  }

  function onClick() {
    var nd = pick();
    if (nd) { selectNode(nd.id); }
    else { selectNode(null); }
  }

  function setHover(id) {
    hoverId = id;
    var lbl = document.getElementById("hover-label");
    if (!id) { lbl.style.opacity = 0; document.body.style.cursor = "default"; return; }
    var nd = nodeById[id];
    lbl.innerHTML = '<span class="hl-kind">' + nd.kind + " · </span>" + escapeHtml(nd.label);
    lbl.style.opacity = 1;
    document.body.style.cursor = "pointer";
  }

  function moveHoverLabel(x, y) {
    var lbl = document.getElementById("hover-label");
    lbl.style.left = x + "px"; lbl.style.top = y + "px";
  }

  function selectNode(id) {
    selectedId = id;
    pulseId = null;
    if (VIEW === "atlas") {
      // in the atlas a file's symbols stay folded away until you pick the file
      var want = id && nodeById[id] && nodeById[id].kind === "File" ? id : null;
      if (want !== expandedFile) { expandedFile = want; expandSymbols(want); buildPoints(); }
      buildEdges();          // selection changes which wires are worth drawing
    }
    rebuildVisibility();
    updateHighlight();
    renderInspector(id ? nodeById[id] : null);
    if (id) flyTo(nodeById[id]);
  }

  /** Fan a file's classes/functions out onto a few concentric rings around it.
   *  Ring spacing is fixed so a 40-symbol file grows outward instead of
   *  swallowing its neighbours. */
  function expandSymbols(fileId) {
    nodes.forEach(function (n) { if (isSymbol(n)) { n._ax = null; n._ay = null; } });
    if (!fileId) return;
    var f = nodeById[fileId];
    if (!f) return;
    var syms = nodes.filter(function (n) { return isSymbol(n) && n.path === f.path; });
    var i = 0, r = 11;
    while (i < syms.length) {
      var cap = Math.max(4, Math.floor((2 * Math.PI * r) / 7));
      var take = Math.min(cap, syms.length - i);
      for (var k = 0; k < take; k++) {
        var a = -Math.PI / 2 + (k / take) * Math.PI * 2;
        syms[i + k]._ax = (f.ax || 0) + Math.cos(a) * r;
        syms[i + k]._ay = (f.ay || 0) + Math.sin(a) * r;
      }
      i += take; r += 7;
    }
  }

  function updateHighlight() {
    if (hlSeg) { scene.remove(hlSeg); hlGeo.dispose(); }
    var hid = selectedId || pulseId;
    if (!hid) { hlSeg = null; return; }
    var inc = edges.filter(function (e) {
      return (e.source === hid || e.target === hid) &&
             !hiddenEdges[e.kind] && nodeById[e.source] && nodeById[e.target];
    });
    if (!inc.length) { hlSeg = null; return; }
    var pos = new Float32Array(inc.length * 6);
    var col = new Float32Array(inc.length * 6);
    var n = 0;
    inc.forEach(function (e) {
      var a = nodeById[e.source], b = nodeById[e.target];
      if (VIEW === "atlas") { a = anchorOf(a); b = anchorOf(b); if (!a || !b || a === b) return; }
      var pa = nvec(a), pb = nvec(b);
      pos[n*6]=pa.x;pos[n*6+1]=pa.y;pos[n*6+2]=pa.z;
      pos[n*6+3]=pb.x;pos[n*6+4]=pb.y;pos[n*6+5]=pb.z;
      var c = new THREE.Color(HL_COLOR);
      for (var k=0;k<2;k++){ col[n*6+k*3]=c.r;col[n*6+k*3+1]=c.g;col[n*6+k*3+2]=c.b; }
      n++;
    });
    if (!n) { hlSeg = null; return; }
    hlGeo = new THREE.BufferGeometry();
    hlGeo.setAttribute("position", new THREE.BufferAttribute(pos.subarray(0, n * 6), 3));
    hlGeo.setAttribute("color", new THREE.BufferAttribute(col.subarray(0, n * 6), 3));
    hlSeg = new THREE.LineSegments(hlGeo, new THREE.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: VIEW === "atlas" ? 0.55 : 0.9,
      depthWrite: false,
      blending: VIEW === "atlas" ? THREE.NormalBlending : THREE.AdditiveBlending
    }));
    hlSeg.frustumCulled = false;
    hlSeg.renderOrder = 1;
    scene.add(hlSeg);
  }

  var flyT = null;
  function flyTo(nd) {
    var target = nvec(nd);
    if (camera.isOrthographicCamera) {
      // pan to it, and only zoom in if we are so far out it would be a speck
      var end = new THREE.Vector3(target.x, target.y, camera.position.z);
      flyToVec(new THREE.Vector3(target.x, target.y, 0), end,
               Math.max(camera.zoom, 3.2));
      return;
    }
    var dist = Math.max(45, (nd.size || 2) * 14);
    var dir = camera.position.clone().sub(controls.target).normalize();
    var camEnd = target.clone().add(dir.multiplyScalar(dist));
    var t0 = performance.now(), dur = 620;
    var camStart = camera.position.clone(), tgtStart = controls.target.clone();
    if (flyT) cancelAnimationFrame(flyT);
    (function step(now) {
      var k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      camera.position.lerpVectors(camStart, camEnd, e);
      controls.target.lerpVectors(tgtStart, target, e);
      controls.update();
      if (k < 1) flyT = requestAnimationFrame(step);
    })(t0);
  }

  // ==========================================================================
  //  Inspector
  // ==========================================================================
  function renderInspector(nd) {
    var host = document.getElementById("inspector");
    if (!nd) { renderSummary(host); return; }
    host.className = "";
    var col = KIND_COLOR[nd.kind] || "#fff";
    var cells = [];
    if (nd.loc != null) cells.push(cell("LOC", nd.loc));
    if (nd.language) cells.push(cell("Lang", nd.language));
    if (nd.line != null) cells.push(cell("Line", nd.line));
    if (nd.length != null) cells.push(cell("Length", nd.length));
    if (nd.complexity != null) cells.push(cell("Complexity", nd.complexity));
    cells.push(cell("Degree", nd.degree || (adjacency[nd.id] || []).length));
    if (nd.documented != null) cells.push(cell("Docs", nd.documented ? "yes" : "no"));
    if (nd.external) cells.push(cell("Source", "external"));

    var neigh = (adjacency[nd.id] || []).map(function (id) { return nodeById[id]; })
      .filter(Boolean)
      .sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); })
      .slice(0, 12);
    var nh = neigh.map(function (m) {
      return '<div class="nh-item" data-id="' + escAttr(m.id) + '">' +
        '<span class="nh-dot" style="background:' + (KIND_COLOR[m.kind] || "#fff") + '"></span>' +
        escapeHtml(m.label) + '<span class="nh-arrow">›</span></div>';
    }).join("");

    host.innerHTML =
      '<div class="insp-title"><span class="insp-dot" style="color:' + col + ';background:' + col + '"></span>' +
      '<div><div class="insp-name">' + escapeHtml(nd.label) + '</div>' +
      '<div class="insp-kind">' + nd.kind + '</div></div></div>' +
      '<div class="insp-grid">' + cells.join("") + '</div>' +
      (nd.path ? '<div class="insp-path">' + escapeHtml(nd.path) +
        (nd.line ? ':' + nd.line : '') + '</div>' : "") +
      (nd.path && vscodeAvailable ?
        '<button class="vsc-open" id="insp-vsc">↗ Open in VS Code <kbd>o</kbd></button>' : "") +
      (nh ? '<div class="insp-neighbours"><div class="nh-title">Connections (' +
        (adjacency[nd.id] || []).length + ')</div>' + nh + '</div>' : "");

    var vb = host.querySelector("#insp-vsc");
    if (vb) vb.addEventListener("click", function () { openInVSCode(nd); });
    Array.prototype.forEach.call(host.querySelectorAll(".nh-item"), function (el) {
      el.addEventListener("click", function () { selectNode(el.getAttribute("data-id")); });
    });
  }
  function cell(k, v) {
    return '<div class="insp-cell"><div class="k">' + k + '</div><div class="v">' + v + '</div></div>';
  }

  function renderSummary(host) {
    host.className = "";
    var sc = DATA.scores, col = scoreColor(sc.overall);
    var top = nodes.filter(function (n) { return n.kind !== "Module"; })
      .sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); }).slice(0, 6);
    var big = nodes.filter(function (n) { return n.kind === "File" && n.loc; })
      .sort(function (a, b) { return b.loc - a.loc; }).slice(0, 4);
    function row(n, right) {
      return '<div class="nh-item" data-id="' + escAttr(n.id) + '">' +
        '<span class="nh-dot" style="background:' + (KIND_COLOR[n.kind] || "#fff") + '"></span>' +
        escapeHtml(n.label) + '<span class="nh-right">' + right + '</span></div>';
    }
    host.innerHTML =
      '<div class="sum-read">This codebase scores <b style="color:' + col + '">' +
        Math.round(sc.overall) + '</b>/100 for AI-readiness (grade <b style="color:' + col +
        '">' + sc.grade + '</b>). Click any node to inspect it, or search above.</div>' +
      '<div class="insp-neighbours"><div class="nh-title">Top hubs (most connected)</div>' +
        top.map(function (n) { return row(n, (n.degree || 0) + " links"); }).join("") + '</div>' +
      '<div class="insp-neighbours"><div class="nh-title">Largest files</div>' +
        big.map(function (n) { return row(n, n.loc + " LOC"); }).join("") + '</div>' +
      '<div class="sum-tip">' + (VIEW === "atlas"
        ? "Drag to pan · scroll to zoom · click a file to open it up"
        : "Drag to orbit · scroll to zoom · right-drag to pan") + '</div>';
    Array.prototype.forEach.call(host.querySelectorAll(".nh-item"), function (el) {
      el.addEventListener("click", function () { selectNode(el.getAttribute("data-id")); });
    });
  }

  function fmt(n) { return (n || 0).toLocaleString(); }

  // ==========================================================================
  //  View modes
  // ==========================================================================
  function initialView() {
    var q = (location.search.match(/[?&]view=(\w+)/) || [])[1];
    if (q === "atlas" || q === "nebula") return q;
    var s;
    try { s = localStorage.getItem("peacock_view"); } catch (err) { s = null; }
    if (s === "atlas" || s === "nebula") return s;
    return (DATA.meta && DATA.meta.view) === "atlas" ? "atlas" : "nebula";
  }

  function setView(name, first) {
    if (name !== "atlas" && name !== "nebula") name = "nebula";
    if (!first && name === VIEW) return;
    VIEW = name;
    var atlas = name === "atlas";
    try { localStorage.setItem("peacock_view", name); } catch (err) { /* private mode */ }

    hiddenEdges = hiddenEdgesByView[name];
    expandedFile = null;
    expandSymbols(null);

    camera = atlas ? camOrtho : camPersp;
    camera.userData.fitted = false;
    makeControls();

    // the nebula's atmosphere is exactly what makes long edges unreadable —
    // the atlas drops fog and stars entirely
    scene.fog = atlas ? null : new THREE.FogExp2(0x04050a, 0.0016);
    if (starField) starField.visible = !atlas;

    buildCells();
    buildPoints();
    buildEdges();
    updateHighlight();
    onResize();
    fitCamera(false);

    buildEdgeLegend();          // per-view edge filters have their own off-states
    renderInspector(selectedId ? nodeById[selectedId] : null);
    var box = document.getElementById("view-toggle");
    if (box) {
      Array.prototype.forEach.call(box.querySelectorAll("button"), function (b) {
        b.classList.toggle("active", b.getAttribute("data-view") === name);
      });
    }
    refreshHud();
  }

  // ==========================================================================
  //  Controls wiring
  // ==========================================================================
  function wireControls() {
    var search = document.getElementById("search");
    search.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      var q = search.value.trim().toLowerCase();
      if (!q) return;
      var hit = nodes.filter(function (n) { return !hiddenKinds[n.kind]; })
        .filter(function (n) { return (n.label || "").toLowerCase().indexOf(q) >= 0 ||
          (n.path || "").toLowerCase().indexOf(q) >= 0; })
        .sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); })[0];
      if (hit) selectNode(hit.id);
      else flash("No match for “" + q + "”");
    });

    document.getElementById("btn-reset").addEventListener("click", function () {
      selectNode(null);
      fitCamera(true);
      if (VIEW !== "atlas") controls.autoRotate = true;
    });
    var vt = document.getElementById("view-toggle");
    if (vt) {
      Array.prototype.forEach.call(vt.querySelectorAll("button"), function (b) {
        b.addEventListener("click", function () { setView(b.getAttribute("data-view")); });
      });
    }
    document.getElementById("btn-labels").addEventListener("click", function () {
      pinnedLabels = !pinnedLabels;
      this.classList.toggle("active", pinnedLabels);
    });
    document.getElementById("btn-help").addEventListener("click", function () {
      document.getElementById("help").classList.remove("hidden");
    });
    document.getElementById("help-close").addEventListener("click", function () {
      document.getElementById("help").classList.add("hidden");
    });
    document.getElementById("help").addEventListener("click", function (e) {
      if (e.target.id === "help") this.classList.add("hidden");
    });
    document.addEventListener("keydown", function (e) {
      var t = e.target.tagName;
      if (t === "INPUT" || t === "TEXTAREA") return;
      if ((e.key === "o" || e.key === "O") && !e.metaKey && !e.ctrlKey) {
        var id = selectedId || hoverId;
        if (id && nodeById[id] && nodeById[id].path) { e.preventDefault(); openInVSCode(nodeById[id]); }
      }
      if (e.key === "v" || e.key === "V") setView(VIEW === "atlas" ? "nebula" : "atlas");
      if (e.key === "Escape") selectNode(null);
    });

    document.getElementById("ext-add").addEventListener("click", addLiveNode);
    document.getElementById("ext-name").addEventListener("keydown", function (e) {
      if (e.key === "Enter") addLiveNode();
    });

    refreshHud();
  }

  function refreshHud() {
    var hud = document.getElementById("hud");
    var tot = DATA.stats.total_nodes, shown = nodes.length;
    var langN = Object.keys(DATA.stats.languages).length;
    hud.textContent = (VIEW === "atlas" ? "atlas · " : "nebula · ") +
      (shown < tot ? "showing " + fmt(shown) + " of " + fmt(tot) : fmt(tot)) +
      " nodes · " + fmt(DATA.stats.total_edges) + " edges · " + langN + " languages";
  }

  function flyToVec(tgt, cam, zoom) {
    var t0 = performance.now(), dur = 700;
    var cs = camera.position.clone(), ts = controls.target.clone();
    var zs = camera.zoom, ze = zoom == null ? camera.zoom : zoom;
    var ortho = camera.isOrthographicCamera;
    if (flyT) cancelAnimationFrame(flyT);
    (function step(now) {
      var k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      camera.position.lerpVectors(cs, cam, e);
      controls.target.lerpVectors(ts, tgt, e);
      if (ortho) { camera.zoom = zs + (ze - zs) * e; camera.updateProjectionMatrix(); }
      controls.update();
      if (k < 1) flyT = requestAnimationFrame(step);
    })(t0);
  }

  // ==========================================================================
  //  Live extension
  // ==========================================================================
  function addLiveNode() {
    var name = document.getElementById("ext-name").value.trim();
    if (!name) { flash("Enter a name first."); return; }
    var kind = document.querySelector('input[name="ekind"]:checked').value;
    var attach = document.getElementById("ext-attach").value.trim();

    var host = null;
    if (attach) {
      host = nodes.filter(function (n) { return n.kind === "File"; })
        .filter(function (n) { return (n.path || n.label) === attach ||
          (n.label || "") === attach || (n.path || "").indexOf(attach) >= 0; })[0];
    }
    // sensible default: attach to the currently-selected node, else float freely
    // (no fabricated edges — an unattached import is honestly shown floating)
    if (!host && selectedId && nodeById[selectedId] &&
        nodeById[selectedId].kind !== "Function") {
      host = nodeById[selectedId];
    }
    var origin = host || null;
    var jitter = 22;
    var seed = origin ? [origin.x, origin.y, origin.z] : outerShellPoint();
    var aseed = origin ? [origin.ax || 0, origin.ay || 0] : atlasShellPoint();
    var nd = {
      id: "live:" + kind + ":" + name + ":" + Date.now(),
      kind: kind, label: name, external: kind === "Library",
      degree: origin ? 1 : 0, size: kind === "Library" ? 3.4 : 2.6,
      x: seed[0] + rand(jitter), y: seed[1] + rand(jitter), z: seed[2] + rand(jitter),
      ax: aseed[0] + rand(14), ay: aseed[1] - 16 + rand(6),
      _live: true
    };
    nodes.push(nd);
    nodeById[nd.id] = nd; adjacency[nd.id] = [];
    // a live node joins its host's cell so its new wire is an ordinary,
    // immediately-visible arc rather than a filtered-out library route
    modKeyOf[nd.id] = origin ? modKeyOf[origin.id] : moduleKey(nd);
    if (origin) {
      edges.push({ source: origin.id, target: nd.id, kind: "imports" });
      adjacency[nd.id].push(origin.id); adjacency[origin.id].push(nd.id);
      if (VIEW !== "atlas") relaxNode(nd);
    }

    // keep every counter in sync with the live addition
    DATA.stats.total_nodes += 1;
    if (origin) {
      DATA.stats.total_edges += 1;
      if (DATA.stats.edge_kinds.imports != null) DATA.stats.edge_kinds.imports++;
    }
    buildPoints(); buildEdges(); buildLegend(); buildEdgeLegend();
    buildOverview(); refreshHud();

    // keep the WHOLE graph in view and pulse the newcomer (no global dimming)
    selectedId = null;
    pulseId = nd.id;
    pulseStart = performance.now();
    rebuildVisibility();
    updateHighlight();
    renderInspector(nd);
    fitCamera(true);

    var log = document.getElementById("ext-log");
    log.innerHTML = '<span class="ok">✓ added</span> ' + escapeHtml(kind) + " “" +
      escapeHtml(name) + "”" + (origin ? " → " + escapeHtml(origin.label) : " · floating") +
      "<br>" + log.innerHTML;
    document.getElementById("ext-name").value = "";
    document.getElementById("ext-attach").value = "";
  }

  function atlasShellPoint() {
    var xs = 0, ys = 0, n = 0, maxy = -1e9;
    nodes.forEach(function (p) {
      if (p.ax == null) return;
      xs += p.ax; ys += p.ay; n++; maxy = Math.max(maxy, p.ay);
    });
    if (!n) return [0, 0];
    return [xs / n + rand(60), maxy + 34];
  }

  function outerShellPoint() {
    var box = new THREE.Box3();
    nodes.forEach(function (n) { box.expandByPoint(new THREE.Vector3(n.x, n.y, n.z)); });
    var c = box.getCenter(new THREE.Vector3());
    var r = box.getSize(new THREE.Vector3()).length() * 0.42 || 120;
    var u = Math.random() * 2 - 1, th = Math.random() * Math.PI * 2, s = Math.sqrt(1 - u * u);
    return [c.x + r * s * Math.cos(th), c.y + r * u, c.z + r * s * Math.sin(th)];
  }

  function relaxNode(nd) {
    // a few local force iterations so the new node settles near its neighbours
    for (var it = 0; it < 30; it++) {
      var fx = 0, fy = 0, fz = 0;
      adjacency[nd.id].forEach(function (id) {
        var o = nodeById[id];
        fx += (o.x - nd.x) * 0.08; fy += (o.y - nd.y) * 0.08; fz += (o.z - nd.z) * 0.08;
      });
      // light repulsion from nearest few
      for (var i = 0; i < nodes.length; i += Math.max(1, (nodes.length / 200) | 0)) {
        var o = nodes[i]; if (o === nd) continue;
        var dx = nd.x - o.x, dy = nd.y - o.y, dz = nd.z - o.z;
        var d2 = dx*dx + dy*dy + dz*dz + 1;
        if (d2 < 900) { var f = 30 / d2; fx += dx*f; fy += dy*f; fz += dz*f; }
      }
      nd.x += fx * 0.5; nd.y += fy * 0.5; nd.z += fz * 0.5;
    }
  }

  // ==========================================================================
  //  Render loop
  // ==========================================================================
  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    if (pointMat) {
      if (camera.isOrthographicCamera) {
        pointMat.uniforms.uPix.value = camera.zoom * renderer.getPixelRatio();
      } else {
        var cd = camera.position.distanceTo(controls.target);
        pointMat.uniforms.uNear.value = cd * 0.42;
        pointMat.uniforms.uFar.value = cd * 1.75;
      }
    }
    var nd = pick();
    setHover(nd ? nd.id : null);
    updateLabels();
    tickPulse();
    renderer.render(scene, camera);
  }

  function tickPulse() {
    if (!pulseId || !pointGeo) return;
    var pn = nodeById[pulseId]; if (!pn || pn._i == null) return;
    var t = (performance.now() - pulseStart) / 1600;
    var alp = pointGeo.getAttribute("aalpha");
    if (t >= 1) { alp.array[pn._i] = pn._b == null ? 1 : pn._b; alp.needsUpdate = true; pulseId = null; return; }
    // 3 quick throbs then settle
    var p = 1 + 0.9 * Math.abs(Math.sin(t * Math.PI * 3)) * (1 - t);
    alp.array[pn._i] = (pn._b == null ? 1 : pn._b) * p * 1.4;
    alp.needsUpdate = true;
  }

  function updateLabels() {
    // labels for: hovered, selected, and (if pinned) top-degree nodes
    var atlas = VIEW === "atlas";
    var want = [];              // [id, priority] — higher priority wins collisions
    if (hoverId) want.push([hoverId, 100]);
    if (selectedId) want.push([selectedId, 100]);
    if (pinnedLabels) {
      nodes.slice().sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); })
        .slice(0, 28).forEach(function (n) { if (!isHidden(n)) want.push([n.id, 5]); });
    }
    if (atlas && camera.zoom * 26 > LOD_FILE_PX) {
      // zoomed in far enough that file names fit — name them, busiest first
      nodes.forEach(function (n) {
        if (n.kind === "File" && !isHidden(n)) want.push([n.id, 10 + (n.degree || 0) * 0.01]);
        else if (isSymbol(n) && !isHidden(n)) want.push([n.id, 8]);
      });
    }

    var keep = {}, taken = [];
    var w = renderer.domElement.clientWidth, h = renderer.domElement.clientHeight;
    want.sort(function (a, b) { return b[1] - a[1]; });
    want.forEach(function (it) {
      var id = it[0];
      if (keep[id]) return;
      var nd = nodeById[id]; if (!nd || isHidden(nd)) return;
      // module names are drawn by the cell chrome in the atlas — no duplicates
      if (atlas && nd.kind === "Module" && it[1] < 100) return;
      var v = nvec(nd).project(camera);
      if (v.z > 1) return;
      var sx = (v.x + 1) / 2 * w, sy = (-v.y + 1) / 2 * h;
      if (it[1] < 100 && !place(taken, sx, sy, nd.label.length * 6.5 + 14)) return;
      var el = ensureLabel(labelEls, id, "pin-label");
      el.textContent = nd.label;
      el.style.display = "block";
      el.style.left = sx + "px";
      el.style.top = sy + "px";
      el.style.borderColor = id === selectedId ? (KIND_COLOR[nd.kind] || "#fff")
                                               : "rgba(140,160,220,.28)";
      keep[id] = 1;
    });
    Object.keys(labelEls).forEach(function (id) {
      if (!keep[id]) { labelLayer.removeChild(labelEls[id]); delete labelEls[id]; }
    });
    updateCellLabels(taken, w, h);
  }

  /** Directory names sit on their cell's header band. These carry the atlas:
   *  a rectangle with a name on it is what makes it read as a map. */
  function updateCellLabels(taken, w, h) {
    var keep = {};
    if (VIEW === "atlas") {
      var v = new THREE.Vector3();
      atlasCells.forEach(function (cl) {
        // clear the module node's dot, which sits in the same header band
        var pad = cl.depth ? 18 : 12;
        var onScreen = cl.w * camera.zoom;
        if (cl.depth === 1 && onScreen < LOD_CELL_PX) return;
        v.set(cl.x + pad, cl.y + cl.h - (cl.depth ? 10 : 13), 0).project(camera);
        if (v.z > 1) return;
        var sx = (v.x + 1) / 2 * w, sy = (-v.y + 1) / 2 * h;
        if (sx < -200 || sx > w + 200 || sy < -80 || sy > h + 80) return;
        // a district and a module can share a key ("samples" / "samples/")
        var ck = cl.depth + ":" + cl.key;
        var el = ensureLabel(cellEls, ck, "cell-label depth" + cl.depth);
        el.textContent = cl.label;
        el.style.left = sx + "px";
        el.style.top = sy + "px";
        place(taken, sx, sy, cl.label.length * 6.5 + 14);
        keep[ck] = 1;
      });
    }
    Object.keys(cellEls).forEach(function (k) {
      if (!keep[k]) { labelLayer.removeChild(cellEls[k]); delete cellEls[k]; }
    });
  }

  /** Greedy screen-space collision cull: claim a box, or report a clash. */
  function place(taken, x, y, wpx) {
    for (var i = 0; i < taken.length; i++) {
      var t = taken[i];
      if (Math.abs(t[0] - x) < (t[2] + wpx) * 0.5 && Math.abs(t[1] - y) < 17) return false;
    }
    taken.push([x, y, wpx]);
    return true;
  }

  function ensureLabel(store, key, cls) {
    var el = store[key];
    if (!el) {
      el = document.createElement("div");
      el.className = cls;
      labelLayer.appendChild(el);
      store[key] = el;
    }
    el.style.display = "block";
    return el;
  }
  var labelEls = {}, cellEls = {};

  // ==========================================================================
  //  VS Code integration
  // ==========================================================================
  function checkVSCode() {
    fetch("api/vscode-status").then(function (r) { return r.json(); }).then(function (s) {
      vscodeAvailable = !!(s && s.available);
      if (!vscodeAvailable) return;
      if (selectedId) renderInspector(nodeById[selectedId]);   // refresh button
      if (sessionStorage.getItem("peacock_vscode") === "1") { vscodeEnabled = true; return; }
      if (sessionStorage.getItem("peacock_vscode") === "0") return;
      showVSCodeModal(s.root || DATA.meta.name);
    }).catch(function () { vscodeAvailable = false; });
  }

  function showVSCodeModal(rootPath) {
    var m = document.createElement("div");
    m.className = "overlay";
    m.innerHTML =
      '<div class="overlay-card vscode-card">' +
      '<h2>🦚 → <span style="color:var(--accent-2)">VS&nbsp;Code</span></h2>' +
      '<p>Open this project in VS Code for this session? Then clicking a node and ' +
      'pressing <kbd>o</kbd> (or the Inspector button) jumps straight to that ' +
      'function/file in the already-open editor.</p>' +
      '<p class="muted vscode-path" title="' + escAttr(rootPath) + '">' + escapeHtml(rootPath) + '</p>' +
      '<div class="vscode-actions">' +
      '<button class="ghost" id="vsc-no">Not now</button>' +
      '<button class="primary" id="vsc-yes">Open in VS Code</button></div></div>';
    document.body.appendChild(m);
    m.querySelector("#vsc-yes").addEventListener("click", function () {
      fetch("api/open-project").then(function (r) { return r.json(); }).then(function (res) {
        if (res && res.ok) {
          vscodeEnabled = true; sessionStorage.setItem("peacock_vscode", "1");
        } else { flashToast("Could not launch VS Code: " + ((res && res.error) || "unknown")); }
      });
      document.body.removeChild(m);
    });
    m.querySelector("#vsc-no").addEventListener("click", function () {
      sessionStorage.setItem("peacock_vscode", "0");
      document.body.removeChild(m);
    });
  }

  function openInVSCode(nd) {
    if (!nd || !nd.path) { flashToast("This node has no file to open."); return; }
    if (!vscodeAvailable) { flashToast("VS Code CLI not found — launch Peacock via ./run.sh."); return; }
    var q = "api/open?path=" + encodeURIComponent(nd.path) + "&line=" + (nd.line || 1);
    fetch(q).then(function (r) { return r.json(); }).then(function (res) {
      if (res && res.ok) flashToast("↗ Opened " + nd.label + " in VS Code");
      else flashToast("Open failed: " + ((res && res.error) || "unknown"));
    }).catch(function () { flashToast("VS Code bridge unavailable."); });
  }

  var toastT = null;
  function flashToast(msg) {
    var t = document.getElementById("toast");
    if (!t) {
      t = document.createElement("div"); t.id = "toast"; t.className = "toast";
      document.getElementById("stage").appendChild(t);
    }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(toastT);
    toastT = setTimeout(function () { t.classList.remove("show"); }, 2600);
  }

  // ---- demo hooks (used for automated screenshots: ?demo=select|extend|filter)
  function applyDemoParam() {
    var q = (location.search.match(/demo=(\w+)/) || [])[1];
    if (!q) return;
    controls.autoRotate = false;
    if (q === "atlas" || q === "nebula") { setView(q); return; }
    if (q === "select") {
      var top = nodes.filter(function (n) { return n.kind === "File"; })
        .sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); })[0];
      if (top) selectNode(top.id);
    } else if (q === "extend") {
      var f = nodes.filter(function (n) { return n.kind === "File"; })
        .sort(function (a, b) { return (b.degree || 0) - (a.degree || 0); })[0];
      var fname = f ? (f.path || f.label) : "";
      document.getElementById("ext-attach").value = fname;
      document.getElementById("ext-name").value = "numpy";
      addLiveNode();
      document.getElementById("ext-attach").value = fname;
      document.getElementById("ext-name").value = "pandas";
      addLiveNode();
    } else if (q === "filter") {
      hiddenKinds.Function = true;
      var rows = document.querySelectorAll("#legend .legend-row");
      if (rows[3]) rows[3].classList.add("off");
      rebuildVisibility();
    } else if (q === "help") {
      document.getElementById("help").classList.remove("hidden");
    } else if (q === "vscode") {
      vscodeAvailable = true;
      showVSCodeModal(DATA.meta.root || DATA.meta.name);
    }
  }

  // ---- helpers -------------------------------------------------------------
  function rand(a) { return (Math.random() - 0.5) * 2 * a; }
  function flash(msg) {
    var log = document.getElementById("ext-log");
    log.innerHTML = msg + "<br>" + log.innerHTML;
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function escAttr(s) { return escapeHtml(s).replace(/"/g, "&quot;"); }
})();
