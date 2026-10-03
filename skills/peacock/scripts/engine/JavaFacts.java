/*
 * Java front-end for Peacock: declarations, imports and *resolved* call sites
 * from javac itself (com.sun.source Tree API) instead of regexes.
 *
 * Two jobs, one pass per batch of files:
 *
 *   parse    Declarations, imports and every call site, with its line. A
 *            wrapped signature, an annotation, a text block or a comment
 *            marker in a string can no longer hide or invent a declaration.
 *
 *   resolve  javac's attribution (type checking, without generating code)
 *            works out which method each call site binds to. `x.handle()`
 *            stops being a name to guess from and becomes a specific
 *            declaration in a specific file, or a library method, which is
 *            reported as external so nothing guesses an in-repo target for it.
 *            Types javac cannot see (a dependency jar that is not on hand) come
 *            back unresolved, and only those fall back to name matching.
 *
 * Run with the JDK's single-file source launcher, no build step:
 *
 *     java --add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED \
 *          JavaFacts.java < list
 *
 * (The export gives access to per-class attribution; see run().)
 *
 * Input lines: an optional "#ROOT<TAB>abs", an optional "#SOURCEPATH<TAB>a:b",
 * then "rel<TAB>abs" per file. The source path lets a batch resolve types
 * declared in files outside the batch.
 *
 * Output: one JSON object per input file:
 *
 *     {"path": rel, "error": bool, "imports": [...],
 *      "symbols": [{"name", "kind", "line", "end", "parent",
 *                   "calls": [[name, recv, line, target], ...]}]}
 *
 * `recv` is null for a bare call `f()`, the name just before the dot for
 * `x.f()` or `a.x.f()`, and "" when that is not a name (`a.b().f()`). Same
 * contract as index._receiver. `target` is [rel, line] of the declaration a
 * call binds to, "ext" for a declaration outside the repository, or null when
 * javac could not resolve it.
 */
import com.sun.source.tree.*;
import com.sun.source.util.*;

import javax.lang.model.element.*;
import javax.lang.model.type.TypeKind;
import javax.tools.*;
import java.io.*;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.regex.*;

public class JavaFacts {

    static final int BATCH = Integer.getInteger("peacock.batch", 400);
    static String root = null;
    static String sourcepath = null;

    public static void main(String[] args) throws Exception {
        JavaCompiler javac = ToolProvider.getSystemJavaCompiler();
        if (javac == null) {
            System.err.println("no system Java compiler (a JRE, not a JDK?)");
            System.exit(2);
        }
        BufferedReader in = new BufferedReader(
                new InputStreamReader(System.in, StandardCharsets.UTF_8));
        PrintStream out = new PrintStream(
                new BufferedOutputStream(System.out), false, StandardCharsets.UTF_8);

        List<String[]> batch = new ArrayList<>();
        for (String line; (line = in.readLine()) != null; ) {
            int tab = line.indexOf('\t');
            if (tab <= 0) continue;
            String a = line.substring(0, tab), b = line.substring(tab + 1);
            if (a.equals("#ROOT")) { root = Paths.get(b).toAbsolutePath().normalize().toString(); continue; }
            if (a.equals("#SOURCEPATH")) { sourcepath = b; continue; }
            batch.add(new String[]{a, b});
            if (batch.size() >= BATCH) { run(javac, batch, out); batch.clear(); }
        }
        if (!batch.isEmpty()) run(javac, batch, out);
        out.flush();
    }

    static void run(JavaCompiler javac, List<String[]> batch, PrintStream out) throws IOException {
        // Only *syntax* errors send a file to the regex fallback. Attribution
        // errors are expected — a dependency jar is rarely on hand — and are
        // the reason a call can come back unresolved, nothing more.
        Set<String> broken = new HashSet<>();
        boolean[] parsing = {true};
        DiagnosticListener<JavaFileObject> diags = d -> {
            if (parsing[0] && d.getKind() == Diagnostic.Kind.ERROR && d.getSource() != null) {
                broken.add(key(d.getSource().toUri()));
                if (System.getenv("PEACOCK_DEBUG") != null) System.err.println(d);
            }
        };
        StandardJavaFileManager fm = javac.getStandardFileManager(diags, null, StandardCharsets.UTF_8);
        Map<String, String> relOf = new HashMap<>();
        List<File> files = new ArrayList<>();
        for (String[] rp : batch) {
            File f = new File(rp[1]);
            files.add(f);
            relOf.put(key(f.toURI()), rp[0]);
        }
        List<String> opts = new ArrayList<>(List.of("-proc:none", "-implicit:none",
                "-XDshould-stop.ifError=FLOW", "-XDshould-stop.ifNoError=FLOW", "-Xlint:none", "-nowarn"));
        if (sourcepath != null && !sourcepath.isEmpty()) { opts.add("-sourcepath"); opts.add(sourcepath); }
        JavacTask task = (JavacTask) javac.getTask(null, fm, diags, opts, null,
                fm.getJavaFileObjectsFromFiles(files));
        Iterable<? extends CompilationUnitTree> units;
        try {
            units = task.parse();
        } catch (Throwable t) {
            if (System.getenv("PEACOCK_DEBUG") != null) t.printStackTrace();
            for (String[] rp : batch) out.println("{\"path\":" + q(rp[0]) + ",\"error\":true}");
            return;
        }
        parsing[0] = false;
        // Attribute one top-level class at a time. javac's error recovery can
        // crash outright on broken code (an AssertionError in Attr on a
        // `switch` expression whose types are missing, seen 15 times in
        // spring-boot); attributing the whole batch at once let one such file
        // take resolution away from the other 399.
        Trees trees = Trees.instance(task);
        Set<String> unattributed = new HashSet<>();
        Iterable<? extends Element> entered;
        try {
            entered = ((com.sun.tools.javac.api.JavacTaskImpl) task).enter();
        } catch (Throwable t) {
            if (System.getenv("PEACOCK_DEBUG") != null) t.printStackTrace();
            entered = List.of();
            for (CompilationUnitTree cu : units) unattributed.add(key(cu.getSourceFile().toUri()));
        }
        for (Element el : entered) {
            String uri = null;
            try {
                TreePath tp = trees.getPath(el);
                if (tp != null) uri = key(tp.getCompilationUnit().getSourceFile().toUri());
            } catch (Throwable ignored) { }
            if (uri != null && unattributed.contains(uri)) continue;
            try {
                ((com.sun.tools.javac.api.JavacTaskImpl) task).analyze(List.of(el));
            } catch (Throwable t) {
                // Resolution is an upgrade, not a requirement: this file's
                // calls keep a null target and fall back to name matching.
                if (uri != null) unattributed.add(uri);
                if (System.getenv("PEACOCK_DEBUG") != null)
                    System.err.println("attribution failed: " + uri + ": " + t);
            }
        }
        Set<String> done = new HashSet<>();
        for (CompilationUnitTree cu : units) {
            String uri = key(cu.getSourceFile().toUri());
            String rel = relOf.get(uri);
            if (rel == null) continue;
            done.add(rel);
            if (broken.contains(uri)) {
                out.println("{\"path\":" + q(rel) + ",\"error\":true}");
                continue;
            }
            try {
                out.println(new Facts(rel, cu, trees, !unattributed.contains(uri),
                        task.getElements()).json());
            } catch (Throwable t) {
                if (System.getenv("PEACOCK_DEBUG") != null) t.printStackTrace();
                out.println("{\"path\":" + q(rel) + ",\"error\":true}");
            }
        }
        for (String[] rp : batch)
            if (!done.contains(rp[0])) out.println("{\"path\":" + q(rp[0]) + ",\"error\":true}");
        fm.close();
    }

    /* Everything emitted for one compilation unit. */
    static final class Facts {
        final String rel;
        final CompilationUnitTree cu;
        final Trees trees;
        final boolean attributed;
        final SourcePositions sp;
        final String src;
        final LineMap lm;
        final javax.lang.model.util.Elements elements;

        Facts(String rel, CompilationUnitTree cu, Trees trees, boolean attributed,
              javax.lang.model.util.Elements elements) throws IOException {
            this.rel = rel; this.cu = cu; this.trees = trees; this.attributed = attributed;
            this.elements = elements;
            this.sp = trees.getSourcePositions();
            this.src = cu.getSourceFile().getCharContent(true).toString();
            this.lm = cu.getLineMap();
        }

        String json() {
            StringBuilder sb = new StringBuilder();
            sb.append("{\"path\":").append(q(rel)).append(",\"error\":false,\"imports\":[");
            boolean first = true;
            for (ImportTree it : cu.getImports()) {
                if (!first) sb.append(',');
                first = false;
                sb.append(q(it.getQualifiedIdentifier().toString()));
            }
            sb.append("],\"symbols\":[");
            List<String> syms = new ArrayList<>();
            Deque<String> owners = new ArrayDeque<>();
            new TreePathScanner<Void, Void>() {
                @Override
                public Void visitClass(ClassTree ct, Void v) {
                    String name = ct.getSimpleName().toString();
                    if (!name.isEmpty()) {
                        // A class owns the calls in its field initializers,
                        // initializer blocks and enum constants; no method
                        // does, and a call with no owner is in no graph.
                        List<Object[]> calls = new ArrayList<>();
                        for (Tree m : ct.getMembers()) {
                            if (m instanceof VariableTree || m instanceof BlockTree)
                                new Calls(calls).scan(new TreePath(getCurrentPath(), m), null);
                        }
                        syms.add(sym(name, "class", declLine(cu, ct, src, sp, lm),
                                line(lm, sp.getEndPosition(cu, ct)), owners.peek(), calls));
                    }
                    owners.push(name);
                    try { return super.visitClass(ct, v); } finally { owners.pop(); }
                }

                @Override
                public Void visitMethod(MethodTree mt, Void v) {
                    boolean ctor = mt.getName().contentEquals("<init>");
                    String name = ctor ? owners.peek() : mt.getName().toString();
                    if (name == null || name.isEmpty()) return super.visitMethod(mt, v);
                    // Attribution inserts the default constructor into the
                    // tree; it has no source, so it is not a declaration.
                    if (ctor && generated(mt)) return null;
                    List<Object[]> calls = new ArrayList<>();
                    if (mt.getBody() != null)
                        new Calls(calls).scan(new TreePath(getCurrentPath(), mt.getBody()), null);
                    String parent = owners.peek();
                    syms.add(sym(name, "function", declLine(cu, mt, src, sp, lm),
                            line(lm, sp.getEndPosition(cu, mt)),
                            parent == null || parent.isEmpty() ? null : parent, calls));
                    return super.visitMethod(mt, v);
                }
            }.scan(cu, null);
            sb.append(String.join(",", syms)).append("]}");
            return sb.toString();
        }

        /* Call sites in one body, with where each binds. Nested class bodies
           are skipped: their methods are symbols of their own and own their
           calls. Lambdas are not symbols, so their calls belong here. */
        final class Calls extends TreePathScanner<Void, Void> {
            final List<Object[]> out;
            Calls(List<Object[]> out) { this.out = out; }

            @Override
            public Void visitClass(ClassTree ct, Void v) { return null; }

            @Override
            public Void visitMethodInvocation(MethodInvocationTree mi, Void v) {
                ExpressionTree sel = mi.getMethodSelect();
                String name = null, recv = null;
                if (sel instanceof IdentifierTree id) {
                    name = id.getName().toString();
                } else if (sel instanceof MemberSelectTree ms) {
                    name = ms.getIdentifier().toString();
                    ExpressionTree r = ms.getExpression();
                    recv = r instanceof IdentifierTree rid ? rid.getName().toString()
                            : r instanceof MemberSelectTree rms ? rms.getIdentifier().toString() : "";
                }
                if (name != null) add(name, recv, mi, getCurrentPath());
                return super.visitMethodInvocation(mi, v);
            }

            @Override
            public Void visitMemberReference(MemberReferenceTree mr, Void v) {
                // `props::getTimeout` passes the method on to be called
                // elsewhere; for "what uses this method" it is a call site.
                String name = mr.getName().toString();
                if (!name.equals("<init>") && !name.equals("new")) {
                    ExpressionTree r = mr.getQualifierExpression();
                    String recv = r instanceof IdentifierTree rid ? rid.getName().toString()
                            : r instanceof MemberSelectTree rms ? rms.getIdentifier().toString() : "";
                    add(name, recv, mr, getCurrentPath());
                }
                return super.visitMemberReference(mr, v);
            }

            @Override
            public Void visitNewClass(NewClassTree nc, Void v) {
                Tree t = nc.getIdentifier();
                if (t instanceof ParameterizedTypeTree pt) t = pt.getType();
                if (t instanceof AnnotatedTypeTree at) t = at.getUnderlyingType();
                String name = t instanceof IdentifierTree id ? id.getName().toString()
                        : t instanceof MemberSelectTree ms ? ms.getIdentifier().toString() : null;
                if (name != null) add(name, null, nc, getCurrentPath());
                // scan(Tree) extends the current path; scan(TreePath) would
                // reset it to null on return.
                scan(nc.getEnclosingExpression(), v);
                scan(nc.getArguments(), v);
                return null;   // an anonymous class body is scanned as its own symbols
            }

            void add(String name, String recv, Tree at, TreePath path) {
                long ln = line(lm, sp.getStartPosition(cu, at));
                if (at instanceof MethodInvocationTree mi) {
                    // The line of the name, not of a receiver chain that
                    // starts several lines up.
                    ExpressionTree sel = mi.getMethodSelect();
                    long end = sp.getEndPosition(cu, sel);
                    if (end > 0) ln = line(lm, end);
                }
                Object tg = null;
                if (attributed) {
                    tg = target(path);
                    // A receiver whose type javac cannot find is a type that is
                    // not in the repository (every repo type is on the source
                    // path), so its method is external too. Without this, the
                    // call falls back to name matching and binds to whichever
                    // repo method shares the name.
                    if (tg == null && receiverMissing(at, path)) tg = "ext";
                }
                out.add(new Object[]{name, recv, ln, tg});
            }

            boolean receiverMissing(Tree at, TreePath path) {
                Tree r = null;
                if (at instanceof MethodInvocationTree mi && mi.getMethodSelect() instanceof MemberSelectTree ms)
                    r = ms.getExpression();
                else if (at instanceof MemberReferenceTree mr)
                    r = mr.getQualifierExpression();
                else if (at instanceof NewClassTree nc)
                    r = nc.getIdentifier();
                if (r == null) return false;
                try {
                    javax.lang.model.type.TypeMirror tm = trees.getTypeMirror(
                            new TreePath(new TreePath(path, at), r));
                    return tm != null && tm.getKind() == TypeKind.ERROR;
                } catch (Throwable t) {
                    return false;
                }
            }
        }

        boolean generated(MethodTree mt) {
            long start = sp.getStartPosition(cu, mt), end = sp.getEndPosition(cu, mt);
            if (start < 0 || end <= start) return true;
            try {
                Element el = trees.getElement(new TreePath(new TreePath(cu), mt));
                return el != null && elements != null
                        && elements.getOrigin(el) == javax.lang.model.util.Elements.Origin.MANDATED;
            } catch (Throwable t) {
                return false;
            }
        }

        /* Where the call at `path` binds: [rel, line], "ext", or null. */
        Object target(TreePath path) {
            Element el;
            try {
                el = trees.getElement(path);
            } catch (Throwable t) {
                return null;
            }
            if (el == null || el.asType() == null || el.asType().getKind() == TypeKind.ERROR) return null;
            if (!(el instanceof ExecutableElement) && !(el instanceof TypeElement)) return null;
            TreePath decl;
            try {
                decl = trees.getPath(el);
            } catch (Throwable t) {
                decl = null;
            }
            if (decl == null) {
                // A constructor javac generated (no source of its own) binds
                // to its class, which does have one.
                if (el.getKind() == ElementKind.CONSTRUCTOR) {
                    Element owner = el.getEnclosingElement();
                    try { decl = owner == null ? null : trees.getPath(owner); } catch (Throwable t) { decl = null; }
                }
                if (decl == null) return isFromSource(el) ? null : "ext";
            }
            CompilationUnitTree tcu = decl.getCompilationUnit();
            String abs = key(tcu.getSourceFile().toUri());
            if (root == null || !abs.startsWith(root + File.separator)) return "ext";
            String trel = abs.substring(root.length() + 1).replace(File.separatorChar, '/');
            Tree leaf = decl.getLeaf();
            try {
                String tsrc = tcu.getSourceFile().getCharContent(true).toString();
                return new Object[]{trel, declLine(tcu, leaf, tsrc, sp, tcu.getLineMap())};
            } catch (IOException e) {
                return null;
            }
        }
    }

    /* A declaration javac knows came from a .java file, even if it could not
       hand back the tree. Such a target is unknown, not external. */
    static boolean isFromSource(Element el) {
        Element e = el;
        while (e != null && !(e instanceof TypeElement)) e = e.getEnclosingElement();
        while (e != null && e.getEnclosingElement() instanceof TypeElement) e = e.getEnclosingElement();
        if (e == null) return false;
        try {
            java.lang.reflect.Field f = e.getClass().getField("sourcefile");
            Object sf = f.get(e);
            return sf != null && sf.toString().endsWith(".java");
        } catch (Throwable t) {
            return false;
        }
    }

    /* The declaration line is the signature, not a preceding annotation:
       the line the regex parser reports, so ids and `span` agree whichever
       parser ran, and a call target can be matched to its symbol by line. */
    static long declLine(CompilationUnitTree cu, Tree t, String src, SourcePositions sp, LineMap lm) {
        if (t instanceof ClassTree ct) {
            String name = ct.getSimpleName().toString();
            return line(lm, find(src, sp.getStartPosition(cu, ct),
                    "\\b(?:class|interface|enum|record|@\\s*interface)\\s+" + Pattern.quote(name) + "\\b"));
        }
        if (t instanceof MethodTree mt) {
            boolean ctor = mt.getName().contentEquals("<init>");
            long at;
            if (!ctor && mt.getReturnType() != null) {
                at = sp.getStartPosition(cu, mt.getReturnType());
                List<? extends TypeParameterTree> tps = mt.getTypeParameters();
                if (!tps.isEmpty()) at = Math.min(at, sp.getStartPosition(cu, tps.get(0)));
            } else {
                String name = ctor ? "[A-Za-z_$][\\w$]*" : Pattern.quote(mt.getName().toString());
                at = find(src, sp.getStartPosition(cu, mt), "\\b" + name + "\\s*\\(");
            }
            return line(lm, at);
        }
        return line(lm, sp.getStartPosition(cu, t));
    }

    /* Files are matched back to their input by absolute path: File.toURI()
       and the file manager's URIs do not agree on form ("file:/" vs "file:///"). */
    static String key(java.net.URI u) {
        try {
            return Paths.get(u).toAbsolutePath().normalize().toString();
        } catch (Throwable t) {
            return u.toString();
        }
    }

    static long find(String src, long from, String regex) {
        if (from < 0) return from;
        Matcher m = Pattern.compile(regex).matcher(src);
        return m.find((int) from) ? m.start() : from;
    }

    static long line(LineMap lm, long pos) {
        return pos < 0 ? -1 : lm.getLineNumber(pos);
    }

    static String sym(String name, String kind, long line, long end, String parent, List<Object[]> calls) {
        StringBuilder sb = new StringBuilder();
        sb.append("{\"name\":").append(q(name)).append(",\"kind\":\"").append(kind)
          .append("\",\"line\":").append(line).append(",\"end\":").append(end)
          .append(",\"parent\":").append(parent == null ? "null" : q(parent))
          .append(",\"calls\":[");
        if (calls != null) {
            for (int i = 0; i < calls.size(); i++) {
                if (i > 0) sb.append(',');
                Object[] c = calls.get(i);
                sb.append('[').append(q((String) c[0])).append(',')
                  .append(c[1] == null ? "null" : q((String) c[1])).append(',')
                  .append(c[2]).append(',');
                Object tg = c[3];
                if (tg == null) sb.append("null");
                else if (tg instanceof String s) sb.append(q(s));
                else {
                    Object[] a = (Object[]) tg;
                    sb.append('[').append(q((String) a[0])).append(',').append(a[1]).append(']');
                }
                sb.append(']');
            }
        }
        return sb.append("]}").toString();
    }

    static String q(String s) {
        StringBuilder sb = new StringBuilder("\"");
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                default -> {
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        return sb.append('"').toString();
    }
}
