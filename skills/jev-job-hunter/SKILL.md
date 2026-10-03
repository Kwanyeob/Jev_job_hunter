---
name: jev-job-hunter
description: >-
  Finds AI and software-engineer jobs by driving Chrome DevTools MCP and letting
  Jev (TypeSafe System One) score every careers link and job title. Use when the
  user wants to find jobs, run the job hunter, browse careers pages, match AI
  engineer roles, or mentions Jev / TypeSafe job search. Also use when the user
  gives a resume (PDF) and wants LinkedIn jobs ranked against it in Excel.
  Chrome sees and acts; Jev decides — the host LLM never picks links.
---

# Jev AI Job Hunter

Host CLI skill. **Chrome sees and acts. Jev decides.** You never choose links, never judge fit, never invent URLs.

Run every command from the repo root with `uv run jjh ...`.

## Setup

1. If `.env` has no `TYPESAFE_API_KEY`, tell the user and use `--mock` **only if they agree**. Copy `.env.example` otherwise.
2. Use a **visible** Chrome window. `jjh run` talks to Chrome over CDP itself — do **not** call `list_pages` / `evaluate_script` / `navigate_page` in the primary path (a second debugger connection makes Chrome ask Allow again).
3. Follow ATS redirects (Greenhouse / Lever / Ashby / Workday). Cross-domain is expected. Never log in, apply, or fill forms.

## LinkedIn from a resume

Use when the user hands you a resume and wants LinkedIn jobs ranked for it. Two steps; you write the profile, Jev does all the judging.

1. **Resume → `config/profile.yaml`.** Read the PDF yourself (Read tool). Write `config/profile.yaml` with exactly the shape of `config/profile.example.yaml`:
   - `candidate`: `headline`, `summary` (2–3 sentences), `years_experience` (number), `seniority`, `skills` (concrete technologies, ≤ 20), `target_roles` (3–5 LinkedIn-style titles), `interests`, `locations`, `languages`.
   - `search`: `queries` (2–3 short titles from `target_roles`), `location`, `remote`, `posted`.
   - **No name, email, phone, address, or profile links** — everything under `candidate` is sent to Jev. Only what the resume supports; do not embellish.
   - Show the user the YAML and ask them to confirm or edit `target_roles`, `locations`, and `search` before running. The file is git-ignored.
2. **Hunt.** `uv run jjh linkedin` (flags override `search`: `--query` repeatable, `--location`, `--posted day|week|month|any`, `--remote any|remote|hybrid|onsite`, `--max-cards`, `--max-open`). Stream the entire stdout and give the user the `Excel` path from the report.

Flow: **LinkedIn public search (logged out) → Jev triages every card title → open the top cards via LinkedIn's guest posting endpoint (full description + seniority criteria; the company ATS only when an apply URL is exposed, which logged-out LinkedIn no longer does) → Jev fit vs the profile → `results/linkedin-*.xlsx`** (sheets Matches / All listings / Profile). `jjh` launches its own Chrome profile on port 9222 if no debuggable Chrome is found.

If the user wants a UI instead: `uv run jjh web` (resume drop → Claude Fable 5.1 fills the profile when `ANTHROPIC_API_KEY` is set; live table; Excel download).

- Never log in to LinkedIn, never click Easy Apply. One login wall skips a posting; if stdout says `LinkedIn login wall` at the end, two came in a row and the partial Excel is already written — tell the user; retry later or with a smaller `--max-open` / larger `--delay`.
- You do not re-rank or second-guess Jev's scores in your summary; report them as-is.

## Loop

**Primary:** `uv run jjh run --url https://<company-homepage> --query "<role keywords>"`.

Same flow on every site: **homepage → Jev picks Careers/Jobs → listing (filter / scroll / next page) → job details → report**. Never invent `/careers`. `--query` is short titles/skills, never a full sentence. Stream the **entire** stdout. Catalog ids (`openai`, `typesafe`) are shortcuts, not a closed list.

`jjh` **scrolls each page to the bottom before scoring links** (footer Careers/Jobs often fail `checkVisibility` above the fold), then scores listing **filters** with Jev (from the query — no hardcoded team names) and pages by clicking next / **scrolling** until the list stops growing.

**Slow-motion:** `uv run jjh start --query "..."` then `uv run jjh step` → MCP `navigate_page` on the same tab. Never `filePath` / `take_snapshot`.

**Fallback** (stdout contains `Chrome CDP not reachable`): `evaluate_script` with the JS below (`uv run jjh js`) then `uv run jjh step --page -`.

```js
() => {
  const cur = location.href.replace(/#.*$/, "");
  const tidy = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const links = [], seen = new Map();
  for (const a of document.querySelectorAll("a[href]")) {
    const raw = (a.getAttribute("href") || "").trim();
    if (!raw || raw[0] === "#" || /^(javascript:|mailto:|tel:)/i.test(raw)) continue;
    const href = String(a.href).replace(/#.*$/, "");
    if (!href || href === cur) continue;
    if (a.checkVisibility && !a.checkVisibility({checkOpacity: true})) continue;
    let text = tidy(a.innerText || "");
    text = text.replace(/\(opens in a new (window|tab)\)/gi, "").trim();
    if (!text) text = a.getAttribute("aria-label") || a.title || "";
    if (!text) {
      const last = (href.replace(/^[a-z]+:\/\/[^/]+/i, "").split(/[?#]/)[0] || "").split("/").filter(Boolean).pop() || "";
      try { text = decodeURIComponent(last).replace(/[-_]/g, " "); } catch { text = last.replace(/[-_]/g, " "); }
    }
    text = tidy(text).slice(0, 100);
    if (!text && !href) continue;
    if (seen.has(href)) {
      if (text && !seen.get(href)) { seen.set(href, text); const p = links.find(l => l.href === href); if (p) p.text = text; }
      continue;
    }
    seen.set(href, text);
    links.push({ text, href });
    if (links.length >= 2000) break;
  }
  const controls = [], cseen = new Set();
  for (const el of document.querySelectorAll('button, label, [role="option"], [role="menuitemcheckbox"], [role="menuitem"], [role="checkbox"], [role="combobox"]')) {
    let text = tidy(el.innerText || el.getAttribute("aria-label") || "");
    text = text.replace(/\(opens in a new (window|tab)\)/gi, "").trim().slice(0, 60);
    if (!text || text.length < 2 || cseen.has(text)) continue;
    cseen.add(text);
    controls.push({ text });
    if (controls.length >= 50) break;
  }
  const body = document.body;
  return {
    url: location.href, title: document.title, links, controls,
    text: ((body && body.innerText) || "").replace(/\n{3,}/g, "\n\n").slice(0, 6000),
    scroll: { y: window.scrollY, h: body ? body.scrollHeight : 0, inner: window.innerHeight }
  };
}
```

## Hard rules

- You do **not** pick Careers, filters, or job titles. `jjh` / Jev already scored them.
- You do **not** invent careers URLs or filter names.
- Never click Apply, never log in, never fill forms.
- Cookie banner: the **one** allowed host decision, only if `jjh` cannot proceed.
