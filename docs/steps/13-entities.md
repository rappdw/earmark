# Step 13: Entity registry and linking

Plan written 2026-11-20 against the code at `9c080e6` (steps 01–12 done).

## 1. Outcome

Recurring people, papers, companies, tools, and topics get one note each, under
`<VAULT_SUBDIR>/Entities/<Kind>/<Name>.md`, and accumulate across episodes. Each entity
note lists every episode that mentions it, with the episode date, a wikilink to the episode
note, the show, the context, and the timestamps. Every entity line in an episode note's
analysis sections is a wikilink to its entity note. An optional, user-edited
`<VAULT_SUBDIR>/aliases.yaml` merges names into one entity and can pin display names.

The registry is a set of SQLite tables in `earmark.db` (migration `0002_entities`):
`episodes`, `mentions`, `aliases`, `entities`. They are derived data. `earmark entities
rebuild` empties them and rebuilds them from `episodes/*/analyses/*.json` (plus
`aliases.yaml` and the vault scan), then regenerates every earmark note, and on an intact
vault that writes nothing.

Every store regenerates links from the real paths on disk, so a link resolves even after
the user renames or moves an entity note or an episode note. A store also updates the notes
of other episodes whose links went stale. Obsidian rewrites links when a note is renamed,
and that rewrite is not treated as a hand edit.

The step also fixes the lone-surrogate bug reported by the step-12 verifier, at its real
source (section 2.1). Lone UTF-16 surrogates are replaced with U+FFFD when any `earmark`
model is constructed, in the LLM answer before parsing, in job error text, and again at the
vault render boundary.

One new runtime dependency: PyYAML, already in the lock (2.4). No new setting.

## 2. Findings

### 2.1 Lone surrogates: where they really break

Checked in the sandbox (pydantic 2.14, Python 3.13.5):

- `json.loads('"\\ud800"')` returns a `str` holding a lone surrogate. The stdlib decoders
  behind the iTunes lookup, the Apple page JSON, the remote ASR response, the OpenAI-compatible
  response, and the Anthropic SDK can all hand one to earmark.
- Plain `str` model fields **accept** it: `Segment(text="a\ud800")` and
  `Episode(title="T\ud800", ...)` construct without error, and so does
  `AnalysisOutput.model_validate(json.loads(...))`.
- Constrained `str` fields **reject** it: `Entity(name="a\ud800b")` (`min_length=1`) raises a
  `ValidationError` of type `string_unicode`.
- `model_dump_json()` of a model holding one raises `PydanticSerializationError`
  (UnicodeEncodeError inside). Both errors are `ValueError`s.
- `AnalysisOutput.model_validate_json(text)`, where `text` is a `str` that holds a raw lone
  surrogate, raises `ValidationError` (invalid JSON). So does a JSON escape `\ud800` in the
  text (`lone leading surrogate in hex escape`).
- `sqlite3` raises `UnicodeEncodeError` when a bound parameter holds one.

So in production the bug does **not** first show up in the store, as the step-12 verifier
reported. It fails earlier, on the first artifact write: `write_episode` after resolve,
`write_transcript` after ASR (the remote engine would re-upload the audio on every retry),
and `build_analysis` / `write_analysis`. Each raises a `ValueError`, which the runner treats
as transient, so the job retries until its attempts run out. The verifier's store-level
crash is real too, but it can only be reached with models built by `model_construct` or
`model_copy(update=)`. Section 5.11 fixes both layers.

Proof that a base-class validator works (sandbox): `field_validator("*", mode="before")` on
the shared `Model` base applies to every field of every subclass. That includes constrained
fields (it runs before the `min_length` check), `str | None`, and `tuple[str, ...]` when the
helper recurses into tuples and lists. Nested models given as dicts are cleaned by their own
class. A JSON round trip is unchanged. Validating 20 000 segments took 15 ms.

### 2.2 The normaliser: reuse `entity_key`, with one fix

`analyze/merge.py::entity_key` (step 11) is NFKC, casefold, every P*/S* character to a space,
whitespace collapsed. Probed:

| Name | Key |
|---|---|
| `C++`, `C#`, `C` | all `c` |
| `F#` | `f` |
| `AT&T` | `at t` |
| `GPT-4`, `GPT 4` | both `gpt 4` |
| `Zoë ☕` | `zoë` |

Within one episode that merges C, C++, and C# into one tool, which is already a step-11 bug.
Across episodes it would make one `C` note collect every C++ and C# episode, and
`aliases.yaml` can only merge, not split. **Decision:** keep one normaliser, move it to
`models.py` (shared vocabulary, next to `episode_key` and `prompt_hash`), and refine it: a `+`
or `#` right after a letter, digit, or another kept `+`/`#` is kept. With that,
`C++` → `c++`, `C#` → `c#`, `F#` → `f#`, `#MeToo` → `metoo`, and `C++ & Rust` →
`c++ rust`. `merge.py` imports it from `models`, so map-reduce merging uses the same rule.
One step-11 assertion changes (section 8). The categories were checked: `+` is Sm; `#`,
`&`, `.`, `,`, `'`, `/`, `!`, `*` are Po; `-` is Pd; `’` is Pf.

Not changed on purpose: accents are not folded (`José` and `Jose` stay two entities;
`aliases.yaml` merges them). Titles such as `Dr.` are not stripped.

### 2.3 Obsidian links (checked at `obsidian.md/help/links` and `/help/settings`, 2026-11-20)

- `[[target|display]]` sets the display text. Characters `# | ^ : %% [[ ]]` "may not work
  as a link". Folder paths start at the vault root.
- "When enabled, Obsidian automatically updates internal links when you rename a file." The
  help pages say this only for renames and do not give the default, but they suggest the
  option is on. It writes links in the "New link format" (default "Shortest path when
  possible"), so a rename turns earmark's `[[Earmark/Entities/People/Ada Lovelace|Ada
  Lovelace]]` into something like `[[Ada King|Ada Lovelace]]`. That changes the managed
  region, and step 12 would back it up and warn. Section 5.8 makes edit detection ignore
  link targets.
- The pages do not say how Obsidian resolves `[[Name]]` when two files share a basename.
  earmark's own links are full paths and never ambiguous. A user's own short `[[Ada
  Lovelace]]` elsewhere in the vault may become ambiguous once
  `Earmark/Entities/People/Ada Lovelace.md` exists. This is **not verified** and is recorded
  as a known risk (section 9). earmark does not scan outside its subdir.

### 2.4 YAML for `aliases.yaml`

**Decision:** PyYAML becomes a runtime dependency (`pyyaml>=6.0.3`; already locked as 6.0.3,
with a `cp313 manylinux aarch64` wheel in `uv.lock`). It is imported lazily, only by
`vault/aliases.py`. earmark does **not** use `safe_load`. It calls
`yaml.compose(text, Loader=_NoAliasLoader)` and walks the node tree. Checked in the sandbox:

- `compose` builds nodes and constructs nothing, so tags such as `!!python/object:os.system`
  have no effect. Each `ScalarNode.value` is the **raw text**: `1984` stays `"1984"` (an int
  under `safe_load`), `No` stays `"No"` (a bool, the "Norway problem"). Entity names need
  exactly this.
- Every node carries `start_mark.line`, so problems are reported with line numbers.
- Duplicate mapping keys appear as separate pairs (`safe_load` silently keeps the last), so
  they can be reported.
- A subclass of `yaml.SafeLoader` that overrides `compose_node(self, parent, index)` to raise
  `yaml.composer.ComposerError` on `self.check_event(yaml.AliasEvent)` refuses `*alias` at
  its line. Anchors alone are harmless. So no alias expansion bomb is possible. The stub
  signature is `compose_node(self, parent: Node | None, index: int) -> Node | None`.
- A syntax error raises `yaml.YAMLError` with `problem` and `problem_mark.line`. A second
  document raises `ComposerError` ("but found another document"). An empty or comment-only
  file composes to `None`. `key:` with no value is a `ScalarNode` with value `''` and the
  null tag. The merge key `<<` is an ordinary scalar named `<<`.
- The composer is recursive, so deep nesting can raise `RecursionError`, which is not a
  `YAMLError`. Catch it too.

Why not a constrained parser: users edit this file by hand and expect YAML to behave like
YAML (quoting, flow lists, comments). The step-12 plan already lists a hand-rolled YAML
parser as a dead end. PyYAML is mature and already locked. It costs about 0.8 MB, and node
composition is safe. Why not `safe_load`: implicit typing corrupts names, and you lose line
numbers and duplicate detection.

### 2.5 What exists (step 12) that this step builds on

- `VaultStore.store` scans the subdir for frontmatter identities (only the current episode's
  key today), settles paths, and writes the transcript note, then the episode note, under
  `<DATA_DIR>/vault.lock`. `_write_note` does read, `update_note`, an optional backup, and
  `write_atomic(expect=stamp)` with 3 attempts. `note_link`, `file_component`, `fold_key`, the
  `child(create=True)` fold reuse, and `_free_name` collision suffixes are all reusable.
- `document.note_identity` knows only `earmark: episode|transcript` and `episode_key`.
- `render_analysis` prints entity lines as `- **{name}**…` and leaves `[[`/`]]` alone (step
  10 left linking to this step).
- `db.MIGRATIONS` holds one migration. The rules are append-only, plain `CREATE` statements,
  and `STRICT` tables. `tests/test_db.py` uses `SCHEMA_VERSION`, so adding a migration needs
  no test edits there.
- `build_vault_store` must create nothing (`test_factory.py::test_vault_store_built`), so the
  database is opened lazily, inside the lock, on the first `store`.
- `tests/test_vault_notes.py::test_vault_modules_import_light` asserts that `yaml` is not
  imported by `import earmark.vault.notes`. Keep the yaml import inside a function.

### 2.6 Dead ends

- Do not `clean_unicode` (NFC) a whole note before writing. User text outside the markers is
  decoded with `surrogateescape` and must round-trip byte for byte. Clean only generated text:
  the region and owned frontmatter.
- Do not use basename wikilinks (`[[Ada Lovelace]]`). They survive moves but break on
  renames and become ambiguous with the user's notes. Use full paths and regenerate them.
- Do not record note paths in SQLite before the writes succeed. Section 5.6 records them
  after.
- Do not store `entity_key` in frontmatter as the identity. Reading back a YAML-escaped
  string would need a YAML parser. Use the 16-hex `entity_id`.

## 3. Files

| File | Purpose |
|---|---|
| `src/earmark/models.py` | `scrub_surrogates`, `entity_key` (moved and refined, 2.2), the `Model` base validator (5.11). |
| `src/earmark/analyze/merge.py` | Import `entity_key` from `models` (remove the local definition; keep the name importable from `merge`). |
| `src/earmark/analyze/render.py` | `render_analysis(..., entity_link=None)`, `EntityLinker`. |
| `src/earmark/analyze/calls.py` | Scrub lone surrogates in `response.text` and `repaired.text` before `model_validate_json`. |
| `src/earmark/pipeline/runner.py` | `describe_error` scrubs lone surrogates. |
| `src/earmark/db.py` | Append `Migration("0002_entities", ...)` (4.4). |
| `src/earmark/vault/sanitize.py` | `escape_markdown` starts with `clean_unicode`; new `wikilink_label`. |
| `src/earmark/vault/document.py` | Entity identity; region scrubbing; link-insensitive edit detection; `content_digest`. |
| `src/earmark/vault/aliases.py` | New. Read and parse `aliases.yaml` (lazy PyYAML). |
| `src/earmark/vault/entities.py` | New. Kinds, `entity_id`, mention extraction, `EntityRegistry` (SQLite), entity resolution, entity note rendering. |
| `src/earmark/vault/notes.py` | `VaultStore` integrates the registry: full identity scan, entity notes, relinking other episodes, `rebuild()`; `note_link` sanitises labels; `render_episode_region(..., entity_link=)`. |
| `src/earmark/cli.py` | `earmark entities rebuild [--prune]`. |
| `pyproject.toml`, `uv.lock` | `pyyaml>=6.0.3` moved from the dev group to `dependencies` (`types-pyyaml` stays dev). `uv lock`. |
| `tests/test_vault_entities.py` | New. Registry, resolution, rendering, store integration, relinking, rebuild. |
| `tests/test_vault_aliases.py` | New. |
| `tests/test_models.py`, `test_analyze_merge.py`, `test_analyze_render.py`, `test_analyzer.py`, `test_remote_asr.py`, `test_runner.py`, `test_vault_document.py`, `test_vault_sanitize.py`, `test_vault_notes.py`, `test_db.py`, `test_cli.py` | Additions and the updates listed in 6. |
| `docs/ARCHITECTURE.md` | Updated in this planning step (section 8). |

No change to `fakes.py`, `factory.py`, `config.py`, or `.env.example`. `FakeStore` stays as it
is.

## 4. Interfaces

Python 3.13. The signatures are the contract; private helpers are up to the implementer.

### 4.1 `earmark.models`

```python
def scrub_surrogates(text: str) -> str:
    """Each lone surrogate (U+D800–U+DFFF) becomes U+FFFD; otherwise unchanged."""


def entity_key(name: str) -> str:
    """NFKC, casefold; '+'/'#' kept right after a letter, digit, or kept '+'/'#'; every
    other P*/S* character becomes a space; whitespace collapsed."""


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def _scrub(cls, value: object) -> object: ...  # str, and tuples/lists of them, recursively
```

`entity_key` in pseudocode. "Previous" means the previous character **written to the output**:

```python
text = unicodedata.normalize("NFKC", name).casefold()
out: list[str] = []
for c in text:
    prev = out[-1] if out else ""
    if c in "+#" and (prev.isalnum() or prev in {"+", "#"}):
        out.append(c)
    elif unicodedata.category(c)[0] in "PS":
        out.append(" ")
    else:
        out.append(c)
return " ".join("".join(out).split())
```

`_scrub` uses a precompiled `re.compile("[\ud800-\udfff]")` and returns the input object
unchanged when it holds none. That matters for speed and keeps identity for nested model
instances.

### 4.2 `earmark.analyze.render`

```python
type EntityLinker = Callable[[str, Entity], str | None]  # (field, entity) -> name markup


def render_analysis(
    analysis: Analysis, *, heading_level: int = 2, entity_link: EntityLinker | None = None
) -> str: ...
```

`field` is the `Analysis` field name (`"people"`, `"papers"`, `"companies"`, `"tools"`,
`"topics"`). If `entity_link` returns a string, the entity line is `f"- **{markup}**"`
followed by the unchanged context and timestamps. With `None` (or no linker) the line is
exactly as before. Without a linker the output is byte-identical to step 10, so `earmark
analyze` and its tests do not change.

### 4.3 `earmark.vault.sanitize` and `earmark.vault.document`

```python
def escape_markdown(text: str) -> str: ...  # now clean_unicode(text) first, then as before
def wikilink_label(text: str) -> str: ...
```

`wikilink_label`: `clean_unicode`, collapse whitespace (`str.split`), then `[` → `(`, `]` →
`)`, `|` → `-`, `\` → `/`, `<` → `&lt;`, and runs of `%%`… → `%`. It never returns text
containing `]]`, `|`, or a newline.

`document` changes:

```python
type NoteKind = Literal["episode", "transcript", "entity"]


def note_identity(text: str) -> tuple[NoteKind, str] | None: ...
def content_digest(owned: Sequence[tuple[str, YamlValue]], region: str) -> str: ...
def unlinked(region: str) -> str: ...
```

- `note_identity`: from the frontmatter, the first top-level `earmark:` line whose unquoted
  value is `episode`, `transcript`, or `entity`. For `episode`/`transcript` it then needs the
  first `episode_key:` matching `[0-9a-f]{20}` (as before). For `entity` it needs the first
  `entity_id:` matching `[0-9a-f]{16}`. Otherwise it returns `None`.
- `new_note` and `update_note` first apply `scrub_surrogates` to `region`. That is the
  render-boundary guard, and it is idempotent for clean input. Owned values already go
  through `yaml_quote`, which cleans.
- `content_digest(owned, region) = region_digest("".join(emit_frontmatter(owned)) +
  scrub_surrogates(region))`.
- `unlinked(region)`: CRLF → LF, then every wikilink `\[\[([^\[\]|\n]*)(\|[^\[\]\n]*)?\]\]`
  becomes `"[[" + (group 2 or "") + "]]"`, and every Markdown link destination
  `\]\(([^()\s]*)\)` becomes `"]()"`.
- `update_note`'s `edited` becomes `(stamped is None or stamped != old_digest) and
  unlinked(old_region) != unlinked(region)`.

### 4.4 Migration `0002_entities` (`db.py`)

Appended to `MIGRATIONS`. One `execute` per statement:

```sql
CREATE TABLE episodes (
    episode_key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    show_title TEXT NOT NULL,
    published_at TEXT,
    first_shared_at TEXT NOT NULL,
    analyses TEXT NOT NULL CHECK (json_valid(analyses)),
    note_path TEXT,
    transcript_note_path TEXT,
    links_digest TEXT
) STRICT
```

```sql
CREATE TABLE mentions (
    episode_key TEXT NOT NULL REFERENCES episodes (episode_key) ON DELETE CASCADE,
    prompt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('person', 'paper', 'company', 'tool', 'topic')),
    name_key TEXT NOT NULL CHECK (name_key <> ''),
    position INTEGER NOT NULL CHECK (position >= 0),
    name TEXT NOT NULL,
    context TEXT NOT NULL,
    timestamps TEXT NOT NULL CHECK (json_valid(timestamps)),
    PRIMARY KEY (episode_key, prompt_hash, kind, name_key)
) STRICT
```

```sql
CREATE INDEX mentions_by_name ON mentions (kind, name_key)
```

```sql
CREATE TABLE aliases (
    kind TEXT NOT NULL CHECK (kind IN ('person', 'paper', 'company', 'tool', 'topic')),
    alias_key TEXT NOT NULL CHECK (alias_key <> ''),
    canonical_key TEXT NOT NULL CHECK (canonical_key <> ''),
    canonical_name TEXT NOT NULL,
    PRIMARY KEY (kind, alias_key)
) STRICT
```

```sql
CREATE TABLE entities (
    entity_id TEXT PRIMARY KEY,
    kind TEXT CHECK (kind IS NULL OR kind IN ('person', 'paper', 'company', 'tool', 'topic')),
    entity_key TEXT,
    name TEXT,
    note_path TEXT NOT NULL,
    content_digest TEXT NOT NULL
) STRICT
```

What the columns hold:

- Times are `jobs.py`-style fixed-width UTC ISO strings. Reuse `_to_db`/`_from_db`, moved or
  duplicated as the implementer prefers.
- `analyses` is a JSON array of the prompt hashes the episode notes were last rendered with,
  ordered by `(created_at, prompt_hash)`.
- `note_path` and `transcript_note_path` are vault-relative paths (like `StoreResult`), or
  NULL when the note was never written. `links_digest` is NULL until the notes are written.
- `timestamps` is a JSON array of floats, sorted and unique.
- `entities` holds one row per entity note earmark manages: every resolved entity, plus every
  orphan entity note found by the scan. `kind`, `entity_key`, and `name` are NULL for an
  orphan whose key cannot be derived (5.5).

### 4.5 `earmark.vault.aliases`

```python
ALIASES_FILE: Final = "aliases.yaml"
MAX_ALIASES_BYTES: Final = 256 * 1024
MAX_ALIAS_NAMES: Final = 10_000
MAX_REPORTED: Final = 5


@dataclass(frozen=True, slots=True)
class AliasEntry:
    kind: EntityKind
    alias_key: str  # entity_key of a listed name; equals canonical_key for the entry itself
    canonical_key: str
    canonical_name: str  # whitespace-collapsed, as written


@dataclass(frozen=True, slots=True)
class AliasFile:
    entries: tuple[AliasEntry, ...]  # file order
    problems: tuple[str, ...]  # entry-level, already formatted, at most MAX_REPORTED + 1


class AliasFileError(Exception):
    message: str  # whole-file problem, safe to show


def parse_aliases(text: str) -> AliasFile: ...  # raises AliasFileError
def read_aliases(root: VaultDir) -> AliasFile | None: ...  # None: no file; raises AliasFileError
```

`EntityKind` is imported from `entities.py`. To avoid an import cycle, define `EntityKind`
and the kind table in `entities.py`, and have `aliases.py` import from it. `entities.py`
must not import `aliases.py` at module top: it only needs `AliasEntry`, imported inside
functions or under `TYPE_CHECKING`. Alternatively the implementer may put the kind table in
a tiny `vault/kinds.py`. Either is fine as long as `yaml` stays lazy.

### 4.6 `earmark.vault.entities`

```python
type EntityKind = Literal["person", "paper", "company", "tool", "topic"]


@dataclass(frozen=True, slots=True)
class KindInfo:
    field: str  # Analysis field
    kind: EntityKind
    folder: str  # directory under Entities/
    label: str  # shown in the note


KINDS: Final[tuple[KindInfo, ...]] = (
    KindInfo("people", "person", "People", "Person"),
    KindInfo("papers", "paper", "Papers", "Paper"),
    KindInfo("companies", "company", "Companies", "Company"),
    KindInfo("tools", "tool", "Tools", "Tool"),
    KindInfo("topics", "topic", "Topics", "Topic"),
)
ENTITIES_DIR: Final = "Entities"
NAME_FALLBACK: Final = "Unnamed"


def entity_id(kind: EntityKind, key: str) -> str: ...  # sha256(f"{kind}:{key}")[:16]


@dataclass(frozen=True, slots=True)
class Mention:
    episode_key: str
    prompt_hash: str
    created_at: datetime
    kind: EntityKind
    name_key: str  # entity_key(name), never ""
    position: int  # index in the analysis's list for that kind
    name: str
    context: str
    timestamps_s: tuple[float, ...]


def mentions_from(analyses: Sequence[Analysis]) -> list[Mention]: ...


@dataclass(frozen=True, slots=True)
class EpisodeRow:
    key: str
    title: str
    show_title: str
    published_at: datetime | None
    first_shared_at: datetime
    analyses: tuple[str, ...]
    note_path: str | None
    transcript_note_path: str | None
    links_digest: str | None


@dataclass(frozen=True, slots=True)
class EntityRow:
    entity_id: str
    kind: EntityKind | None
    entity_key: str | None
    name: str | None
    note_path: str
    content_digest: str


@dataclass(frozen=True, slots=True)
class RegistryState:
    episodes: Mapping[str, EpisodeRow]
    mentions: tuple[Mention, ...]
    aliases: tuple[AliasEntry, ...]
    entities: Mapping[str, EntityRow]


class EntityRegistry:
    def __init__(self, db: Database) -> None: ...
    def record_episode(
        self,
        episode: Episode,
        analyses: Sequence[Analysis],
        first_shared_at: datetime,
        aliases: Sequence[AliasEntry] | None,  # None: keep the stored aliases
    ) -> None: ...
    def reset(
        self,
        episodes: Sequence[tuple[Episode, Sequence[Analysis], datetime]],
        aliases: Sequence[AliasEntry],
    ) -> None: ...
    def state(self) -> RegistryState: ...
    def record_notes(
        self,
        episodes: Mapping[str, tuple[str | None, str | None, str]],  # key -> paths, digest
        entities: Sequence[EntityRow],  # replaces the whole table
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EpisodeMention:
    episode_key: str
    context: str
    timestamps_s: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class EntityView:
    entity_id: str
    kind: EntityKind
    key: str  # canonical key
    name: str  # display name
    other_names: tuple[str, ...]
    episodes: tuple[EpisodeMention, ...]  # one per mentioning episode, any order
    first_seen: tuple[str, str, str, str, int]  # sort key of the earliest mention


def canonical(aliases: Sequence[AliasEntry]) -> Mapping[tuple[EntityKind, str], AliasEntry]: ...
def resolve_entities(state: RegistryState) -> dict[str, EntityView]: ...  # by entity_id


@dataclass(frozen=True, slots=True)
class EpisodeLink:
    path: str  # vault-relative path of the episode note
    title: str
    show_title: str
    day: date


def entity_fields(view: EntityView) -> list[tuple[str, YamlValue]]: ...
def render_entity_region(
    view: EntityView, links: Mapping[str, EpisodeLink], *, note_link: Callable[[str, str], str]
) -> str: ...
def render_orphan_region(
    stem: str, merged_into: tuple[str, str] | None, *, note_link: Callable[[str, str], str]
) -> str: ...
```

`note_link` is passed in (it lives in `notes.py`) to avoid an import cycle. The implementer
may instead move `note_link` into `entities.py` or `sanitize.py` and import it from
`notes.py`.

`record_episode`, `reset`, and `record_notes` each run in one `db.transaction()` (`BEGIN
IMMEDIATE`). `state()` is one read connection.

### 4.7 `earmark.vault.notes`

```python
RENDER_VERSION: Final = 1  # bump when note rendering changes; part of both digests


class VaultStore:
    def __init__(
        self,
        vault_dir: Path,
        subdir: str,
        *,
        data_dir: Path,
        tz: tzinfo | None = None,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        now: Callable[[], float] = time.time,
        artifacts: ArtifactStore | None = None,  # default ArtifactStore(data_dir)
    ) -> None: ...

    def store(self, episode, transcript, analyses, shared_at, /) -> StoreResult: ...
    def rebuild(self, *, prune: bool = False) -> RebuildReport: ...


@dataclass(frozen=True, slots=True)
class RebuildReport:
    episodes: int  # episodes in the rebuilt registry
    skipped: int  # episode directories without episode.json, transcript, share, or analysis
    entities: int  # entity notes managed (resolved + orphans)
    written: int  # notes created or changed
    warnings: tuple[str, ...]


class RebuildRefused(PermanentError): ...


def note_link(vault_rel_path: str, label: str) -> str: ...  # label now sanitised here
def render_episode_region(
    episode, transcript, analyses, transcript_link, tz, *, entity_link: EntityLinker | None = None
) -> str: ...
```

`note_link`: when `wikilink_target_ok(target)`, it returns `f"[[{target}|{wikilink_label(label)}]]"`.
Otherwise it returns `f"[{escape_markdown(label)}]({quote(path, safe='/')})"`. The fixed
labels `Transcript` and `Episode note` come out unchanged.

### 4.8 CLI

```
earmark entities rebuild [--prune]
```

It loads the settings (exit 2 on `ConfigError`) and builds the store with
`build_vault_store(settings)` whatever `STORE_BACKEND` is set to (exit 2 when `VAULT_DIR` is
unset or missing, with the builder's message). Then it calls `store.rebuild(prune=...)`:

- Success: each warning goes to stderr as `earmark entities rebuild: warning: <w>`. stdout
  gets one line, `rebuilt the entity registry: {episodes} episodes, {entities} entities;
  {written} notes written` (plus `; skipped {skipped} incomplete episodes` when non-zero).
  Exit 0.
- `AliasFileError`: `earmark entities rebuild: <message>`, exit 1. Nothing is changed.
- `RebuildRefused` and other `PermanentError`: message, exit 1.
- `TransientError`: message plus ` (temporary; try again later)`, exit 1.
- `DatabaseSetupError`, `sqlite3.Error`, `OSError`: `cannot open the entity registry: …`,
  exit 1.

All text goes through `_clean`. It is a sub-app: `entities_app = typer.Typer(no_args_is_help=True,
help="The entity registry.")`, then `app.add_typer(entities_app, name="entities")`.

## 5. Behaviour and edge cases

### 5.1 Mentions from an analysis (`mentions_from`)

For each analysis, and each `KindInfo` in order, enumerate the entities of that field with
their positions. Then:

- Skip an entity whose `entity_key(name) == ""` (for example `"???"`). It is still rendered
  in the episode note, unlinked.
- Several entities in one list with the same key (one-shot analyses do not merge) become
  one mention, following `merge.py`'s semantics: the first name and position, the first
  non-blank context, and the sorted union of the timestamps.
- `name` and `context` are whitespace-collapsed (they already are, via `build_analysis`).

### 5.2 Identity and aliases

- An entity is `(kind, canonical_key)`. Kinds are never merged with each other: a person and
  a topic named "Ada Lovelace" are two notes.
- `canonical_key = aliases[(kind, name_key)].canonical_key` when that alias row exists, else
  `name_key`. `entity_id = sha256(f"{kind}:{canonical_key}")[:16]`. Golden values:
  `entity_id("person", "ada lovelace") == "82a355902603f38e"`, `entity_id("person", "zoë")
  == "ebd1f8ca95b62ffe"`, `entity_id("tool", "c++") == "bcbf1db2e4124478"`.
- **Display name:** the `canonical_name` of the alias entry whose `alias_key ==
  canonical_key` (the entry's own row), if there is one. Otherwise the `name` of the earliest
  mention by `(episode.first_shared_at, episode_key, mention.created_at, prompt_hash,
  position)`. `first_seen` is that tuple, with times as ISO strings.
- `other_names`: the distinct mention names other than the display name, sorted by
  `(fold_key(n), n)`.
- An entity is "resolved" when at least one mention maps to it. Only resolved entities get
  links and notes. Orphans are 5.5.

### 5.3 `aliases.yaml`

Location: `<VAULT_DIR>/<VAULT_SUBDIR>/aliases.yaml`, read with `root.read(ALIASES_FILE,
max_bytes=MAX_ALIASES_BYTES)`. earmark never creates or writes it. Format:

```yaml
# Under each kind, the key is the name to show and the list holds other names for the
# same entity. Names match case- and punctuation-insensitively.
people:
  Ada Lovelace:
    - Ada King
    - Augusta Ada King
  "Dr. Mara Ellison": [Mara Ellison]
companies:
  Meta: [Facebook, Meta Platforms]
topics:
  Large language models: LLMs
  AI:
```

**Whole-file problems** raise `AliasFileError`. The message is one line from this list:

- `aliases.yaml is a symlink or not a regular file` (`SymlinkRefused` from `read`)
- `aliases.yaml is larger than 256 KiB` (the `PermanentError` from `read`)
- `aliases.yaml is not UTF-8 text` (strict decode; a leading BOM is allowed)
- `aliases.yaml is not valid YAML at line {n}: {problem[:100]}`
- `aliases.yaml uses a YAML alias (*name) at line {n}; aliases are not supported`
- `aliases.yaml holds more than one document`
- `aliases.yaml nests too deeply` (`RecursionError`)
- `aliases.yaml must be a mapping of kinds (people, papers, companies, tools, topics)`
  when the root is not a mapping. `None` (an empty file) is valid and has no entries.

**Entry-level problems** skip that entry or name and are reported as `aliases.yaml line {n}:
…`:

- an unknown kind key (`use people, papers, companies, tools, or topics`);
- a kind whose value is not a mapping (a null value is fine and means no entries);
- a non-scalar name;
- a name whose `entity_key` is empty (`has no letters or digits`);
- a name already listed in this kind, as a canonical name or as an alias (`{name} is
  already listed at line {m}`). The first listing wins: later appearances are skipped, and
  for a canonical name its whole entry is skipped;
- aliases that are neither a scalar nor a sequence of scalars.

Other rules:

- A scalar value is a single alias. An empty or null-tagged scalar means no aliases (the
  entry then only pins the display name). An alias whose key equals its canonical key is
  silently dropped.
- Names are `scrub_surrogates` + whitespace-collapsed raw scalar text, and tags are ignored.
- Every name used beyond `MAX_ALIAS_NAMES` is ignored with one problem.
- User text quoted in problems is collapsed and capped at 60 characters (`…`).
- At most `MAX_REPORTED` problems are kept, then `aliases.yaml: and {k} more problems`.

Chains are not followed: under `A: [B]` and then `B: [C]`, the `B` entry is a duplicate and
is skipped, so C stays separate. The problem text says so.

In `store`, whole-file and entry problems become job warnings. On a whole-file problem the
store warns `{message}; the aliases loaded before are kept` and passes `aliases=None` to
`record_episode`. A missing file means no aliases (an empty tuple, so removing the file
removes the merges). In `rebuild`, a whole-file problem aborts before anything changes, and
entry problems become report warnings.

### 5.4 Entity notes

Path: `<subdir>/Entities/<folder>/<stem>.md`. The directories are opened with
`child(create=True)` (fold reuse).

- **Finding.** A resolved entity's note is the scan path for `("entity", entity_id)`. With
  duplicates, use step 12's `_pick` rule, with the candidate = the allocated name and the
  warning `found {n} notes for entity {entity_id}; updated {path}`. A scanned note is
  re-verified under the lock, as step 12 does (`_verify`).
- **Allocation** of new notes, in the order `(first_seen, kind index, key)`: `stem =
  file_component(name, max_bytes=TITLE_BYTES, fallback=NAME_FALLBACK)`. The name is free when
  no directory entry **and no name allocated earlier in this run** has the same fold key.
  Otherwise try `f"{stem} ({id[:8]})"`, then `f"{stem} ({id})"`, else `PermanentError("no
  free note name for entity {id} in {rel dir}")`. Reuse `_free_name`, extended with a
  `taken` set.
- **Collisions with user notes** inside `Entities/<folder>/`, of any kind (a file, folder,
  or symlink with the same fold key), get the suffix. The user's file is never touched.
  Names elsewhere in the vault are not checked (2.3).
- **Owned frontmatter**, in order: `earmark: "entity"`, `entity_id`, `kind`, `name` (the
  display name). The seeded key is `tags = ("earmark", "entity")`.
- **Region**, blocks joined by `"\n\n"` and ending with `"\n"` (`E` = `escape_markdown`):
  1. `f"# {E(name)}"`
  2. `f"**{label}** · mentioned in {n} {'episode' if n == 1 else 'episodes'}"`, where `n`
     counts the listed episodes.
  3. Only if `other_names` is non-empty: `"Also mentioned as: " + " · ".join(E(x) for x in
     other_names)`.
  4. `"## Episodes"`, then one line per listed episode, joined by `"\n"` in the order
     `(day desc, first_shared_at desc, episode_key)`:
     `f"- {day.isoformat()} · {note_link(path, title or 'Untitled')} · {E(show)}"`, plus
     `f" — {E(context)}"` when the context is non-empty, plus `" " + " ".join(f"[{hms}]"
     ...)` with timestamps formatted by `format_hms` and deduplicated after formatting.
     With no listed episode, the block is `"*No episode note mentions this entity.*"`
     instead of the heading and lines.
- **A listed episode** is a registry episode with at least one mention mapping to this
  entity whose episode note exists: found by the scan, or settled and written in this run.
  `day` = the published date in `tz`, else the first-share date in `tz` (same as the note
  file name). The context is the one from the newest analysis `(created_at, prompt_hash)`
  of that episode with a non-blank context. The timestamps are the union over all its
  analyses.

Example: the factories' episode, a default analysis, `tz=UTC`, so the entity "Zoë" as a
person:

```
# Zoë

**Person** · mentioned in 1 episode

## Episodes

- 2026-01-02 · [[Earmark/Podcasts/Le Podcast ☕/2026-01-02 Épisode 1 - «Tests»|Épisode 1: «Tests»]] · Le Podcast ☕ — Cité en exemple [00:00:01] [00:00:02]
```

### 5.5 Orphan entity notes

An orphan is an entity note found by the scan whose `entity_id` is not resolved: it was
merged away by `aliases.yaml`, or nothing mentions it any more. earmark never deletes it. It
writes a stub with `update_note(existing, owned=(), seeds=(), region)`, so the frontmatter is
kept verbatim. The region:

1. `f"# {E(stem)}"`, where `stem` is the note's current file stem;
2. either `f"Merged into {note_link(path_A, name_A)} by aliases.yaml."` or
   `"*No episode mentions this entity any more.*"`.

"Merged into A" applies when `entity_id(kind, alias_key)` equals the orphan's id for some
alias row whose `canonical_key` differs from its `alias_key`, and A is resolved. A missing
orphan file is not recreated. The orphan's `EntityRow` has `kind`/`entity_key` from that alias
row (else NULL), `name = None`, `note_path` = its path, and `content_digest =
content_digest((), region)`.

### 5.6 What a store does

`VaultStore.store(E, transcript, analyses, shared_at)`, extending step 12's 5.9:

1. Key checks (unchanged). `vault_lock`.
2. Open the registry lazily: `open_database(database_path(data_dir))` on the first call,
   then cache the `Database` and an `EntityRegistry` on the instance.
3. `open_subdir()`, then the **full identity scan**: a `Snapshot` mapping `(NoteKind, key)`
   to a list of subdir-relative paths, for every note (the same walk and skip rules as step
   12, `Entities/` included).
4. Read the aliases (5.3).
5. `registry.record_episode(E, analyses, shared_at, aliases)`. It upserts the `episodes` row
   (title, show, published_at, first_shared_at, analyses = the hashes in `(created_at,
   prompt_hash)` order), deletes E's mentions, inserts `mentions_from(analyses)`, and when
   `aliases is not None`, replaces the `aliases` table with one row per entry. It does not
   touch `note_path`, `transcript_note_path`, or `links_digest`.
6. `state = registry.state()`, `views = resolve_entities(state)`.
7. **Settle paths**, opening every directory before any write:
   - E's episode and transcript notes, exactly as step 12;
   - every resolved entity's note (5.4);
   - every orphan's scan path;
   - for each other registry episode Y with an episode note in the scan: its note (the
     recorded path if it is among the duplicates, else the smallest; no warning) and its
     transcript note (the scan path, or a candidate allocated as step 12 does).
     Registry episodes without an episode note in the scan are **not** recreated.
8. **Linkers.** For an episode X with analyses A, the entity linker maps `(field, entity)` to
   `note_link(entity_path, entity.name)`, where `entity_path` is the path of the resolved
   entity for `(kind_of(field), canonical(entity_key(entity.name)))`. It returns `None` when
   the key is empty or the entity has no settled path (that cannot happen for an
   in-registry mention). A **links digest** is `sha256` (first 16 hex) of the canonical JSON
   `{"v": RENDER_VERSION, "note": ep_path, "transcript": tr_path, "links": sorted([field,
   name_key, entity_path] for every distinct mention of X)}`.
9. **Remove stale temps** in every directory about to be written (as step 12).
10. **Write**, in this order. Each write uses step 12's `_write_note`, extended to `entity`
    identities and to return whether it wrote:
    1. E's transcript note.
    2. Every resolved entity note, in `(kind index, entity_id)` order: `owned =
       entity_fields(v)`, `region = render_entity_region(...)`, `digest =
       content_digest(owned, f"v{RENDER_VERSION}\n" + region)` (the prefix is only digest
       input and is never written). Process the note (read, merge, and write only if the
       text differs, as `_write_note` already does) when it is missing, or when `digest`
       differs from `entities[id].content_digest`, or when the row's `note_path` differs
       from the settled path. Otherwise skip it without reading it.
    3. Every orphan, by the same digest rule (5.5).
    4. Every other episode Y (7) whose links digest differs from `episodes[Y].links_digest`.
       The notes are rendered from artifacts: `read_episode(Y)`, `read_transcript(Y)`,
       `read_analysis(Y, h)` for each `h` in `episodes[Y].analyses`, and `shared_at =
       episodes[Y].first_shared_at`. Write Y's transcript note, then its episode note, with
       the same functions as E. If any artifact is missing or corrupt (`None`, or
       `PermanentError` from `ArtifactStore`), add the warning `{Y's note path}: could not
       update its links: the episode's files under EARMARK_DATA_DIR are missing or
       unreadable` and skip Y.
    5. E's episode note, last, so its links resolve when it appears.
11. `registry.record_notes(...)`. For E and every Y handled in 10.4, record the two settled
    paths and the links digest (also for a skipped Y, so the warning appears once). For
    entities, record one `EntityRow` per resolved entity and per orphan, with the settled
    path and the digest computed in 10.2/10.3. Record it whether the write happened, was
    skipped as unchanged, or the note was frozen (damaged markers: the warning appears once
    per content change).
12. Return `StoreResult` as before (episode and transcript paths), with all the warnings, in
    order and deduplicated.

The invariants this keeps, which the tests rely on:

- After a store, every earmark-written link to a note that is not frozen points at that
  note's current path.
- A store whose inputs and vault did not change writes nothing.
- Note contents depend only on the artifacts, the aliases, the paths on disk, `TZ`, and
  `RENDER_VERSION`, never on a clock or the order of earlier stores (allocation aside).

### 5.7 Renames and moves

- **The user renames or moves an entity note** inside the subdir (not `Backups/`). The scan
  finds it by `entity_id`. Its settled path changes, so every episode that mentions it gets
  a new links digest and is re-rendered in 10.4 on the next store, whichever episode that
  store is for. The entity note itself is not rewritten unless its content changed (10.2
  compares `note_path` too, so it is just re-verified and recorded).
- **The user renames or moves an episode note.** The entity notes listing it change content
  (the link path) and are rewritten. Its own links digest changes too, so its transcript
  note is re-rendered with the new back link. That also fixes step 12's stale back link.
- **Obsidian rewrote the links already** (rename with auto-update on). The old region
  differs from earmark's new one only in link targets, so `unlinked()` makes it "not
  edited": no backup and no warning, and earmark's full-path form is restored.
- **The user deleted an entity note.** It is recreated (allocated afresh) by the next store.
  If its new path differs, the episodes are relinked.
- **The user deleted an episode note.** It is dropped from the entity notes on the next
  store, and recreated only by its own next store or by `rebuild`.
- **Between stores**, links stay stale only if Obsidian did not update them. That window is
  documented.

### 5.8 Edit detection change (step 12's 5.7)

A region counts as edited by hand only when its hash does not match **and** the old and new
regions differ after `unlinked()`. So editing only a link target inside the markers is
overwritten without a backup. That is acceptable, because earmark regenerates links anyway.
Step 12's tests keep passing: they edit text, not link targets.

### 5.9 `rebuild(prune=False)`

1. `vault_lock`, then the lazy registry, `open_subdir`, and the scan.
2. Aliases: `read_aliases`. `AliasFileError` propagates (nothing has changed yet).
3. Collect the episodes: for each key in `artifacts.list_episode_keys()` (sorted), read the
   episode, transcript, share, and `list_analyses`. Keep it only when all four exist (at least
   one analysis); otherwise count it as `skipped`. A corrupt artifact (`PermanentError`) is
   skipped with the warning `episode {key}: unreadable files under EARMARK_DATA_DIR; skipped`.
4. **Safety check.** Count the episode identities in the scan whose key is not among the
   kept episodes. If that count is above 0 and not `prune`, raise `RebuildRefused(f"{n}
   episode notes in the vault have no usable files under EARMARK_DATA_DIR; rebuilding would
   drop them from every entity note. Restore the data directory, or pass --prune")`.
   Nothing has been written at this point.
5. `registry.reset(kept, aliases.entries)` in one transaction: delete from `mentions`,
   `episodes`, `entities`, `aliases`, then insert.
6. Run 5.6 steps 6–11 with the **target set = every kept episode** instead of E alone,
   processed in `(first_shared_at, key)` order. Each target is settled as E is (allocating
   when its note is missing) and always rendered. After the reset there are no recorded
   digests, so every entity note and orphan is read and compared. Only notes whose text
   differs are written.
7. Return the `RebuildReport`.

So on an intact vault, deleting `earmark.db` and running `rebuild` gives the same registry
rows (5.10) and changes no byte in the vault.

### 5.10 Registry equality after rebuild

Incremental stores and a rebuild produce identical rows in all four tables. Orphan rows
match too, because both derive an orphan's `kind`/`entity_key` from the alias rows only
(5.5), never from an earlier `entities` row. The one exception is `episodes.links_digest` and
the paths of an episode whose relink was skipped for missing artifacts: rebuild drops that
episode (or refuses, 5.9).

### 5.11 Lone surrogates (the step-12 finding)

- **Model boundary.** The `Model` base validator replaces each lone surrogate in any `str`
  (recursively in tuples and lists) with U+FFFD. This covers `Episode` (resolver), `Segment`
  and `Transcript` (both ASR engines), `Claim`, `Entity`, `ResearchLead`, and `Analysis`
  (`build_analysis`), and `Job`. Artifacts therefore always serialise, and SQLite never sees
  one through a model.
- **LLM answer.** `ModelCaller.parse` validates `scrub_surrogates(response.text)` (and the
  repaired text). A JSON-escaped lone surrogate (`\ud800` written by the model inside its
  JSON) is still rejected by pydantic's JSON parser. That is a schema error: permanent, or
  one repair for `openai_compat`. It is never a retry loop. Documented, not worked around.
- **Job text.** `describe_error` applies `scrub_surrogates` before redaction.
- **Vault boundary** (defence in depth, for data built with `model_construct` or
  `model_copy(update=)`): `escape_markdown` cleans, `wikilink_label` cleans, and
  `file_component` and `yaml_quote` already clean. `new_note`, `update_note`, and
  `content_digest` scrub the region. `render_analysis` output is not cleaned in
  `render.py`, so the region scrub covers it.
- **Registry.** All values come from models, and entity-name inputs to SQLite also go through
  `scrub_surrogates` in `mentions_from` (cheap; covers `model_construct`).

### 5.12 Errors

- In `store` and `rebuild`, `sqlite3.OperationalError` becomes `TransientError("the entity
  registry is busy or unavailable ({class name})")`, `DatabaseSetupError` becomes
  `PermanentError(str(exc))` (it names only version numbers or the path inside the data dir;
  use `"the entity registry database cannot be used: " + reason`, without absolute paths),
  and any other `sqlite3.Error` becomes `TransientError(f"entity registry error: {class
  name}")`. OS errors keep step 12's mapping.
- A symlink at `Entities/` or a kind folder raises `SymlinkRefused` (permanent), like any
  directory on the write path.
- Alias problems and unrecoverable relinks are warnings and never fail the job.

### 5.13 Upgrading from step 12

Existing episode notes have no registry rows, so they are not linked until they are stored
again. `docs/ARCHITECTURE.md` says to run `earmark entities rebuild` once after upgrading.
That re-renders every episode note (the region's hash matches, so no backup) and creates
the entity notes.

## 6. Tests

All tests use `tmp_path` and no sockets, with `tz=UTC` unless stated. Helpers in
`tests/test_vault_entities.py`:

- `make_store` (as in `test_vault_notes.py`);
- `ep(n)`: `make_episode(guid=f"g-{n}", title=f"Épisode {n}")`;
- `an(n, *, prompt=None, created_at=T0, **fields)`: an analysis for `ep(n)` with the given
  entity tuples (the others empty), built with `Analysis.model_validate` from
  `make_analysis(...).model_dump()` merged with the fields;
- `put(store, n, analyses, shared_at=T0 + n hours)`: stores `ep(n)` with
  `make_transcript(guid=...)`;
- `links(vault)`: every `[[target|…]]` in every `.md` under the vault, plus the decoded
  Markdown `.md` links;
- `db_rows(data_dir)`: a dump of the four tables, sorted.

No external fixtures are needed.

### Brief acceptance criteria

| # | Criterion | Tests |
|---|---|---|
| B1 | Two episodes naming the same person with different casing or spacing produce one entity note listing both episodes with date, context, and timestamp. | `test_vault_entities.py::test_same_person_two_episodes`: episode 1 has `people=(Entity("Ada Lovelace", "Wrote the first program", (65.0,)),)` and episode 2 has `Entity("ada  LOVELACE.", "Mentioned again", (3.0,))`. Exactly one file under `Earmark/Entities/People/`, `Ada Lovelace.md`. Its region lists episode 2 then episode 1 (newest share first, same day), each line with `2026-01-02`, the episode link, the context, and `[00:01:05]` / `[00:00:03]`, and `Also mentioned as: ada LOVELACE.` (escaped). The frontmatter `name == "Ada Lovelace"`, `entity_id == "82a355902603f38e"`. `::test_entity_region_exact` pins the 5.4 example. `test_models.py::test_entity_key` (moved from merge, with the refined cases: `C++`/`C#`/`C` distinct, `F#`, `#MeToo` → `metoo`, `C++ & Rust` → `c++ rust`, `!!!` → `""`, full-width NFKC). `::test_entity_id_golden` (the three values in 5.2). `test_analyze_merge.py::test_entity_key` updated to import from `merge` and assert `c++ rust`, proving the same function. |
| B2 | Re-storing an episode does not duplicate its mentions. | `::test_restore_no_duplicate_mentions`: store episode 1 three times (the third with a second analysis that names the same person). `mentions` has one row per `(prompt_hash, kind, key)`, the entity note lists episode 1 once with the union of the timestamps, and the 2nd store writes nothing (mtimes unchanged). `::test_duplicates_within_one_analysis_merge` (one list with `Ada Lovelace` and `ada lovelace`: one mention, first name, union of timestamps). |
| B3 | `aliases.yaml` merges are honoured. | `::test_alias_merges_entities`: `people: {Ada Lovelace: [Ada King]}`; episode 1 names `Ada King` and episode 2 `Ada Lovelace`; one note `Ada Lovelace.md` lists both, and the episode-1 note links `Ada King` to it. `::test_alias_added_later_merges_and_stubs`: store both without aliases (two notes), add the file, store episode 3 (unrelated). The `Ada King` note becomes the stub `Merged into [[…Ada Lovelace|Ada Lovelace]] by aliases.yaml.` with its frontmatter byte-unchanged, episode 1's note is relinked to `Ada Lovelace`, and no backup or warning appears. `::test_alias_removed_splits_again`. `::test_alias_pins_display_name` (`topics: {"Large language models": LLMs}`, mention `LLMs` → note `Large language models.md`). `::test_broken_aliases_keep_previous` (valid file, store, then invalid YAML, store: the merge is still applied and the warning contains `not valid YAML at line` and `the aliases loaded before are kept`). `::test_missing_aliases_file_means_none`. `test_vault_aliases.py`: `::test_parse_valid` (the 5.3 example: entries in order, null value pins only, scalar alias), `::test_raw_scalars` (`1984`, `No`, `null` as names stay literal), `::test_whole_file_errors` (parametrised: bad YAML, `*alias`, two documents, a list root, 300 KiB, invalid UTF-8, 5000 nested `[`, all raising `AliasFileError` with the 5.3 message and line), `::test_entry_problems` (unknown kind, non-mapping kind, non-scalar name, `???`, a duplicate canonical, an alias claimed twice, a chain, non-scalar aliases; each reported with its line, the rest kept), `::test_problem_cap` (8 problems → 5 plus `and 3 more problems`), `::test_tags_ignored` (`!!python/object:os.system` composes harmlessly), `::test_symlinked_file_refused` (`read_aliases` raises `AliasFileError`, the target is not read: point the symlink at a FIFO and assert no hang), `::test_bom_allowed`. |
| B4 | Episode notes link each entity, and every link resolves to an existing file. | `::test_episode_note_links_every_entity`: with `make_analysis()`, the region's five entity lines are `- **[[Earmark/Entities/<Folder>/Zoë\|Zoë]]**: Cité en exemple [00:00:01] [00:00:02]`, and every target in `links(vault)` plus `.md` exists. `::test_all_links_resolve_after_many_operations`: store 3 episodes, rename one entity note in Python (`os.rename` to `Entities/People/Ada K.md`), move an episode note to `Earmark/Archive/`, add an alias, store a 4th episode. After each step `links(vault)` all resolve. `::test_unlinkable_name_rendered_plain` (`Entity("???")`: plain `**???**`, no registry row, no note). `::test_markdown_link_fallback` (subdir `Notes#1`: the entity links are percent-encoded Markdown links that decode to existing files). `test_analyze_render.py::test_entity_link_hook` (a linker returning markup for `people` only; other kinds unchanged; `None` → the step-10 text) and `::test_no_linker_unchanged` (equals the reference example). |
| B5 | User text outside the managed markers in an entity note survives regeneration. | `::test_entity_note_user_text_survives`: after the first store, add a paragraph before the begin marker, text after the end marker, a property `born: 1815`, and edit `tags`. Store another episode mentioning the same person: the user bytes are identical, the region lists two episodes, and `name` is rewritten if changed. `::test_entity_note_hand_edit_backed_up` (a text edit inside the region plus a new mention: backup in `Backups/`, warning). `::test_entity_note_damaged_markers_frozen` (warning once; a second unchanged store adds no warning; links to it still resolve). `::test_obsidian_link_rewrite_not_an_edit` (replace `[[Earmark/Entities/People/Zoë\|Zoë]]` in the episode region with `[[Zoë2\|Zoë]]` and rename the note to `Zoë2.md`. The next store of any episode restores full-path links with no backup and no warning). `test_vault_document.py::test_unlinked` and `::test_link_only_change_is_not_edited`. |
| B6 | `earmark entities rebuild` reconstructs the registry from `analyses/*.json` and yields the same notes. | `::test_rebuild_reproduces_registry_and_notes`: through the real pipeline (`make_env` with `VaultStore`), ingest 3 episodes (two prompts on one) with an `aliases.yaml`. Snapshot `tree(vault)` and `db_rows`. Delete `data/earmark.db*`, `open_database` again, and call `store.rebuild()`: the tree is byte-identical with the same mtimes (nothing written), `report.written == 0`, and `db_rows` is equal (all four tables, 5.10). `::test_rebuild_repairs_vault` (delete an entity note and corrupt nothing else: rebuild recreates it, `written == 1`). `::test_rebuild_links_step12_notes` (notes written by a store with the registry emptied are linked after rebuild, without a backup). `::test_rebuild_refuses_without_artifacts` (remove one episode's data dir: `RebuildRefused`, nothing changed; with `prune=True` it proceeds and that episode disappears from the entity notes). `::test_rebuild_bad_aliases_aborts` (`AliasFileError`, the DB is untouched). `::test_rebuild_skips_incomplete` (an episode dir without a transcript: `skipped == 1`). `test_cli.py::test_entities_rebuild` (exit 0, the summary line, warnings on stderr), `::test_entities_rebuild_needs_vault_dir` (exit 2), `::test_entities_rebuild_bad_aliases` (exit 1, message). |
| B7 | Entity names are sanitized like titles. | `::test_hostile_entity_names`, parametrised over `../../x`, `/etc/passwd`, `.obsidian`, `CON`, `a/b\c`, `[[x]]`, `a\|b`, `#tag ^x`, `<!-- earmark:end -->`, `---`, `x: y`, `1000 × é`, `Zoë‮`, as a person and as a topic. Every file stays under `vault/Earmark/Entities/<Folder>/` with an `is_safe_component` name (≤ 200 bytes), the frontmatter parses and `name` equals the input (whitespace-collapsed), the body has exactly one begin and one end marker line, the episode note's link resolves, and nothing is created outside the subdir. `::test_entity_name_collision_with_user_note` (an existing user `Entities/People/Ada Lovelace.md` without frontmatter: the entity note is `Ada Lovelace (82a35590).md`, and the user file is byte-unchanged). `::test_symbol_names_do_not_collide` (tools `C`, `C++`, `C#` in one analysis: three notes, `C.md`, `C++.md`, and `C (<id8>).md` for C# since `#` is removed from the file name). `test_vault_sanitize.py::test_wikilink_label`. |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| C1 | Lone surrogates never fail or loop a job. | `test_models.py::test_lone_surrogates_replaced`: `Episode` title, `Segment` text, `Entity` name (constrained), `Analysis.summary`, `open_questions`, and `Job.warnings` with `\ud800`/`\udfff` all hold U+FFFD, `model_dump_json()` succeeds, and the JSON round trip is equal. `::test_scrub_keeps_valid_text_identical` (an emoji pair, a `\udc80`-free string: `is` the same object). `test_remote_asr.py::test_lone_surrogate_in_segment_text` (the mock server body contains `"text": "a\\ud800"`; the transcript is written with U+FFFD). `test_analyzer.py::test_lone_surrogate_in_answer` (scripted answer text with a raw `\ud800` in the summary and an entity name: the analysis is cached, with U+FFFD) and `::test_escaped_lone_surrogate_is_schema_error` (a `\\ud800` escape: `PermanentError` without repair). `test_runner.py::test_describe_error_scrubs_surrogates`. `test_vault_notes.py::test_lone_surrogates_through_store`: the episode, transcript, and analysis built with `model_construct`/`model_copy(update=)` so they hold `\ud800` in the title, show, segment text, summary, prompt, entity name, and context. `VaultStore.store` succeeds, every written file decodes as strict UTF-8 and contains U+FFFD, and storing again writes nothing. `test_vault_document.py::test_region_surrogates_scrubbed`. `test_vault_sanitize.py::test_escape_markdown_cleans_surrogates`. |
| C2 | Links follow renames and moves (5.7). | `::test_renamed_entity_relinks_other_episodes` (rename `Zoë.md` → `Zoë Z.md` in Python, store an unrelated episode 2: episode 1's note now links `Zoë Z`, and the entity note is not rewritten). `::test_moved_episode_note_updates_entity_and_transcript` (move episode 1's note to `Earmark/Archive/`, store episode 2: the entity notes link the moved path, and episode 1's transcript back link too). `::test_deleted_entity_note_recreated`. `::test_deleted_episode_note_dropped_from_entities`. `::test_relink_with_missing_artifacts_warns_once`. |
| C3 | An unchanged store touches nothing, and the registry stays derived and idempotent. | `::test_second_store_writes_nothing` (all vault mtimes unchanged; `entities` rows unchanged). `::test_two_vaults_identical_with_entities`. `test_db.py::test_entities_migration_tables` (after `open_database`, the four tables and the index exist, `user_version == 2`, and the CHECKs reject an unknown kind and an empty key). |
| C4 | Store errors keep their retry intent. | `::test_registry_busy_is_transient` (monkeypatch `EntityRegistry.record_episode` to raise `sqlite3.OperationalError("database is locked")`: `TransientError`, no vault write). `::test_newer_registry_schema_is_permanent`. `::test_symlinked_entities_dir_refused` (`SymlinkRefused`, and the target stays empty). |
| C5 | The vault stays light and the build creates nothing. | `test_vault_notes.py::test_vault_modules_import_light` unchanged (also import `earmark.vault.entities` and `earmark.vault.aliases`: still no `yaml`). The existing `test_factory.py::test_vault_store_built` still passes (no DB file after the build). `::test_pipeline_end_to_end_entities` (`make_env` with `VaultStore`, two shares: `job.done`, entity notes under `vault/Earmark/Entities/People/Ada Lovelace.md`, listing both). |

Existing tests that must change, and why:

- `test_vault_notes.py::test_episode_region_exact`: the five entity lines become wikilinks
  (B4 form). The direct `render_episode_region` call passes the same `entity_link` (the
  store's helper, or a local lambda giving the same markup).
- `::test_sections_newest_first`: compare against `render_analysis(x, entity_link=...)`, or
  check the order by the `## ` heading lines.
- `test_analyze_merge.py::test_entity_key`: `"C++ & Rust"` → `"c++ rust"`.
- `test_stages.py`: unchanged (the `VaultStore` signature only grows a keyword).

Run the store-level tests also with `tz=ZoneInfo("America/Los_Angeles")` in one parametrised
case (`::test_entity_dates_use_tz`), so dates in entity notes follow `TZ`.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
make check
uv run pytest tests/test_vault_entities.py tests/test_vault_aliases.py tests/test_vault_notes.py \
  tests/test_vault_document.py tests/test_vault_sanitize.py tests/test_models.py \
  tests/test_analyze_merge.py tests/test_analyze_render.py tests/test_db.py tests/test_cli.py -v
git diff --stat pyproject.toml uv.lock     # pyyaml moved to dependencies, nothing else added
uv run python -c 'import sys, earmark.vault.notes, earmark.vault.entities, earmark.vault.aliases; print("yaml" in sys.modules)'
# expect: False
```

Live smoke with every backend `fake` except `STORE_BACKEND=vault`, no network, from a fresh
scratch directory `$SCRATCH`:

```bash
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
mkdir -p vault
export EARMARK_DATA_DIR="$SCRATCH/data" EARMARK_VAULT_DIR="$SCRATCH/vault" EARMARK_TZ=Europe/Paris
for v in RESOLVER_BACKEND FETCH_BACKEND ASR_BACKEND LLM_PROVIDER INDEX_BACKEND NOTIFY_BACKEND; do
  export "EARMARK_$v=fake"
done
export EARMARK_STORE_BACKEND=vault
U='https://podcasts.apple.com/us/podcast/x/id1200361736?i='
$E submit "${U}1000802907981"; $E worker --once
$E submit "${U}1000802907982"; $E worker --once
find vault/Earmark/Entities -type f | sort      # People/Ada Lovelace.md, Papers/…, Companies/Acme Corp.md, …
cat "vault/Earmark/Entities/People/Ada Lovelace.md"   # two episode lines
grep -o '\[\[[^]]*\]\]' vault/Earmark/Podcasts/Fake\ Show/*.md | head
printf 'companies:\n  Acme: [Acme Corp]\n' > vault/Earmark/aliases.yaml
$E submit "${U}1000802907983"; $E worker --once
cat vault/Earmark/Entities/Companies/*.md        # Acme.md lists 3; Acme Corp.md is a "Merged into" stub
find vault -type f -name '*.md' -exec sha256sum {} + | sort > before.txt
rm data/earmark.db*
$E entities rebuild                               # rebuilt …: 3 episodes, …; 0 notes written
find vault -type f -name '*.md' -exec sha256sum {} + | sort | diff - before.txt && echo identical
printf 'people: [oops\n' > vault/Earmark/aliases.yaml
$E entities rebuild; echo "exit $?"               # message with "line 2", exit 1
$E submit "${U}1000802907984"; $E worker --once
$E jobs --json | python3 -m json.tool | grep -A3 warnings   # "... the aliases loaded before are kept"
```

Check by hand that every `[[…]]` target under `vault/` plus `.md` exists.

Cannot be verified in the sandbox: Obsidian's rendering of the entity notes, its link
auto-update format after a rename (simulated by rewriting links in a test), and how it
resolves duplicate basenames.

## 8. Deviations

1. **`entity_key` is moved to `models.py` and refined** (2.2): `+`/`#` after an
   alphanumeric are kept, so C, C++, and C# are distinct entities, both across episodes and
   in step 11's map-reduce merge. One step-11 test assertion changes (`"C++ & Rust"` → `"c++
   rust"`). This is a product-visible behaviour change in step 11's module.
2. **The lone-surrogate fix is mostly outside the vault** (2.1, 5.11): a `Model`-wide
   validator in `models.py`, a scrub in `analyze/calls.py` and in `runner.describe_error`,
   plus the vault-level guards the brief suggested. The vault-only fix would not have stopped
   the retry loops, which happen at the artifact writes, earlier than the store.
3. **PyYAML becomes a runtime dependency**, used through `yaml.compose` (no construction,
   aliases refused), not `safe_load` (2.4). It was dev-only.
4. **An extra table `episodes`** beside the brief's `entities`, `aliases`, and `mentions`.
   It holds the episode metadata and the note paths and digests needed to list episodes and
   detect stale links without reading artifacts.
5. **Stores relink other episodes** whose links went stale (5.6 step 10.4, 5.7). A store can
   now write notes of episodes other than its own, and a job can carry warnings about them.
6. **Edit detection ignores link targets** (5.8), a change to step 12's managed-region rule,
   so that Obsidian's rename rewrites are not treated as hand edits.
7. **Orphan entity notes become stubs** ("Merged into …" or "No episode mentions this
   entity any more."), keeping their frontmatter. They are never deleted.
8. **`rebuild` regenerates every earmark note, not just the registry.** It refuses when
   episode notes in the vault have no artifacts, unless `--prune`, so a lost data directory
   cannot silently empty every entity note.
9. **`note_link` labels are sanitised** (`wikilink_label`, or `escape_markdown` for Markdown
   links), because labels are now untrusted names.
10. **`escape_markdown` now NFC-normalises** (through `clean_unicode`). For NFD input that
    changes a step-12 region once, rewritten without a backup since the hash matches.
11. **Upgrade step:** existing episode notes get entity links only after `earmark entities
    rebuild` (or their next store).

Changes made to `docs/ARCHITECTURE.md` in this planning step:

- the Decisions tooling row (PyYAML);
- the Domain models (surrogate scrubbing, `entity_key`);
- the module layout (`vault/entities.py`, `vault/aliases.py`);
- the Storing section (entities, relinking, rebuild, CLI);
- the data layout (the tables, `Entities/` naming, `aliases.yaml`);
- the Entities and Managed regions paragraphs;
- the Security vault and aliases bullets;
- a Known risk on duplicate basenames in Obsidian.

## 9. Out of scope

- An `aliases.md` note, or any way to edit aliases inside Obsidian (the YAML file is hidden
  by Obsidian unless "Show all file types" is on). Splitting an entity, and cross-kind merges.
- Obsidian `aliases` frontmatter on entity notes (it would collide with the user's own
  `aliases` property). The other names are listed in the region instead.
- Checking basenames outside the subdir for collisions with the user's notes. Known risk:
  Obsidian's resolution of a user's bare `[[Ada Lovelace]]` once earmark's
  `Ada Lovelace.md` exists is unverified.
- Accent folding, honorific stripping, and fuzzy or LLM-assisted entity resolution.
- Entity notes for episodes with deleted episode notes, and any per-user opt-out from
  entity notes.
- Indexing entities for search (step 14), digests (step 18), and listing or querying the
  registry from the CLI or API.
- Rebuilding entity data from the vault when the data directory is lost (the analyses are
  needed).
- Reducing the identity scan's 64 KiB head read per note. With many transcript notes that is
  tens of MB per store; acceptable for now.
