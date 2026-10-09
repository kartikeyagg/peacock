"""
3D layout — fast, structural and pretty.

Rather than an O(n^2) global force simulation (too slow in pure Python for a
few thousand nodes), Peacock lays the graph out *hierarchically*, which is both
fast (linear in nodes) and more meaningful — you see modules as galaxies, files
as clusters within them, and symbols orbiting their file:

  1. lay out MODULE (directory) centres with a small force sim on the module
     graph  — few nodes, so this is cheap;
  2. arrange each module's FILES on a phyllotaxis sphere around its centre;
  3. arrange each file's SYMBOLS on a small phyllotaxis sphere around the file;
  4. park external LIBRARIES just outside the modules that import them.

The result is a clustered nebula that settles instantly and reads clearly.
"""
from __future__ import annotations
import math
import posixpath
import random

_GOLDEN = math.pi * (3 - math.sqrt(5))


def _module_key(node):
    if node["kind"] == "Module":
        return node.get("path", ".") or "."
    p = node.get("path")
    if p is None:
        return None
    d = posixpath.dirname(p)
    return d if d else "."


def _scatter(center, n, radius, seed=0):
    """Organic filled cloud around center: a gaussian core (fills the middle)
    blended with a phyllotactic shell (keeps clusters visibly round). Gives the
    nebula look rather than a hollow sphere or a rigid 'dandelion'."""
    out = []
    if n <= 0:
        return out
    if n == 1:
        return [[center[0], center[1], center[2]]]
    rng = random.Random(seed)
    jit = radius * 0.22
    for i in range(n):
        # phyllotaxis direction for even angular coverage
        y = 1 - (i / (n - 1)) * 2
        rr = math.sqrt(max(0.0, 1 - y * y))
        theta = _GOLDEN * i
        dirx, diry, dirz = math.cos(theta) * rr, y, math.sin(theta) * rr
        # radius fills the whole ball (incl. centre): r ~ R * u^0.7 -> mild edge bias
        rad = radius * (rng.random() ** 0.7)
        out.append([
            center[0] + dirx * rad + rng.gauss(0, jit),
            center[1] + diry * rad + rng.gauss(0, jit),
            center[2] + dirz * rad + rng.gauss(0, jit),
        ])
    return out


def _module_force(mod_ids, mod_edges, iterations=80, seed=7):
    """Small Fruchterman-Reingold on the (few) module centres."""
    rng = random.Random(seed)
    n = len(mod_ids)
    idx = {m: i for i, m in enumerate(mod_ids)}
    pos = []
    for i in range(n):
        u = rng.uniform(-1, 1); th = rng.uniform(0, 2 * math.pi)
        r = (rng.uniform(0, 1) ** (1 / 3)) * 80
        s = math.sqrt(1 - u * u)
        pos.append([r * s * math.cos(th), r * s * math.sin(th), r * u])
    if n == 1:
        return {mod_ids[0]: [0.0, 0.0, 0.0]}

    adj = [dict() for _ in range(n)]
    for (a, b), w in mod_edges.items():
        if a in idx and b in idx and a != b:
            adj[idx[a]][idx[b]] = adj[idx[a]].get(idx[b], 0) + w
            adj[idx[b]][idx[a]] = adj[idx[b]].get(idx[a], 0) + w

    k = max(22.0, 190.0 / (n ** 0.5))
    temp = k * 1.5
    cool = temp / (iterations + 1)
    for _ in range(iterations):
        disp = [[0.0, 0.0, 0.0] for _ in range(n)]
        for i in range(n):
            pi = pos[i]
            for j in range(i + 1, n):
                dx = pi[0] - pos[j][0]; dy = pi[1] - pos[j][1]; dz = pi[2] - pos[j][2]
                d2 = dx * dx + dy * dy + dz * dz or 0.01
                f = k * k / d2
                d = math.sqrt(d2)
                ux, uy, uz = dx / d, dy / d, dz / d
                disp[i][0] += ux * f; disp[i][1] += uy * f; disp[i][2] += uz * f
                disp[j][0] -= ux * f; disp[j][1] -= uy * f; disp[j][2] -= uz * f
        for i in range(n):
            for j, w in adj[i].items():
                if j <= i:
                    continue
                dx = pos[i][0] - pos[j][0]; dy = pos[i][1] - pos[j][1]; dz = pos[i][2] - pos[j][2]
                d = math.sqrt(dx * dx + dy * dy + dz * dz) or 0.01
                f = d * d / k * min(3.0, 0.5 + 0.2 * w)
                ux, uy, uz = dx / d * f, dy / d * f, dz / d * f
                disp[i][0] -= ux; disp[i][1] -= uy; disp[i][2] -= uz
                disp[j][0] += ux; disp[j][1] += uy; disp[j][2] += uz
        for i in range(n):
            disp[i][0] -= pos[i][0] * 0.022; disp[i][1] -= pos[i][1] * 0.022; disp[i][2] -= pos[i][2] * 0.022
            dd = math.sqrt(disp[i][0] ** 2 + disp[i][1] ** 2 + disp[i][2] ** 2) or 0.01
            lim = min(dd, temp)
            pos[i][0] += disp[i][0] / dd * lim
            pos[i][1] += disp[i][1] / dd * lim
            pos[i][2] += disp[i][2] / dd * lim
        temp -= cool
    return {mod_ids[i]: pos[i] for i in range(n)}


def layout_3d(nodes, edges, iterations=80, seed=7):
    if not nodes:
        return {}

    # --- group nodes ---
    modules, files, symbols, libraries = {}, {}, {}, []
    file_of_symbol = {}
    for nid, n in nodes.items():
        k = n["kind"]
        if k == "Module":
            modules.setdefault(n.get("path", ".") or ".", nid)
        elif k == "File":
            files.setdefault(nid, n)
        elif k == "Library":
            libraries.append(nid)
        else:  # Function / Class
            symbols.setdefault(nid, n)

    # module set: union of declared modules and modules referenced by files
    mod_ids = set(modules.keys())
    for nid, n in files.items():
        mod_ids.add(_module_key(n) or ".")
    for nid, n in symbols.items():
        mod_ids.add(_module_key(n) or ".")
    mod_ids = sorted(mod_ids)

    # aggregate module-module edges
    def mod_of(nid):
        n = nodes.get(nid)
        return _module_key(n) if n else None
    mod_edges = {}
    for e in edges:
        ma, mb = mod_of(e["source"]), mod_of(e["target"])
        if ma is None or mb is None or ma == mb:
            continue
        key = (ma, mb) if ma < mb else (mb, ma)
        mod_edges[key] = mod_edges.get(key, 0) + 1

    centers = _module_force(mod_ids, mod_edges, iterations=iterations, seed=seed)
    # scale module centres to a comfortable radius
    if centers:
        maxr = max(math.sqrt(p[0]**2 + p[1]**2 + p[2]**2) for p in centers.values()) or 1.0
        s = 172.0 / maxr
        for m in centers:
            centers[m] = [centers[m][0] * s, centers[m][1] * s, centers[m][2] * s]

    out = {}

    # --- module nodes sit at their centre ---
    for mkey, nid in modules.items():
        c = centers.get(mkey, [0, 0, 0])
        out[nid] = [round(c[0], 2), round(c[1], 2), round(c[2], 2)]

    # --- files: phyllotaxis around their module centre ---
    files_by_mod = {}
    for nid, n in files.items():
        files_by_mod.setdefault(_module_key(n) or ".", []).append(nid)
    file_center = {}
    for mkey, ids in files_by_mod.items():
        c = centers.get(mkey, [0, 0, 0])
        radius = 12 + 4.6 * math.sqrt(len(ids))
        pts = _scatter(c, len(ids), radius, seed=hash(mkey) & 0xffff)
        for nid, p in zip(sorted(ids), pts):
            out[nid] = [round(p[0], 2), round(p[1], 2), round(p[2], 2)]
            file_center[nid] = p

    # --- symbols: small phyllotaxis around their file ---
    syms_by_file = {}
    for nid, n in symbols.items():
        fid = "file:" + (n.get("path") or "")
        syms_by_file.setdefault(fid, []).append(nid)
    for fid, ids in syms_by_file.items():
        c = file_center.get(fid)
        if c is None:
            mkey = _module_key(nodes.get(ids[0], {})) or "."
            c = centers.get(mkey, [0, 0, 0])
        radius = 5 + 2.6 * math.sqrt(len(ids))
        pts = _scatter(c, len(ids), radius, seed=hash(fid) & 0xffff)
        for nid, p in zip(sorted(ids), pts):
            out[nid] = [round(p[0], 2), round(p[1], 2), round(p[2], 2)]

    # --- libraries: just outside the mean of their importers ---
    importers = {}
    for e in edges:
        if e["kind"] == "imports" and nodes.get(e["target"], {}).get("kind") == "Library":
            importers.setdefault(e["target"], []).append(e["source"])
    rng = random.Random(seed)
    for lib in libraries:
        srcs = [out[s] for s in importers.get(lib, []) if s in out]
        if srcs:
            cx = sum(p[0] for p in srcs) / len(srcs)
            cy = sum(p[1] for p in srcs) / len(srcs)
            cz = sum(p[2] for p in srcs) / len(srcs)
            v = math.sqrt(cx*cx + cy*cy + cz*cz) or 1.0
            f = 1.25
            out[lib] = [round(cx * f + rng.uniform(-8, 8), 2),
                        round(cy * f + rng.uniform(-8, 8), 2),
                        round(cz * f + rng.uniform(-8, 8), 2)]
        else:
            u = rng.uniform(-1, 1); th = rng.uniform(0, 2 * math.pi)
            r = 190; s2 = math.sqrt(1 - u * u)
            out[lib] = [round(r * s2 * math.cos(th), 2), round(r * u, 2),
                        round(r * s2 * math.sin(th), 2)]

    # any stragglers
    for nid in nodes:
        if nid not in out:
            out[nid] = [rng.uniform(-120, 120), rng.uniform(-120, 120), rng.uniform(-120, 120)]
    return out


# --------------------------------------------------------------------------- #
#  Atlas layout — a flat, packed 2D map                                       #
# --------------------------------------------------------------------------- #
# The 3D nebula above is pretty but ambiguous: with a perspective camera and no
# occlusion you cannot tell where a long edge ends, so edges read as rays going
# off to infinity. The atlas layout trades beauty for legibility — everything
# lives on a plane, every directory is a rectangle, and containment needs no
# edges at all because the rectangle *is* the containment.

_A_PITCH   = 26.0    # spacing between file dots inside a cell
_A_PAD     = 13.0    # cell inner padding
_A_HEADER  = 20.0    # band at the top of a cell for its module label
_A_GAP     = 20.0    # gap between sibling cells
_A_DPAD    = 15.0    # district inner padding
_A_DHEADER = 26.0    # band at the top of a district for its label
_A_DGAP    = 44.0    # gap between districts
_A_GUTTER  = 110.0   # gap between the field and the library gutter


def _district_key(mkey):
    """Top-level directory a module belongs to ('.' for repo root)."""
    if not mkey or mkey == ".":
        return "."
    return mkey.split("/", 1)[0]


def _shelf_pack(items, target_w, gap):
    """Greedy shelf packing in a y-DOWN space.

    `items` is an ordered list of (key, w, h). Returns ({key: (x, y)}, W, H).
    Order is preserved, so callers control adjacency by sorting beforehand —
    that keeps the map stable between runs (no random force sim).
    """
    placed, x, y, shelf_h, W = {}, 0.0, 0.0, 0.0, 0.0
    for key, w, h in items:
        if x > 0.0 and x + w > target_w:
            x = 0.0
            y += shelf_h + gap
            shelf_h = 0.0
        placed[key] = (x, y)
        x += w + gap
        W = max(W, x - gap)
        shelf_h = max(shelf_h, h)
    return placed, W, (y + shelf_h)


def _target_width(areas, ratio=1.9):
    """Shelf width that lands near a 16:9-ish block, with slack for waste."""
    return math.sqrt((sum(areas) or 1.0) * ratio) * 1.12


def _grid_shape(n):
    """Columns/rows for n items in a slightly-wider-than-tall grid."""
    if n <= 0:
        return 1, 1
    cols = max(1, int(math.ceil(math.sqrt(n * 1.6))))
    rows = int(math.ceil(n / cols))
    return cols, rows


def layout_atlas(nodes, edges=None):
    """Flat 2D 'atlas' layout.

    Every directory becomes a packed rectangular cell whose files are a grid of
    dots inside it; top-level directories group into districts; external
    libraries are parked in a right-hand gutter so their (often enormous)
    fan-in never crosses the map.

    Returns ``(positions, cells)``:
      * ``positions``  — node id -> ``[x, y]``, y-up, centred on the origin.
      * ``cells``      — ``[{key,label,x,y,w,h,depth,count}]`` rectangles for the
        renderer, where ``(x, y)`` is the BOTTOM-LEFT corner (y-up) and
        ``depth`` is 0 for a district frame and 1 for a module cell.

    Symbols (Class/Function) are parked exactly on their file: the renderer
    hides them until that file is expanded, and computes their fan-out locally.
    """
    if not nodes:
        return {}, []

    # ---- group ------------------------------------------------------------
    module_node = {}          # module key -> module node id
    files_by_mod = {}         # module key -> [file id]
    symbols, libraries = [], []
    for nid, n in nodes.items():
        k = n["kind"]
        if k == "Module":
            module_node.setdefault(n.get("path", ".") or ".", nid)
        elif k == "File":
            files_by_mod.setdefault(_module_key(n) or ".", []).append(nid)
        elif k == "Library":
            libraries.append(nid)
        else:
            symbols.append(nid)

    mod_keys = set(module_node) | set(files_by_mod)
    for nid in symbols:
        mod_keys.add(_module_key(nodes[nid]) or ".")
    mod_keys = sorted(mod_keys)
    if not mod_keys:
        mod_keys = ["."]

    # ---- size every module cell from its file count -----------------------
    cell_size, cell_grid = {}, {}
    for mkey in mod_keys:
        fids = sorted(files_by_mod.get(mkey, []),
                      key=lambda i: ((nodes[i].get("file_kind") or "code"),
                                     nodes[i].get("label") or i))
        files_by_mod[mkey] = fids
        cols, rows = _grid_shape(len(fids))
        cell_grid[mkey] = (cols, rows)
        cell_size[mkey] = (cols * _A_PITCH + 2 * _A_PAD,
                           rows * _A_PITCH + 2 * _A_PAD + _A_HEADER)

    # ---- pack modules inside districts, then districts into the field -----
    districts = {}
    for mkey in mod_keys:
        districts.setdefault(_district_key(mkey), []).append(mkey)

    mod_origin = {}       # module key -> (x, y) of its cell, district-local
    dist_size = {}
    for dkey, mkeys in districts.items():
        sizes = [cell_size[m] for m in mkeys]
        tw = _target_width([w * h for w, h in sizes])
        tw = max(tw, max(w for w, _ in sizes))
        placed, W, H = _shelf_pack(
            [(m, cell_size[m][0], cell_size[m][1]) for m in mkeys], tw, _A_GAP)
        framed = len(mkeys) > 1
        ox = _A_DPAD if framed else 0.0
        oy = (_A_DHEADER + _A_DPAD) if framed else 0.0
        for m, (x, y) in placed.items():
            mod_origin[m] = (x + ox, y + oy)
        dist_size[dkey] = (W + 2 * ox, H + oy + (_A_DPAD if framed else 0.0))

    dkeys = sorted(districts)
    dsizes = [dist_size[d] for d in dkeys]
    dtw = _target_width([w * h for w, h in dsizes])
    dtw = max(dtw, max(w for w, _ in dsizes))
    dist_origin, field_w, field_h = _shelf_pack(
        [(d, dist_size[d][0], dist_size[d][1]) for d in dkeys], dtw, _A_DGAP)

    # absolute (still y-down) cell rectangles
    rects = {}
    for mkey in mod_keys:
        dx, dy = dist_origin[_district_key(mkey)]
        mx, my = mod_origin[mkey]
        w, h = cell_size[mkey]
        rects[mkey] = (dx + mx, dy + my, w, h)

    # ---- place the nodes (y-down for now) ---------------------------------
    pos = {}
    for mkey in mod_keys:
        cx, cy, w, h = rects[mkey]
        nid = module_node.get(mkey)
        if nid is not None:
            pos[nid] = (cx + _A_PAD * 0.7, cy + _A_HEADER * 0.5)
        cols = cell_grid[mkey][0]
        for i, fid in enumerate(files_by_mod.get(mkey, [])):
            col, row = i % cols, i // cols
            pos[fid] = (cx + _A_PAD + (col + 0.5) * _A_PITCH,
                        cy + _A_HEADER + _A_PAD + (row + 0.5) * _A_PITCH)

    # symbols sit on their file (hidden until the renderer expands that file)
    for nid in symbols:
        n = nodes[nid]
        p = pos.get("file:" + (n.get("path") or ""))
        if p is None:
            mkey = _module_key(n) or "."
            r = rects.get(mkey)
            p = (r[0] + r[2] * 0.5, r[1] + r[3] * 0.5) if r else (0.0, 0.0)
        pos[nid] = p

    # ---- libraries: a gutter column to the right of the whole field -------
    # Kept off the map on purpose: a single popular dependency can have a
    # four-figure fan-in, and letting those wires cross the field is exactly
    # what makes the 3D view unreadable.
    libs = sorted(libraries, key=lambda i: -(nodes[i].get("degree") or 0))
    gutter = None
    if libs:
        rows = max(1, int(field_h // _A_PITCH) or 1)
        cols = int(math.ceil(len(libs) / rows))
        gx = field_w + _A_GUTTER
        for i, lid in enumerate(libs):
            pos[lid] = (gx + (i // rows) * _A_PITCH * 1.6,
                        (i % rows + 0.5) * _A_PITCH)
        used_rows = min(rows, len(libs))
        gutter = {"key": "__lib", "label": "external",
                  "x": gx - _A_DPAD, "y": -(_A_DHEADER + _A_DPAD),
                  "w": (cols - 1) * _A_PITCH * 1.6 + 2 * _A_DPAD,
                  "h": used_rows * _A_PITCH + _A_DHEADER + 2 * _A_DPAD,
                  "depth": 0, "count": len(libs)}

    # ---- build the cell rectangles the renderer draws ---------------------
    cells = []
    for dkey in dkeys:
        if len(districts[dkey]) < 2:
            continue           # a lone module needs no second frame around it
        dx, dy = dist_origin[dkey]
        w, h = dist_size[dkey]
        cells.append({"key": dkey, "label": dkey if dkey != "." else "/",
                      "x": dx, "y": dy, "w": w, "h": h, "depth": 0,
                      "count": len(districts[dkey])})
    for mkey in mod_keys:
        x, y, w, h = rects[mkey]
        label = mkey.rsplit("/", 1)[-1] if mkey != "." else "/"
        cells.append({"key": mkey, "label": label, "x": x, "y": y,
                      "w": w, "h": h, "depth": 1,
                      "count": len(files_by_mod.get(mkey, []))})
    if gutter:
        cells.append(gutter)

    # ---- flip to y-up and centre on the origin ----------------------------
    xs = [p[0] for p in pos.values()] or [0.0]
    ys = [p[1] for p in pos.values()] or [0.0]
    for c in cells:
        xs += [c["x"], c["x"] + c["w"]]
        ys += [c["y"], c["y"] + c["h"]]
    ox = (min(xs) + max(xs)) * 0.5
    oy = (min(ys) + max(ys)) * 0.5

    out = {nid: [round(p[0] - ox, 2), round(oy - p[1], 2)] for nid, p in pos.items()}
    for c in cells:
        # (x, y) becomes the BOTTOM-LEFT corner in y-up space
        c["x"] = round(c["x"] - ox, 2)
        c["y"] = round(oy - (c["y"] + c["h"]), 2)
        c["w"] = round(c["w"], 2)
        c["h"] = round(c["h"], 2)

    for nid in nodes:
        out.setdefault(nid, [0.0, 0.0])
    return out, cells
