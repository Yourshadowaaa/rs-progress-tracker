"""Completionist cape checklist: derive the requirement list from the wiki page's rendered HTML.

The page is stored as-is in `wiki_pages` and requirements are derived from it, so a parser fix can
rebuild them without refetching (`python -m tracker.jobs comp-sync --stored`). Completions are only
ever set by hand on /admin/comp and are keyed by requirement, so a resync never loses them.

Extraction is deliberately generic because the page layout was not available when this was
written: every `wikitable` row becomes one requirement, grouped by the nearest heading above the
table, the tab it sits in (tabber panels), or a full-width row inside it. A "Category" column, as in
the wiki's achievement tables, takes precedence over all of these. Sections such as references, trivia and update history
are ignored. If a page has no such tables, bullet lists under a "requirement" heading are used.
"""

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

from sqlalchemy import Engine, text
from sqlalchemy.dialects.postgresql import insert

from ..clients.wiki import WikiPage
from ..models import CompRequirement
from ..models import WikiPage as WikiPageRow

SKIP_SECTIONS = ("reference", "trivia", "update history", "history", "changes", "gallery", "see also",
                 "rewards", "perks", "transcript", "external links", "navigation")
CATEGORY_HEADERS = ("category",)  # not "subcategory": those would split the list into dozens of groups
NAME_HEADERS = ("achievement", "requirement", "name", "task", "quest", "item", "skill", "activity", "title")
DESC_HEADERS = ("description", "details", "notes", "requirements", "how to", "criteria")
SKIP_CLASSES = ("navbox", "infobox", "reference", "mw-editsection", "sortkey", "noprint", "mw-references-wrap",
                "references", "toc", "mw-empty-elt")
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
DEFAULT_CATEGORY = "Requirements"


@dataclass
class Requirement:
    category: str
    name: str
    description: str = ""


@dataclass
class _Cell:
    header: bool
    colspan: int
    parts: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return clean("".join(self.parts))


@dataclass
class _Table:
    category: str
    caption: list[str] = field(default_factory=list)
    rows: list[list[_Cell]] = field(default_factory=list)


def clean(s: str) -> str:
    s = re.sub(r"\[\d+\]|\[note \d+\]", "", s)
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip(" ,;:")


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-") or "item"


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, bool]] = []  # (tag, starts a skipped subtree)
        self.headings: dict[int, str] = {}
        self.heading: tuple[int, list[str]] | None = None
        self.tabs: list[tuple[int, str]] = []  # (stack depth, title) of open tabber panels
        self.table: _Table | None = None
        self.table_depth = 0  # stack depth of the tracked wikitable
        self.cell: _Cell | None = None
        self.caption = False
        self.lis: list[list[str]] = []
        self.tables: list[_Table] = []
        self.items: list[tuple[str, str]] = []  # (heading path, li text) for the list fallback

    @property
    def skipping(self) -> bool:
        return any(skip for _, skip in self.stack)

    def section(self) -> tuple[str, str] | None:
        """(category, full heading path) for the current position, or None inside an ignored section."""
        path = [self.headings[k] for k in sorted(self.headings) if self.headings[k]] + [t for _, t in self.tabs]
        if any(p.lower().startswith(SKIP_SECTIONS) for p in path):
            return None
        return (path[-1] if path else DEFAULT_CATEGORY), " / ".join(path)

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        classes = (a.get("class") or "").split()
        style = (a.get("style") or "").replace(" ", "").lower()
        skip = any(c in SKIP_CLASSES for c in classes) or "display:none" in style
        if tag == "table" and self.table is not None:
            skip = True  # nested table inside a tracked one
        if tag in VOID:
            return
        self.stack.append((tag, skip))
        if self.skipping:
            return
        tab_title = a.get("data-mw-tabber-title") or a.get("data-title") or a.get("title")
        if tab_title and {"tabber__panel", "tabbertab"} & set(classes):
            self.tabs.append((len(self.stack), clean(tab_title)))
        if re.fullmatch(r"h[2-4]", tag):
            self.heading = (int(tag[1]), [])
        elif tag == "table" and "wikitable" in classes:
            sec = self.section()
            if sec is not None:
                self.table, self.table_depth = _Table(category=sec[0]), len(self.stack)
        elif self.table is not None and tag == "tr":
            self.table.rows.append([])
        elif self.table is not None and tag in ("td", "th") and self.table.rows:
            span = a.get("colspan") or "1"
            self.cell = _Cell(header=tag == "th", colspan=int(span) if span.isdigit() else 1)
            self.table.rows[-1].append(self.cell)
        elif self.table is not None and tag == "caption":
            self.caption = True
        elif tag == "li":
            self.lis.append([])

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID or not any(t == tag for t, _ in self.stack):
            return
        while self.stack:
            open_tag, _ = self.stack.pop()
            self._close(open_tag)
            if open_tag == tag:
                break

    def _close(self, tag: str) -> None:
        while self.tabs and self.tabs[-1][0] > len(self.stack):
            self.tabs.pop()
        if self.heading and tag == f"h{self.heading[0]}":
            level, parts = self.heading
            self.headings = {k: v for k, v in self.headings.items() if k < level}
            self.headings[level] = clean("".join(parts))
            self.heading = None
        elif tag in ("td", "th"):
            self.cell = None
        elif tag == "caption":
            self.caption = False
        elif tag == "table" and self.table is not None and len(self.stack) < self.table_depth:
            self.tables.append(self.table)
            self.table = None
        elif tag == "li" and self.lis and not self.skipping:
            parts = self.lis.pop()
            sec = self.section()
            if self.table is None and sec is not None and "requirement" in sec[1].lower():
                self.items.append((sec[0], clean("".join(parts))))

    def handle_data(self, data: str) -> None:
        if self.skipping:
            return
        if self.heading:
            self.heading[1].append(data)
        elif self.cell is not None:
            self.cell.parts.append(data)
        elif self.caption and self.table is not None:
            self.table.caption.append(data)
        elif self.lis:
            self.lis[-1].append(data)


def _pick(headers: list[str], wanted: tuple[str, ...], exclude: int | None = None) -> int | None:
    for w in wanted:
        for i, h in enumerate(headers):
            if i != exclude and h.lower().startswith(w):
                return i
    return None


def _table_requirements(table: _Table) -> list[Requirement]:
    category = clean("".join(table.caption)) or table.category
    sub = None
    headers: list[str] = []
    out: list[Requirement] = []
    for row in table.rows:
        if not row:
            continue
        texts = [c.text for c in row]
        first = not headers and not out and sub is None
        if len(row) == 1 and (row[0].colspan > 1 or (row[0].header and not first)) and texts[0]:
            sub = texts[0]  # full-width row: a subgroup inside the table
            continue
        if all(c.header for c in row):
            headers = texts
            continue
        name_i = _pick(headers, NAME_HEADERS) if headers else None
        if name_i is None or name_i >= len(texts) or not texts[name_i]:
            name_i = next((i for i, t in enumerate(texts) if t), None)
        if name_i is None:
            continue
        cat_i = _pick(headers, CATEGORY_HEADERS, exclude=name_i) if headers else None
        row_category = texts[cat_i] if cat_i is not None and cat_i < len(texts) and texts[cat_i] else None
        desc_i = _pick(headers, DESC_HEADERS, exclude=name_i) if headers else None
        if desc_i is not None and desc_i < len(texts):
            desc = texts[desc_i]
        else:
            desc = " · ".join(t for i, t in enumerate(texts) if i not in (name_i, cat_i) and t)
        group = row_category or (f"{category} › {sub}" if sub else category)
        out.append(Requirement(group, texts[name_i], desc[:500]))
    return out


def parse_requirements(html: str) -> list[Requirement]:
    """Requirements in page order, de-duplicated on (category, name)."""
    ex = _Extractor()
    ex.feed(html)
    ex.close()
    reqs = [r for t in ex.tables for r in _table_requirements(t)]
    if not reqs:
        reqs = [Requirement(cat, name) for cat, name in ex.items if name]
    seen: set[tuple[str, str]] = set()
    unique = []
    for r in reqs:
        if (r.category, r.name) not in seen:
            seen.add((r.category, r.name))
            unique.append(r)
    return unique


def assign_keys(reqs: list[Requirement]) -> list[tuple[str, Requirement]]:
    """Stable keys: the name's slug, qualified by category only when the name repeats."""
    counts: dict[str, int] = {}
    for r in reqs:
        counts[slugify(r.name)] = counts.get(slugify(r.name), 0) + 1
    used: set[str] = set()
    out = []
    for r in reqs:
        key = slugify(r.name) if counts[slugify(r.name)] == 1 else slugify(f"{r.category} {r.name}")
        base, n = key, 2
        while key in used:
            key, n = f"{base}-{n}", n + 1
        used.add(key)
        out.append((key, r))
    return out


@dataclass
class SyncResult:
    total: int
    added: int
    removed: int
    revid: int | None

    def summary(self) -> str:
        return f"{self.total} requirements ({self.added} new, {self.removed} no longer listed), revision {self.revid}"


class NoRequirementsFound(ValueError):
    """The page parsed to nothing: the layout is not what the extractor expects."""


def store_page(engine: Engine, page: WikiPage) -> None:
    row = {"title": page.title, "revid": page.revid, "fetched_at": datetime.now(UTC), "html": page.html}
    stmt = insert(WikiPageRow).values(**row)
    with engine.begin() as conn:
        conn.execute(stmt.on_conflict_do_update(index_elements=["title"], set_=row))


def stored_page(engine: Engine, title: str) -> WikiPage | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT title, revid, html FROM wiki_pages WHERE title = :t"), {"t": title}).first()
    return WikiPage(*row) if row else None


def last_fetched(engine: Engine, title: str) -> datetime | None:
    with engine.connect() as conn:
        return conn.execute(text("SELECT fetched_at FROM wiki_pages WHERE title = :t"), {"t": title}).scalar()


def sync_requirements(engine: Engine, page: WikiPage) -> SyncResult:
    """Store the page, then upsert its requirements. Ones no longer listed are deactivated, not deleted."""
    store_page(engine, page)
    keyed = assign_keys(parse_requirements(page.html))
    if not keyed:
        raise NoRequirementsFound(f"no requirement tables found on {page.title!r}; nothing was changed")
    now = datetime.now(UTC)
    with engine.begin() as conn:
        before = set(conn.execute(text("SELECT key FROM comp_requirements WHERE active")).scalars())
        for order, (key, r) in enumerate(keyed):
            values = {"category": r.category, "name": r.name, "description": r.description,
                      "sort_order": order, "active": True, "last_seen_at": now}
            stmt = insert(CompRequirement).values(key=key, first_seen_at=now, **values)
            conn.execute(stmt.on_conflict_do_update(index_elements=["key"], set_=values))
        keys = [k for k, _ in keyed]
        conn.execute(text("UPDATE comp_requirements SET active = false WHERE active AND NOT (key = ANY(:k))"),
                     {"k": keys})
    return SyncResult(total=len(keys), added=len(set(keys) - before), removed=len(before - set(keys)),
                      revid=page.revid)


def save_completions(engine: Engine, done: set[str]) -> int:
    """Make the ticked set of active requirements exactly `done`. Inactive ones keep their state."""
    now = datetime.now(UTC)
    with engine.begin() as conn:
        active = set(conn.execute(text("SELECT key FROM comp_requirements WHERE active")).scalars())
        done &= active
        conn.execute(text("DELETE FROM comp_completions WHERE requirement_key = ANY(:a) "
                          "AND NOT (requirement_key = ANY(:d))"), {"a": list(active), "d": list(done)})
        for key in done:
            conn.execute(text("INSERT INTO comp_completions (requirement_key, completed_at) VALUES (:k, :t) "
                              "ON CONFLICT DO NOTHING"), {"k": key, "t": now})
    return len(done)
