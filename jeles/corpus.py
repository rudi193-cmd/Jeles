"""Jeles' verified-nugget corpus — storage and ranked lookup.

A nugget is a human-verified question/answer pair with citations:
{question, answer, sources, verified_by, verified_at, tags}.

Storage reuses the same SQLite shape as willow-mcp's SOIL `Store` (a
`records` table under `<collection>/store.db`, keyed by WILLOW_STORE_ROOT),
so nuggets written here are already visible to a Willow-style soil scan
with no extra wiring, and the corpus stays readable by anything else that
understands a Willow-style SOIL collection.

This module has no MCP dependency and does no network I/O — see
corpus_server.py for the MCPServer wrapper that exposes it as a standalone,
MCP-agnostic server. It imports only the standard library, on purpose:
that is what keeps its tests fast and network-free, and lets it be reused
as a plain library by any host.

The default collection names (`ask_jeles_corpus` / `ask_jeles_corpus_gaps`)
are preserved from Ask Jeles for back-compat so existing stores keep
resolving; they can be overridden via env for a differently-scoped host.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from itertools import pairwise
from pathlib import Path
from typing import Any

NUGGETS_COLLECTION = os.environ.get("JELES_CORPUS_COLLECTION", "ask_jeles_corpus")
GAPS_COLLECTION = os.environ.get("JELES_CORPUS_GAPS_COLLECTION", "ask_jeles_corpus_gaps")

# A collection name becomes a path component (<root>/<collection>/store.db), so
# an unvalidated one (e.g. from an env var a launcher forwards) could traverse
# out of WILLOW_STORE_ROOT. Mirror willow-mcp's SOIL Store guard — the sibling
# this schema is copied from already validates; this copy hadn't.
_COLLECTION_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _validate_collection(collection: str) -> None:
    if not _COLLECTION_RE.match(collection or ""):
        raise ValueError(
            f"invalid collection name (must match {_COLLECTION_RE.pattern}): {collection!r}"
        )


# The `deviation`/`action` columns are willow-mcp's (SOIL Store, db.py). jeles
# never reads them, but a jeles-created collection missing them makes a
# willow-mcp writer pointed at the same store fail `no such column: deviation`
# (box audit A3 — the two independently-drifted "shared" schemas). Carry them so
# the store stays mutually usable; _migrate_records() backfills older stores.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id         TEXT PRIMARY KEY,
    data       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deviation  REAL NOT NULL DEFAULT 0.0,
    action     TEXT NOT NULL DEFAULT 'work_quiet',
    deleted    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deleted ON records(deleted);
"""


def _migrate_records(conn: sqlite3.Connection) -> None:
    """Add willow-mcp's SOIL columns to a pre-existing jeles store that predates
    this change, so a shared collection stays compatible either direction."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(records)")}
    if "deviation" not in have:
        conn.execute("ALTER TABLE records ADD COLUMN deviation REAL NOT NULL DEFAULT 0.0")
    if "action" not in have:
        conn.execute("ALTER TABLE records ADD COLUMN action TEXT NOT NULL DEFAULT 'work_quiet'")


_lock = threading.RLock()
_conns: dict[str, sqlite3.Connection] = {}


def _store_root() -> Path:
    default = str(Path.home() / ".willow" / "store")
    return Path(os.environ.get("WILLOW_STORE_ROOT", default)).expanduser()


def _conn(collection: str) -> sqlite3.Connection:
    _validate_collection(collection)
    db_path = _store_root() / collection / "store.db"
    key = str(db_path)
    with _lock:
        if key not in _conns:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            # isolation_level=None turns off the driver's implicit transaction
            # handling so `_write` can take a real BEGIN IMMEDIATE. Without it,
            # a read-modify-write is only as atomic as the in-process lock —
            # which is nothing at all to a second process, and this store is
            # explicitly designed to be shared with willow-mcp.
            conn = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
            # WAL lets readers run while a writer holds the database, and
            # busy_timeout makes a contended write wait rather than raising
            # `database is locked` out of put_nugget/ask_corpus — which no
            # caller expects, since both are documented as returning dicts.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            _migrate_records(conn)
            _conns[key] = conn
        return _conns[key]


@contextmanager
def _write(conn: sqlite3.Connection):
    """One atomic write, across processes as well as threads.

    BEGIN IMMEDIATE takes the write lock up front, so a read inside this block
    cannot be overtaken between the read and the write that follows it. The
    module-level `_lock` only ever ordered threads within one interpreter.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Strip C0 control chars (keep tab/newline) from stored text. A NUL that
# truncates downstream C-string tooling, or a BEL nobody can retype, has no
# place in a nugget or a logged question. (Mirrors the-squirrel's db.sanitize —
# the same input-hygiene rule at each app's single write boundary.)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _clean(obj: Any) -> Any:
    if isinstance(obj, str):
        return _CONTROL_CHARS.sub("", obj)
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]  # tuples json-serialize as lists anyway
    return obj


class _WriteRefused(Exception):
    """A guard vetoed a write from inside its transaction. Carries the caller's
    error payload so the public function can return it rather than raise."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload.get("error", "refused"))
        self.payload = payload


def _put(
    collection: str,
    record: dict[str, Any],
    record_id: str | None = None,
    guard: Any = None,
) -> str:
    """Write a record. `guard(existing, tombstoned)` runs *inside* the write
    transaction and may return an error payload to abort it.

    The guard has to be in here rather than in the caller: a check that reads
    the prior record, returns, and only then writes is a read-modify-write with
    nothing holding the gap — the same shape that lost 36 of 50 gap counts.
    """
    rid = record_id or uuid.uuid4().hex[:8]
    now = _now()
    record = _clean(record)  # one chokepoint — covers nuggets and gaps alike
    with _lock:
        conn = _conn(collection)
        with _write(conn):
            if guard is not None:
                row = conn.execute(
                    "SELECT data, deleted FROM records WHERE id = ?", (rid,)
                ).fetchone()
                refusal = guard(json.loads(row[0]) if row else None, bool(row and row[1]))
                if refusal is not None:
                    raise _WriteRefused(refusal)
            # `INSERT OR REPLACE` deletes the row and inserts a new one, so every
            # column not named here reverted to its schema default. That silently
            # reset willow-mcp's `deviation`/`action` — the very columns this
            # module carries so the store stays mutually usable — and forced
            # `deleted = 0`, resurrecting a soft-deleted record. An upsert that
            # names only what jeles owns leaves the rest exactly as it found it,
            # including `created_at`, which is why the read it used to need is
            # gone. Same fix willow-mcp made in its own Store.put.
            conn.execute(
                "INSERT INTO records (id, data, created_at, updated_at, deleted) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (rid, json.dumps(record), now, now),
            )
    return rid


def _get(collection: str, record_id: str) -> dict[str, Any] | None:
    with _lock:
        row = (
            _conn(collection)
            .execute(
                "SELECT data, created_at, updated_at FROM records WHERE id = ? AND deleted = 0",
                (record_id,),
            )
            .fetchone()
        )
    if not row:
        return None
    record = json.loads(row[0])
    record["_id"] = record_id
    record["_created"] = row[1]
    record["_updated"] = row[2]
    return record


def _all(collection: str) -> list[dict[str, Any]]:
    with _lock:
        rows = (
            _conn(collection)
            .execute(
                "SELECT id, data, created_at, updated_at FROM records "
                "WHERE deleted = 0 ORDER BY updated_at DESC"
            )
            .fetchall()
        )
    out = []
    for rid, data, created, updated in rows:
        record = json.loads(data)
        record["_id"] = rid
        record["_created"] = created
        record["_updated"] = updated
        out.append(record)
    return out


_STOP = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "that",
    "this",
    "these",
    "those",
    "have",
    "has",
    "had",
    "was",
    "were",
    "are",
    "is",
    "been",
    "being",
    "what",
    "who",
    "when",
    "where",
    "why",
    "how",
    "which",
    "would",
    "could",
    "should",
    "does",
    "did",
    "about",
    "into",
    "your",
    "you",
    "tell",
    "show",
    "find",
    "give",
    "please",
    "can",
    "will",
    "its",
    "it's",
    # Short function words. Only `_confidence` and `log_gap`'s short-code
    # segment ever see words this brief — `_WORD_RE` needs three characters —
    # so listing them here costs nothing elsewhere. They are here so that
    # `_ask_tokens` can treat *every* remaining short word as meaning-bearing:
    # "up" and "down" change an answer, "of" and "in" do not, and without the
    # separation either all short words are noise or none of them are.
    # Deliberately no single letters: "drug A" vs "drug B" is exactly the case
    # log_gap's short-code segment exists to keep apart (test_hardening.py).
    "an",
    "as",
    "at",
    "be",
    "by",
    "do",
    "he",
    "if",
    "in",
    "it",
    "me",
    "my",
    "of",
    "on",
    "or",
    "so",
    "to",
    "us",
    "we",
}


# The old class was `[a-z0-9][a-z0-9_-]{2,}` — ASCII-only, minimum three
# characters. Both halves made questions *permanently* unanswerable, because
# `_ranked` returns [] on an empty token list and `ask_corpus` then logs a gap:
# measured on this store, three of four nuggets stored and asked back with the
# byte-identical string returned found=False and filed a gap each time. So the
# growth queue filled with questions the corpus already had answers to.
#
#   '¿Cuál es el color primario?' -> ['color', 'primario']   ("cuál" cut at the á)
#   '什么是主色?'                   -> []
#   'Is it up?' / 'AI vs ML?'     -> []
#
# Three rules, in the order they apply:
#
# 1. Word characters are Unicode. `[^\W_]` is "alnum, not underscore" — the
#    Unicode equivalent of the old `[a-z0-9]` — so accented Latin, Cyrillic and
#    Greek words survive whole instead of being truncated at the first
#    non-ASCII byte: 'naïve résumé' was ['sum'] and is ['naïve', 'résumé'],
#    'Какой основной цвет?' was [] and is three tokens. This *does* change the
#    tokens of already-stored non-ASCII text, which is the point of the fix;
#    pure-ASCII text is untouched, since `[^\W_][\w-]{2,}` and
#    `[a-z0-9][a-z0-9_-]{2,}` agree on every ASCII string.
# 2. CJK is cut into character bigrams. Chinese, Japanese and Korean are not
#    space-delimited, so a word-boundary tokenizer returns one token per run
#    (all-or-nothing matching) or, with the ASCII class, nothing. Bigrams are
#    the cheap standard fallback: they make the identical-string case match
#    exactly, and give `search_nuggets` partial credit for a shared phrase.
#    They are not segmentation — see the limit recorded on `_confidence`.
# 3. The three-character minimum stays; a text that yields *no* token under
#    rules 1-2 falls back to its short words instead. Deliberately conditional:
#    lowering the minimum globally would re-tokenize every nugget and shift
#    every ranking, and it would break `log_gap`'s short-code segment
#    (tests/test_hardening.py). Conditional means the fallback only ever adds
#    tokens where there were none, and never reweights an existing question.
_CJK_RE = re.compile(
    "[\u3040-\u30ff"  # hiragana + katakana
    "\u3400-\u4dbf"  # CJK unified ideographs extension A
    "\u4e00-\u9fff"  # CJK unified ideographs
    "\uf900-\ufaff"  # CJK compatibility ideographs
    "\uac00-\ud7af]+"  # hangul syllables
)
_WORD_RE = re.compile(r"[^\W_][\w-]{2,}")
# The apostrophe binds: "what's" is one token, not "what" plus a bare "s".
# Without that, `_ask_tokens` reads the "s" as a content word the other
# phrasing lacks and refuses a pure rephrasing — measured, "What's the primary
# color in Grove?" stopped matching "What is the primary color in Grove?".
# (`_STOP` has carried "it's" since before this, on the same assumption.)
_SHORT_RE = re.compile(r"[^\W_][\w'\u2019-]*")


def _tokens(text: str) -> list[str]:
    """Content tokens, in the order they appear. Order is load-bearing for
    `log_gap`, which keys on bigrams of this list."""
    lowered = (text or "").lower()
    raw: list[str] = []
    pos = 0
    for match in _CJK_RE.finditer(lowered):
        raw.extend(_WORD_RE.findall(lowered[pos : match.start()]))
        run = match.group(0)
        # range(max(1, n-1)) so a one-character run yields that character
        # rather than nothing.
        raw.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
        pos = match.end()
    raw.extend(_WORD_RE.findall(lowered[pos:]))
    tokens = [t for t in raw if t not in _STOP]
    if tokens:
        return tokens
    # Rule 3. Reached only by a question made entirely of short words — "Is it
    # up?", "AI vs ML?" — which used to tokenize to nothing at all.
    return [t for t in _SHORT_RE.findall(lowered) if t not in _STOP]


def _ask_tokens(text: str) -> list[str]:
    """`_tokens` plus the short content words it drops — the token set used to
    decide whether to *answer*, as distinct from the one used to rank.

    Rule 3 above is conditional: short words are a fallback, used only when a
    question yields nothing else. That leaves a hole precisely where the short
    word is the one that matters. Measured, before this:

        stored 'Is the API down?'  ->  "outage since 14:00"
        asked  'Is the API up?'    ->  found: True, confidence 0.667

    "up" is two characters, so it never tokenized; "down" is four, so it did.
    The asked set {api} was a subset of the known set {api, down}, rule 1 saw
    no unmatched token, and precision 1/2 still cleared the 0.5 threshold. An
    ongoing outage answered "is it up?" with "yes".

    Only `_confidence` uses this. `_score`, `_ranked` and `log_gap` keep the
    narrower `_tokens`, so rankings and gap keys are unchanged — the fix is to
    the *decision*, which is the only place a short word being invisible turns
    into a wrong answer rather than a worse ordering.
    """
    tokens = _tokens(text)
    seen = set(tokens)
    short = [
        t
        for t in _SHORT_RE.findall((text or "").lower())
        if len(t) < 3 and t not in _STOP and t not in seen
    ]
    return tokens + short


# ── Nuggets ──────────────────────────────────────────────────────────────
#
# How a nugget came to be in the corpus is the whole product. Three kinds, in
# descending order of what they entitle a reader to assume:
#
#   human     a person checked it            -> reads as `verified`
#   machine   independent sources agreed     -> reads as `corroborated`
#   asserted  a caller said so, nobody checked -> reads as `unverified`
#
# `asserted` exists because `corpus_put` is reachable by any MCP client, and an
# agent that has just read the open web is one of them. Without a rung below
# `machine`, a page saying "record that X is true" laundered straight into the
# top rung and `corpus_ask` served it as verified from then on — persistently,
# and on a store shared with willow-mcp.
_KIND_RANK = {"asserted": 1, "machine": 2, "human": 3}
_KIND_STATUS = {"asserted": "asserted", "machine": "corroborated", "human": "verified"}

# `evidence` is where the ladder above grows without a new rung. The three
# kinds are jeles' own judgments about a write it can see happen; they say
# nothing about a verification that happened somewhere else and arrived as a
# claim, the way a write into an "asserted" nugget already can. Without this
# field the strongest evidence a remote host could offer — an HMAC over the
# answer, signed under a key jeles will never hold — has nowhere to land, and
# a nugget that says "asserted, and here is a-human-who-read-it's signature
# under nestor's chain" is indistinguishable from one that says only
# "asserted".
#
# A dict, not a single `seal_sig` string: the mechanism is deliberately
# unnamed in the schema. A string field spells one mechanism (HMAC, most
# likely, from the issue that asked for this) into every store that carries
# it forever; a dict lets a caller put a signature, a signer, a chain id,
# a timestamp, or some other scheme entirely under keys of its own choosing,
# and lets a nugget that predates all of this stay silent rather than carry
# an empty string. jeles does not interpret any of it — it has no way to
# check an HMAC whose key belongs to the other side, and checking is not the
# point. The point is that a reader, including one that cannot verify a
# single byte, can see that evidence exists and where it claims to come
# from, and a host that does hold the key can check it itself.


def _kind_of(nugget: dict[str, Any]) -> str:
    """The stored kind, defaulting protectively. A nugget written before this
    field existed was human-entered, and an unrecognised value is treated as the
    highest rung so a garbled record cannot be overwritten by a lower one."""
    kind = str(nugget.get("verification_kind") or "human")
    return kind if kind in _KIND_RANK else "human"


def put_nugget(
    question: str,
    answer: str,
    sources: list[str],
    verified_by: str,
    tags: list[str] | None = None,
    nugget_id: str | None = None,
    verified_at: str | None = None,
    verification_kind: str = "human",
    written_by: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Add or update a nugget. Returns {id, action, verification_kind} or {error}.

    ``verification_kind`` is the rung this write is entitled to: ``"human"``
    (the default, for in-process callers, which are the operator's own code),
    ``"machine"`` for corroboration like the conflict-scan reaction's
    two-independent-source finding, or ``"asserted"`` for a write that arrived
    over a tool call and that nobody has checked. It is meant to be set by the
    *driver*, never passed through from caller data — see
    :func:`jeles.corpus_server.corpus_put`, which pins it to ``"asserted"``.

    ``verified_by`` is a claim: whatever string the writer supplied.
    ``written_by`` is the fact beside it — which app actually made the write —
    and is what :func:`to_search_hit` shows for an asserted nugget, because a
    caller can type any name it likes into the first one.

    **A write may not overwrite a nugget of a higher kind.** Without that,
    every protection here is one ``nugget_id=`` away from being bypassed:
    an asserted write would simply land on top of a human-verified answer,
    keeping its id and its place in every search result.

    ``evidence`` is optional and jeles never checks it — see the comment
    above ``_KIND_RANK``. It rides alongside ``verification_kind`` rather
    than replacing it: a nugget can be "asserted" *and* carry a signature
    jeles cannot check, and the two say different things to a reader who can
    check it. Omit it and a nugget behaves exactly as it did before this
    parameter existed — nothing is written, and :func:`to_search_hit` shows
    an empty ``evidence``.
    """
    question = (question or "").strip()
    answer = (answer or "").strip()
    verified_by = (verified_by or "").strip()
    if not question or not answer or not verified_by:
        return {"error": "question, answer, and verified_by are required"}
    kind = str(verification_kind or "").lower()
    if kind not in _KIND_RANK:
        return {
            "error": f"verification_kind must be one of "
            f"{', '.join(sorted(_KIND_RANK))} (got {verification_kind!r})"
        }
    if evidence is not None and not isinstance(evidence, dict):
        return {"error": f"evidence must be a dict (got {type(evidence).__name__})"}
    record = {
        "question": question,
        "answer": answer,
        "sources": [str(s) for s in (sources or [])],
        "verified_by": verified_by,
        "verified_at": verified_at or datetime.now(timezone.utc).date().isoformat(),
        "tags": [str(t) for t in (tags or [])],
        "status": _KIND_STATUS[kind],
        "verification_kind": kind,
    }
    if written_by:
        record["written_by"] = str(written_by)
    if evidence:
        record["evidence"] = dict(evidence)

    outcome: dict[str, Any] = {}

    def _guard(existing: dict[str, Any] | None, tombstoned: bool) -> dict[str, Any] | None:
        if existing is not None:
            prior = _kind_of(existing)
            if _KIND_RANK[prior] > _KIND_RANK[kind]:
                return {
                    "error": "kind_downgrade_refused",
                    "id": nugget_id,
                    "detail": (
                        f"nugget {nugget_id} was written as '{prior}' and this "
                        f"write is '{kind}'. A lower rung cannot overwrite a "
                        "higher one — write it as a new nugget (omit nugget_id) "
                        "and let a person supersede the existing one."
                    ),
                    "existing_kind": prior,
                    "attempted_kind": kind,
                }
        # `_get` filters `deleted = 0`, so a soft-deleted id looked absent and
        # the write reported "created" while landing on a tombstoned row that no
        # reader will ever return. The write is not refused — refusing would let
        # anyone who can soft-delete a record permanently deny the id — but it
        # says so.
        outcome["action"] = (
            "updated_tombstoned" if tombstoned else "updated" if existing is not None else "created"
        )
        return None

    try:
        rid = _put(NUGGETS_COLLECTION, record, record_id=nugget_id, guard=_guard)
    except _WriteRefused as refused:
        return refused.payload
    # The kind comes back in the receipt: a caller that asked for one rung and
    # got another should not have to re-read the record to find out.
    return {"id": rid, "action": outcome["action"], "verification_kind": kind}


def get_nugget(nugget_id: str) -> dict[str, Any]:
    return _get(NUGGETS_COLLECTION, nugget_id) or {"error": "not_found"}


def list_nuggets(limit: int = 50) -> list[dict[str, Any]]:
    return _all(NUGGETS_COLLECTION)[: max(0, limit)]


def _score(nugget: dict[str, Any], query_tokens: list[str]) -> float:
    if not query_tokens:
        return 0.0
    question = (nugget.get("question") or "").lower()
    answer = (nugget.get("answer") or "").lower()
    tags = " ".join(nugget.get("tags") or []).lower()
    q_tokens = set(_tokens(question))
    matched = sum(1 for t in query_tokens if t in q_tokens)
    score = matched / len(query_tokens)
    if set(query_tokens) == q_tokens:
        score += 1.0
    elif matched == len(query_tokens):
        score += 0.5
    if any(t in answer for t in query_tokens):
        score += 0.1
    if any(t in tags for t in query_tokens):
        score += 0.1
    return score


def _confidence(nugget: dict[str, Any], query_tokens: list[str]) -> float:
    """How much of *this question* the nugget's question actually is.

    Separate from `_score` on purpose. `_score` ranks candidates and rewards a
    nugget for mentioning the query anywhere — in its answer, in its tags. That
    is right for ordering search results and wrong for deciding whether to
    answer, because a bonus earned in the tags can carry a nugget over the
    threshold on the strength of a word that is not in its question at all.

    Two rules, both learned from cases where the old score answered confidently
    and wrongly:

    1. **An unmatched query token is disqualifying.** If the asker used a
       content word this nugget's question does not contain, it is probably a
       different question. "rotate the *production* database password" against a
       nugget about *staging* shares every other word — the old recall-only
       score made the one word that changes the answer worth 1/4 of the
       decision. It is worth all of it.

    2. **Overlap is symmetric.** Rule 1 alone still lets a one-word query match
       any nugget containing that word ("vaccine" → a nugget about flu vaccines
       in pregnancy). Scoring the harmonic mean of precision and recall means a
       query far narrower than the nugget it matched scores low, so the corpus
       says "I don't know yet" rather than answering a question nobody asked.

    Returns 0.0 when the nugget cannot answer the question as asked.

    A limit worth naming, since it is what CJK support here amounts to: `_tokens`
    cuts CJK into character bigrams rather than segmenting words, so a rephrased
    CJK question almost always carries a bigram straddling the changed words —
    and rule 1 makes any such bigram disqualifying. In practice CJK matches
    near-identical questions and refuses the rest. That is a real gap in recall,
    not a wrong answer, which is the side of the tradeoff this module takes.
    """
    if not query_tokens:
        return 0.0
    asked = set(query_tokens)
    known = set(_ask_tokens(nugget.get("question") or ""))
    if not known:
        return 0.0
    if asked - known:
        # Rule 1. Deliberately absolute: near-identical questions that differ by
        # one content word are exactly the case worth refusing, and they are the
        # case a threshold is worst at catching.
        return 0.0
    matched = len(asked & known)
    precision = matched / len(known)
    recall = matched / len(asked)
    return 2 * precision * recall / (precision + recall)


#: Below this confidence, a match is too weak to answer with — the corpus logs
#: a gap and says "I don't know yet" instead. Compared against `_confidence`,
#: not `_score`: ranking and answering are different questions.
MIN_ASK_SCORE = 0.5


def _ranked(query: str, limit: int) -> list[tuple[dict[str, Any], float]]:
    tokens = _tokens(query)
    if not tokens:
        return []
    scored = [(n, _score(n, tokens)) for n in _all(NUGGETS_COLLECTION)]
    scored = [(n, s) for n, s in scored if s > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: max(0, limit)]


def search_nuggets(query: str, limit: int = 8) -> list[dict[str, Any]]:
    """Ranked nugget search. Pure lookup — never logs a gap on a miss.

    Question-token overlap is weighted highest so a near-exact question
    match outranks a nugget that merely mentions the query in its answer.
    """
    return [n for n, _ in _ranked(query, limit)]


def ask_corpus(question: str, include_asserted: bool = False) -> dict[str, Any]:
    """The spec's interaction flow: exact match, else best partial match,
    else 'I don't know yet' — which logs the gap for later triage.

    Unlike search_nuggets(), this is the deliberate "ask the corpus"
    entrypoint (used by corpus_server's corpus_ask tool and Jeles'
    synthesize step), so a miss — or a match too weak to trust — is
    assumed to be a real gap worth tracking, not background search noise.

    Asserted nuggets — written over a tool call, checked by nobody — do not
    answer here. ``found: true`` from this function is the settled layer
    speaking, and a caller that reads only ``nugget["answer"]`` (most of them)
    would have no way to tell otherwise. They still come back among
    ``candidates``, and ``search_nuggets``/``get_nugget`` still return them, so
    an assertion is reachable without being authoritative. Pass
    ``include_asserted=True`` to opt into answering from them.
    """
    tokens = _tokens(question)
    # Ranking and deciding use different token sets on purpose: `_ranked` orders
    # candidates with `_tokens`, `_confidence` decides with `_ask_tokens`, which
    # also carries the short words that change an answer. See `_ask_tokens`.
    ask_tokens = _ask_tokens(question)
    ranked = _ranked(question, 5)

    # Rank by `_score`, but decide by `_confidence`. The best candidate is not
    # automatically an answer, and conflating the two is what let a nugget about
    # staging answer a question about production. Confidence is checked across
    # the candidates rather than only the top-ranked one, so a nugget that
    # genuinely answers the question is not lost to a higher-ranked near-miss.
    confident = [
        (n, c)
        for n, c in ((n, _confidence(n, ask_tokens)) for n, _ in ranked)
        if c >= MIN_ASK_SCORE and (include_asserted or _kind_of(n) != "asserted")
    ]
    if not confident:
        log_gap(question)
        # The candidates still come back: "I don't know yet, but these are
        # close" is more useful than a bare miss, and it is the caller's cue to
        # look at the second hop rather than to trust one of these.
        return {"found": False, "nugget": None, "candidates": [n for n, _ in ranked]}

    confident.sort(key=lambda pair: pair[1], reverse=True)
    top = confident[0][0]
    top_tokens = set(_tokens(top.get("question") or ""))
    exact = bool(tokens) and set(tokens) == top_tokens
    others = [n for n, _ in ranked if n is not top]
    return {"found": True, "exact": exact, "nugget": top, "candidates": others}


def to_search_hit(nugget: dict[str, Any], idx: int = 0) -> dict[str, Any]:
    """Shape a nugget as a search hit compatible with a host search
    pipeline's flatten_results()/rank_hit() (source_id="corpus")."""
    sources = nugget.get("sources") or []
    # A machine-corroborated nugget must not read as human-verified: surface the
    # kind, and downgrade its confidence label so the two are distinguishable on
    # read (absent kind => legacy human nugget).
    kind = _kind_of(nugget)
    confidence = {"human": "verified", "machine": "corroborated", "asserted": "unverified"}[kind]
    # `source` is the line a reader actually sees, so it cannot say "Verified
    # corpus" over an assertion nobody checked — and it shows `written_by`, the
    # app that made the write, rather than `verified_by`, which is only ever
    # whatever string that app chose to type.
    if kind == "asserted":
        who = nugget.get("written_by") or nugget.get("verified_by") or "unknown"
        source = f"Corpus (asserted, unchecked) — {who}"
    else:
        source = f"Verified corpus — {nugget.get('verified_by') or 'unknown'}"
    return {
        "title": nugget.get("question") or "Verified nugget",
        "url": sources[0] if sources else "",
        "snippet": nugget.get("answer") or "",
        "source": source,
        "date": nugget.get("verified_at") or "",
        "source_id": "corpus",
        "hostname": "corpus.local",
        "confidence": confidence,
        "verification_kind": kind,
        "nugget_id": nugget.get("_id") or "",
        "verified_by": nugget.get("verified_by") or "",
        "verified_at": nugget.get("verified_at") or "",
        "extra_sources": sources,
        "tags": nugget.get("tags") or [],
        # Carried, never interpreted — see the comment above `_KIND_RANK`. A
        # nugget written before this field existed, or with none supplied,
        # renders {} here: identical to today's shape for every existing hit.
        "evidence": nugget.get("evidence") or {},
        "n": idx,
    }


# ── Gaps ("I don't know yet") ───────────────────────────────────────────


#: How many alternative phrasings of one gap to keep. A gap record is a queue
#: item, not an audit log: enough phrasings to see the shape of the demand,
#: bounded so a gap asked ten thousand times cannot grow its row without limit.
_MAX_GAP_VARIANTS = 8


def _gap_key(question: str) -> str:
    """The identity of a gap: content tokens, their adjacency, and short codes.

    Rephrasings must merge — that is the feature this key exists for. The old
    key was `sorted(set(_tokens(question)))`, and sorting is what made
    rephrasings merge *and* what merged questions whose meaning is their word
    order:

        "how do I migrate from postgres to mysql?" -> d2ceaf6ce807, count 1
        "how do I migrate from mysql to postgres?" -> d2ceaf6ce807, count 2

    One gap for two opposite migrations, its stored `question` overwritten by
    whichever was asked last.

    So the key carries three segments:

    * the token set — unchanged, this is what merges rephrasings;
    * the set of adjacent token pairs, which is what "postgres to mysql" and
      "mysql to postgres" differ in (`{migrate>postgres, postgres>mysql}` vs
      `{migrate>mysql, mysql>postgres}`) while "Does drug A interact with X?"
      and "With X, does drug A interact?" do not (`{drug>interact}` for both —
      moving a whole phrase leaves the content tokens adjacent in the same
      order);
    * short codes, which `_tokens` drops, so "drug A" and "drug B" stay apart.

    Adjacency rather than full order is a deliberate choice between two ways to
    be wrong. Keying on the full token order would split every reordering,
    including the rephrasings; keying on the set alone destroys a question.
    Adjacency splits *some* genuine rephrasings — "the accent color in Nord"
    and "the Nord accent color" now produce two gap rows — and that is the
    error worth having, because a duplicate row is visible and mergeable while
    an overwritten question is gone. `log_gap` also stops overwriting
    `question`, so the phrasings survive whatever this key still merges.

    Known limit: the short-code segment is an unordered set, so a question whose
    direction is carried entirely by short codes still merges — "migrate from v1
    to v2" and "migrate from v2 to v1" share the one content token `migrate`
    (no pairs) and the codes {v1, v2}. Both phrasings are kept in `variants`;
    the count is still shared.

    Changing the key changes every gap id, so gap rows written by the old key
    will never be hit again: they stay in `list_gaps` with their historical
    counts and the next ask of the same question opens a fresh row at 1. No
    backfill is written, because this package has never been published (see
    pyproject's [tool.hatch.version] note — no tags, no release), so the only
    stores affected are development ones.
    """
    tokens = _tokens(question)
    unique = sorted(set(tokens))
    pairs = sorted({f"{a}>{b}" for a, b in pairwise(tokens)})
    short = sorted(
        {t for t in _SHORT_RE.findall(question.lower()) if len(t) < 3 and t not in _STOP}
    )
    if not (unique or short):
        # Nothing tokenized at all (punctuation, emoji): fall back to the text.
        return question.lower()
    return "\n".join(("|".join(unique), "|".join(pairs), "|".join(short)))


def log_gap(question: str) -> dict[str, Any]:
    """Log an unanswered question. Repeated asks bump asked_count instead of
    creating duplicates, keyed by :func:`_gap_key` — the question's content
    tokens, the adjacent pairs among them, and its short codes.

    The record keeps the *first* phrasing seen as `question`; later phrasings
    that land on the same key are appended to `variants` (up to
    ``_MAX_GAP_VARIANTS``) rather than replacing it. Overwriting it erased the
    earlier question outright, so someone working the queue answered the
    surviving phrasing and the other was gone, still unanswered, its count
    folded into a number that then overstated demand for the one left.

    A new or re-asked gap is written ``status: "open"``. Earlier versions wrote
    ``"unverified"`` here, which read as a rung on the confidence ladder
    (``verified``/``corroborated``/``institutional``/``unverified``) when it is
    nothing of the kind — a gap has no answer to be confident about. Nothing
    read the field either way, so no consumer changes; see :func:`resolve_gap`,
    which is what finally gives it a second value to hold.
    """
    question = (question or "").strip()
    if not question:
        return {"error": "question required"}
    gap_id = uuid.uuid5(uuid.NAMESPACE_URL, _gap_key(question)).hex[:12]
    now = _now()

    # The read and the write are one transaction. Split across two, as they were,
    # concurrent asks all read the same count and all write count+1: measured at
    # 14 after 50 concurrent calls. `list_gaps` sorts by asked_count, so the
    # corpus's growth queue was ordered by a number that quietly undercounted
    # exactly the questions being asked most.
    with _lock:
        conn = _conn(GAPS_COLLECTION)
        with _write(conn):
            row = conn.execute(
                "SELECT data FROM records WHERE id = ? AND deleted = 0", (gap_id,)
            ).fetchone()
            existing = json.loads(row[0]) if row else {}
            first = existing.get("question") or question
            variants = list(existing.get("variants") or [])
            if question != first and question not in variants:
                variants = [*variants[: _MAX_GAP_VARIANTS - 1], question]
            fields = {
                "question": first,
                "status": "open",
                "asked_count": int(existing.get("asked_count", 0)) + 1,
                "first_asked_at": existing.get("first_asked_at") or now,
                "last_asked_at": now,
            }
            # A resolved gap that misses again is open again — the miss is the
            # proof that the settled layer stopped covering it. The resolution
            # is kept rather than dropped, so the record still shows it was
            # once answered and `reopened_at` says when that stopped holding;
            # deleting it would make a recurring hole look like a brand new one.
            if existing.get("status") == "resolved":
                fields["resolved_at"] = existing.get("resolved_at") or ""
                fields["resolved_by"] = existing.get("resolved_by") or ""
                fields["reopened_at"] = now
            record = _clean(fields)
            if variants:
                record["variants"] = _clean(variants)
            conn.execute(
                "INSERT INTO records (id, data, created_at, updated_at, deleted) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(id) DO UPDATE SET "
                "data = excluded.data, updated_at = excluded.updated_at",
                (gap_id, json.dumps(record), now, now),
            )
    return {"id": gap_id, "asked_count": record["asked_count"]}


def resolve_gap(
    gap_id: str,
    resolved_by: str = "",
    nugget_id: str = "",
) -> dict[str, Any]:
    """Close a gap: mark it answered so it leaves the growth queue.

    Returns ``{id, status, resolved_at}``, or ``{"error": "not_found"}`` for an
    id no gap has — the same shape :func:`get_nugget` uses for a miss.

    The record is marked, never deleted. What was asked, how often, and in how
    many phrasings is the corpus's own history of its blind spots, and it is
    worth more once the hole is filled than while it is open. ``resolved_by``
    and ``nugget_id`` are recorded when given, so a reader can tell *who* said
    this was answered and *what* answers it.

    This closes a queue, it does not verify anything. Marking a gap resolved
    makes no claim that the nugget answering it is true, and cannot promote
    anything up the confidence ladder — the rung is decided by
    :func:`put_nugget` and, for the ``human`` one, by a signature
    (`jeles._nestor_seal`). A caller that resolves a gap against a worthless
    nugget has hidden a question, not answered it.
    """
    gap_id = (gap_id or "").strip()
    if not gap_id:
        return {"error": "gap_id required"}
    now = _now()
    with _lock:
        conn = _conn(GAPS_COLLECTION)
        with _write(conn):
            row = conn.execute(
                "SELECT data FROM records WHERE id = ? AND deleted = 0", (gap_id,)
            ).fetchone()
            if row is None:
                return {"error": "not_found"}
            record = json.loads(row[0])
            record["status"] = "resolved"
            record["resolved_at"] = now
            if resolved_by:
                record["resolved_by"] = resolved_by
            if nugget_id:
                record["nugget_id"] = nugget_id
            # A gap resolved after having been reopened is resolved *now*;
            # leaving the old reopening timestamp behind would date the record
            # to the last time it was open.
            record.pop("reopened_at", None)
            conn.execute(
                "UPDATE records SET data = ?, updated_at = ? WHERE id = ?",
                (json.dumps(record), now, gap_id),
            )
    return {"id": gap_id, "status": "resolved", "resolved_at": now}


def list_gaps(limit: int = 50, include_resolved: bool = False) -> list[dict[str, Any]]:
    """Gaps, most-asked first — the corpus's growth queue.

    Resolved gaps are left out unless asked for. They stay in the store, but
    this list is sorted by ``asked_count``, and a resolved gap's count is
    frozen at whatever it reached: once answered, :func:`ask_corpus` hits the
    nugget and stops logging. Kept in the list it would sit near the top on a
    historical number forever, outranking every newer open gap — the queue
    getting less useful precisely as the corpus got better.

    Anything whose ``status`` is not ``"resolved"`` counts as open, which
    includes the ``"unverified"`` written by every version of :func:`log_gap`
    from before a gap could be closed at all.
    """
    gaps = _all(GAPS_COLLECTION)
    if not include_resolved:
        gaps = [g for g in gaps if g.get("status") != "resolved"]
    gaps.sort(key=lambda g: g.get("asked_count", 0), reverse=True)
    return gaps[: max(0, limit)]
