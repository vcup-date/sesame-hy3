// A small markdown renderer. Headings, lists, tables, fences, inline code,
// bold, italic, links, quotes, rules. Escapes first, so model output cannot
// inject HTML.

const esc = (s) => s.replace(/[&<>"]/g, (c) => (
  { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]
));

function inline(s) {
  s = esc(s);

  // Code comes out first and is parked behind a placeholder. `subtotal * discount`
  // otherwise hands its asterisks to the italic rule, and everything after it on
  // the line goes italic.
  const code = [];
  s = s.replace(/`([^`]+)`/g, (_, body) => {
    code.push(body);
    return `\u0000${code.length - 1}\u0000`;
  });

  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*(\S[^*\n]*?\S|\S)\*(?=[\s).,!?;:]|$)/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
  s = s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  s = s.replace(/(^|[\s])(https?:\/\/[^\s<]+)/g,
    '$1<a href="$2" target="_blank" rel="noreferrer">$2</a>');

  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${code[Number(i)]}</code>`);
}

export function md(src) {
  const lines = String(src || "").split("\n");
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // fenced code
    if (/^\s*```/.test(line)) {
      const lang = line.replace(/^\s*```/, "").trim();
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) body.push(lines[i++]);
      i++;
      out.push(`<pre><code data-lang="${esc(lang)}">${esc(body.join("\n"))}</code></pre>`);
      continue;
    }

    // table
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || "")) {
      const cells = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(
        "<table><thead><tr>" + head.map((h) => `<th>${inline(h)}</th>`).join("") +
        "</tr></thead><tbody>" +
        rows.map((r) => "<tr>" + r.map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
        "</tbody></table>"
      );
      continue;
    }

    // list
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>` + items.map((t) => `<li>${inline(t)}</li>`).join("") + `</${tag}>`);
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      out.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      continue;
    }

    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }

    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      const level = Math.min(h[1].length + 1, 6);
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    if (!line.trim()) { i++; continue; }

    const para = [];
    while (i < lines.length && lines[i].trim() &&
           !/^\s*(```|\||>|#{1,6}\s|([-*+]|\d+[.)])\s)/.test(lines[i])) {
      para.push(lines[i++]);
    }
    // Always consume at least this line. While an answer is still streaming, a
    // table's first row arrives before its |---| separator: the table branch
    // above declines it, the paragraph loop above refuses it too, and i never
    // moves. That is an infinite loop, and it freezes the tab.
    if (!para.length) para.push(lines[i++]);
    out.push(`<p>${inline(para.join("\n"))}</p>`);
  }

  return out.join("\n");
}

// Tool output: colour a unified diff, leave everything else alone.
export function toolBody(text) {
  const t = String(text || "");
  if (!/^@@|\n@@|\n[+-]/.test(t)) return esc(t);
  return t.split("\n").map((l) => {
    if (/^@@/.test(l)) return `<span class="hunk">${esc(l)}</span>`;
    if (/^\+/.test(l)) return `<span class="add">${esc(l)}</span>`;
    if (/^-(?!-)/.test(l)) return `<span class="del">${esc(l)}</span>`;
    return esc(l);
  }).join("\n");
}
