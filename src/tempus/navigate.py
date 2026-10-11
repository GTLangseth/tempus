"""Natural-language navigation: walk the concept tree top-down to find series for a phrase.

The same Jevbox pattern as filing (src/tempus/filing.py), turned around: instead of asking
"where does this series belong?", ask "where in the tree is what this person describes?".
At each level a Chooser picks among the children plus "none of these". Up to `beam` routes
survive each level, ranked by the geometric mean of their decision probabilities (a
single-child step doesn't count as a decision). A route stops at a leaf, when "none of
these" is at least as likely as every child, or when no child clears MIN_KEEP. Finally the
chooser ranks the series under each reached concept.

With a TypeSafe key the chooser is Jev. Without one, LexicalChooser stands in: BM25 over
concept names, descriptions and the titles of the series filed beneath each option. It is
clearly labeled in every response, so nobody mistakes it for Jev's calibrated answer.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from tempus.filing import NONE, Chooser
from tempus.jev import ChoiceAnswer
from tempus.ontology import Concept, walk

BEAM = 4
MIN_KEEP = 0.2  # a child below this probability is not explored
AMBIGUOUS_MARGIN = 0.1  # two routes this close in score are both plausible
SERIES_MENU = 48  # options shown when ranking series (Jev allows up to 255)
SERIES_RESULTS = 20


class Catalog:
    """The tree plus the series filed at each concept, with lookups navigate needs."""

    def __init__(self, roots: list[Concept], series: dict[str, list[dict]]):
        self.roots = roots
        self.by_id = {c.id: c for c in walk(roots)}
        self.series = series

    def subtree_series(self, concept_id: str) -> list[dict]:
        out = []
        for c in walk([self.by_id[concept_id]]):
            out += [s | {"concept_id": c.id} for s in self.series.get(c.id, [])]
        return out


def concept_text(c: Concept) -> str:
    return f"{c.name}: {c.description}"


def series_text(s: dict) -> str:
    facets = ", ".join(str(s[k]) for k in ("frequency", "units") if s.get(k))
    return f"{s['title']} ({facets})" if facets else str(s["title"])


@dataclass
class Path:
    concepts: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)
    log_p: float = 0.0
    decisions: int = 0
    stopped: str | None = None  # leaf | none | unclear

    @property
    def score(self) -> float:
        return math.exp(self.log_p / self.decisions) if self.decisions else 1.0

    def extend(self, child: Concept, p: float, top: list, counts: bool) -> "Path":
        step = {"at": self.concepts[-1] if self.concepts else None, "choice": child.id}
        return Path(
            [*self.concepts, child.id],
            [*self.steps, step | {"p": round(p, 3), "top": top}],
            self.log_p + (math.log(max(p, 1e-6)) if counts else 0.0),
            self.decisions + counts,
        )

    def to_dict(self, catalog: Catalog) -> dict:
        return {
            "concepts": self.concepts,
            "names": [catalog.by_id[c].name for c in self.concepts],
            "steps": self.steps,
            "score": round(self.score, 4),
            "stopped": self.stopped,
        }


@dataclass
class Navigation:
    query: str
    chooser: str
    paths: list[Path]
    series: list[dict]

    @property
    def ambiguous(self) -> bool:
        return (
            len(self.paths) > 1
            and self.paths[0].concepts[-1] != self.paths[1].concepts[-1]
            and self.paths[0].score - self.paths[1].score < AMBIGUOUS_MARGIN
        )

    def to_dict(self, catalog: Catalog) -> dict:
        return {
            "query": self.query,
            "chooser": self.chooser,
            "ambiguous": self.ambiguous,
            "paths": [p.to_dict(catalog) for p in self.paths],
            "series": self.series,
        }


def navigate(
    query: str,
    catalog: Catalog,
    chooser: Chooser,
    *,
    role: str | None = None,
    beam: int = BEAM,
    label: str = "",
) -> Navigation:
    """With `role`, find only the series that plays that part in the query (e.g. "the measure
    that moves first" in "does crude lead gasoline?"); every decision sees it as `find`."""
    state = {"query": query} | ({"find": role} if role else {})
    frontier = [Path()]
    done: list[Path] = []
    while frontier:
        grown: list[Path] = []
        for path in frontier:
            parent = catalog.by_id[path.concepts[-1]] if path.concepts else None
            level = parent.children if parent else catalog.roots
            if not level:
                path.stopped = "leaf"
                done.append(path)
                continue
            by_slug = {c.id.rsplit(".", 1)[-1]: c for c in level}
            criteria = {slug: concept_text(c) for slug, c in by_slug.items()}
            if parent is None:
                criteria[NONE] = "None of these areas is what the request is about."
                question = "Which area of the economy holds the data this request asks for?"
            else:
                criteria[NONE] = f"None of these; the request is about {parent.name} generally."
                question = f"Which part of {parent.name} holds the data this request asks for?"
            ranked = chooser.choice(state, question, criteria).ranked()
            p_none = dict(ranked).get(NONE, 0.0)
            top = [[k, round(p, 3)] for k, p in ranked[:3]]
            keep = [(k, p) for k, p in ranked if k != NONE and p >= MIN_KEEP and p > p_none]
            if not keep:
                path.stopped = "none" if p_none >= ranked[0][1] else "unclear"
                if path.concepts:
                    done.append(path)
                continue
            counts = len(level) > 1
            grown += [path.extend(by_slug[k], p, top, counts) for k, p in keep]
        frontier = sorted(grown, key=lambda p: -p.score)[:beam]
    paths = sorted(done, key=lambda p: -p.score)[:beam]
    return Navigation(
        query, label or type(chooser).__name__, paths, _rank_series(state, catalog, chooser, paths)
    )


def _rank_series(state: dict, catalog: Catalog, chooser: Chooser, paths: list[Path]) -> list[dict]:
    """Rank the series under each reached concept, best route first.

    Each route gets its own menu, so series compete with their neighbors rather than with
    another route's; `score` (route score x series probability) is reported for comparison.
    Popularity breaks near-ties, e.g. UNRATE over the same title for a single state.
    """
    query = state["query"]
    out: list[dict] = []
    seen: set[str] = set()
    for rank, path in enumerate(paths):
        candidates = {
            f"{s['source']}:{s['series_id']}": s
            for s in catalog.subtree_series(path.concepts[-1])
            if f"{s['source']}:{s['series_id']}" not in seen
        }
        if not candidates:
            continue
        # Pre-filter to a menu Jev can take, by lexical match then popularity.
        lex = Bm25({ref: series_text(s) for ref, s in candidates.items()})
        menu = sorted(
            candidates,
            key=lambda r: (-lex.score(query, r), -(candidates[r].get("popularity") or 0)),
        )[:SERIES_MENU]
        criteria = {ref: series_text(candidates[ref]) for ref in menu}
        criteria[NONE] = "None of these series is what the request asks for."
        probs = chooser.choice(
            state, "Which data series is the one this request asks for?", criteria
        ).probabilities
        ranked = [
            candidates[ref]
            | {
                "p": round(probs.get(ref, 0.0), 3),
                "score": round(path.score * probs.get(ref, 0.0), 4),
                "route": rank,
            }
            for ref in menu
            if probs.get(ref, 0.0) > 0.001
        ]
        # Probabilities within ~0.1 of each other are a tie (Jev drifts by about ±0.03 between
        # identical calls), and among ties the more popular series comes first.
        ranked.sort(key=lambda s: (-round(s["p"] * 10), -(s.get("popularity") or 0)))
        out += ranked
        seen |= set(candidates)
    return out[:SERIES_RESULTS]


# --- lexical fallback --------------------------------------------------------------------

TOKEN = re.compile(r"[a-z0-9]+")
STOP = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the",
    "to", "with", "what", "how", "show", "me", "data", "series", "us", "u", "s", "united",
    "states", "total", "all", "level", "index",
}  # fmt: skip
# A few everyday words for the ones the tree uses. A hint for the fallback, not a thesaurus.
SYNONYMS = {
    "job": ["employment", "payrolls", "nonfarm"],
    "inflation": ["cpi", "prices"],
    "oil": ["crude"],
    "stock": ["equities"],
    "home": ["house", "housing"],
    "house": ["home", "housing"],
    "fed": ["federal", "reserve"],
    "layoff": ["discharges", "claims"],
    "pay": ["wages", "earnings"],
    "salary": ["wages", "earnings"],
    "growth": ["gdp"],
    "economy": ["gdp", "output"],
    "dollar": ["dollars"],
    "currency": ["exchange", "dollar"],
    "rent": ["rental", "shelter"],
    "gas": ["gasoline", "natural"],
    "recession": ["nber", "sahm"],
    "spending": ["consumption", "expenditures", "sales"],
    "fx": ["exchange"],
}


def tokens(text: str) -> list[str]:
    out = []
    for t in TOKEN.findall(text.lower()):
        if t in STOP:
            continue
        out.append(stem(t))
    return out


def stem(t: str) -> str:
    if len(t) > 4 and t.endswith("ies"):
        return t[:-3] + "y"
    if len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
        return t[:-1]
    return t


class Bm25:
    """BM25 over a small set of documents keyed by option."""

    K1, B = 1.2, 0.75

    def __init__(self, docs: dict[str, str], idf_from: list[str] | None = None):
        self.tf = {k: Counter(tokens(v)) for k, v in docs.items()}
        self.len = {k: sum(c.values()) for k, c in self.tf.items()}
        self.avg = (sum(self.len.values()) / len(self.len)) if self.len else 1.0
        corpus = [Counter(tokens(t)) for t in idf_from] if idf_from else list(self.tf.values())
        df = Counter(t for c in corpus for t in c)
        n = len(corpus)
        self.idf = {t: math.log(1 + (n - d + 0.5) / (d + 0.5)) for t, d in df.items()}
        self.default_idf = math.log(1 + (n + 0.5) / 0.5)

    def score(self, query: str, key: str) -> float:
        tf, length, total = self.tf.get(key, Counter()), self.len.get(key, 0), 0.0
        for q in set(expand(tokens(query))):
            f = tf.get(q, 0)
            if f:
                norm = f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * length / self.avg))
                total += self.idf.get(q, self.default_idf) * norm
        return total


def expand(query_tokens: list[str]) -> list[str]:
    out = list(query_tokens)
    for t in query_tokens:
        out += [s for w in SYNONYMS.get(t, []) for s in tokens(w)]
    return out


class LexicalChooser:
    """No-key stand-in for Jev: BM25 scores turned into a choice with probabilities.

    A concept option is scored by its best-matching piece of text: its own name and
    description (full weight), a concept beneath it (0.8) or a series filed beneath it
    (0.6). So "crude" finds Prices even though the word only appears deep in the tree, while
    an area whose own description names the thing ("GDP" under Output) beats one where it
    only turns up in series titles ("as Percent of GDP" under Government Finance). "None of
    these" gets a fixed score, so a request that matches nothing stops instead of wandering.
    """

    OWN, BELOW, SERIES = 1.0, 0.8, 0.6
    NONE_SCORE = 1.5
    TEMPERATURE = 1.0

    def __init__(self, catalog: Catalog):
        units: list[str] = []
        index: dict[str, int] = {}

        def unit(text: str) -> int:
            if text not in index:
                index[text] = len(units)
                units.append(text)
            return index[text]

        self.members: dict[str, list[tuple[int, float]]] = {}
        self.docs: dict[str, str] = {}
        for c in walk(catalog.roots):
            below = [concept_text(d) for d in walk(c.children)]
            titles = [series_text(s) for s in catalog.subtree_series(c.id)]
            m = [(unit(concept_text(c)), self.OWN)]
            m += [(unit(t), self.BELOW) for t in below]
            m += [(unit(t), self.SERIES) for t in titles]
            self.members[concept_text(c)] = m
            self.docs[concept_text(c)] = " ".join([concept_text(c), *below, *titles])
        for ss in catalog.series.values():
            for s in ss:
                unit(series_text(s))
        self.index = index
        self.units = units
        self.bm25 = Bm25(dict(enumerate(units)))
        self._memo: tuple[str, dict[int, float]] = ("", {})

    def _unit_score(self, query: str, i: int) -> float:
        if self._memo[0] != query:
            self._memo = (query, {})
        memo = self._memo[1]
        if i not in memo:
            memo[i] = self.bm25.score(query, i)
        return memo[i]

    def _score(self, query: str, text: str) -> float:
        members = self.members.get(text)
        if members is None:
            if text not in self.index:
                return Bm25({0: text}).score(query, 0)
            members = [(self.index[text], 1.0)]
        return max(w * self._unit_score(query, i) for i, w in members)

    def choice(self, state, instructions: str, criteria: dict[str, str]) -> ChoiceAnswer:
        query = state["query"]
        options = {k: v for k, v in criteria.items() if k != NONE}
        scores = {k: self._score(query, v) for k, v in options.items()}
        if all(v in self.docs for v in options.values()):
            # Concepts: blend the best single match with how much of the whole subtree
            # matches, so an area that is *about* the request beats a stray mention.
            whole = Bm25({k: self.docs[v] for k, v in options.items()}, idf_from=self.units)
            scores = {k: 0.5 * s + 0.5 * whole.score(query, k) for k, s in scores.items()}
        if NONE in criteria:
            scores[NONE] = self.NONE_SCORE
        top = max(scores.values(), default=0.0)
        weights = {k: math.exp((s - top) / self.TEMPERATURE) for k, s in scores.items()}
        total = sum(weights.values())
        probs = {k: w / total for k, w in weights.items()}
        return ChoiceAnswer(max(probs, key=probs.get), probs)
