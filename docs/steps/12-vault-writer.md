# Step 12: Vault writer

Plan written 2026-11-20 against the code at `8fb212e` (steps 01–11 done).

## 1. Outcome

`EARMARK_STORE_BACKEND=vault` (the default) is built. `earmark.vault.notes.VaultStore` is
the real `Store`. For each episode it writes two Obsidian notes inside
`<VAULT_DIR>/<VAULT_SUBDIR>/`:

- the **episode note** `Podcasts/<Show>/<YYYY-MM-DD> <Title>.md`: YAML frontmatter, then a
  managed region with a header, a transcript link, and one `render_analysis` section per
  analysis, newest first;
- the **transcript note** `Podcasts/<Show>/transcripts/<YYYY-MM-DD> <Title> (transcript).md`,
  linked both ways with the episode note.

Names are sanitized, keep Unicode (NFC), are capped in bytes, and get a short key suffix
only on a collision. Collisions are judged case- and normalization-insensitively, so
the vault stays valid on macOS and iCloud. Each note is found again by the
`episode_key` in its frontmatter. A note keeps its path after the episode is retitled
or the user renames or moves it. Text and frontmatter keys the user adds survive
regeneration. Edits inside the markers are backed up before they are replaced. A note
with damaged markers is left alone, with a warning. Every write goes through directory
file descriptors with `O_NOFOLLOW`, so a symlink inside the vault cannot send a write
outside the subdir. Writes are atomic. Storing the same inputs twice changes no byte and
touches no file. The job result's `note_path` and `transcript_note_path` point at the
two notes. The runner already records them, so the runner does not change.

No new runtime dependency. No new setting.

## 2. Findings

### 2.1 What exists

- `Store` Protocol (`pipeline/stages.py`): `store(episode, transcript, analyses,
  shared_at, /) -> StoreResult`, positional-only. The runner (`pipeline/runner.py`)
  passes every analysis on disk in `(created_at, prompt_hash)` order, plus the first
  share time from `shared.json`. It copies `stored.warnings` into the job's warnings
  and puts `note_path` and `transcript_note_path` into `JobResult`. **Nothing in the
  runner changes.**
- The task brief calls these `NoteStore`/`FakeNoteStore`. The code calls them `Store`
  and `FakeStore`. This plan uses the code's names. `FakeStore` stays as it is: it
  writes `Earmark/Podcasts/<key>.md`, and the fake-store tests and the step-05 smoke
  depend on that.
- `StoreResult.note_path` and `transcript_note_path` are validated as relative POSIX
  paths with no `..` and no `\` (`models._check_relative_posix`). A path we return must
  pass that check, so the identity scan (5.4) ignores names containing `\`.
- `Settings.vault_dir: Path | None`, `vault_subdir: str` (default `Earmark`). The subdir
  is validated as a relative path with no `..` or `\`. `PurePosixPath` drops `.` and
  empty parts. `Settings.tz: str | None` is a validated IANA zone, and the architecture
  says it governs note dates.
- `factory.STORES` has only `"fake"`. `tests/test_factory.py::test_default_settings_list_all_unbuilt`
  expects `store_backend` to be unbuilt and must be updated (section 6, A9).
- `analyze.render.render_analysis(analysis, heading_level=2)` is deterministic and ends
  with one `\n`. It escapes `<` to `&lt;`, so analysis text can never contain a marker
  line. It escapes Markdown block syntax at paragraph starts. Warnings are not rendered,
  and step 10 left that choice to this step.
- `resolve.text.clean_text` already strips control characters from titles and show
  titles, collapses their whitespace, and caps them at 500 characters. `Episode` itself
  accepts any `str`, so the vault code must not rely on that cleaning.
- `artifacts.atomic_writer` works on `Path`s through `tempfile.mkstemp` and
  `Path.replace`. It cannot use directory fds, so the vault gets its own fd-based
  writer (4.2). The data directory stays on `ArtifactStore`.
- PyYAML 6.0.3 is in the lock **only** as a dev dependency (for `tests/test_ci.py`) and
  as a transitive dependency of the `transcribe` extra (huggingface-hub). It is not a
  runtime core dependency.

### 2.2 YAML frontmatter: a constrained emitter, no new dependency

**Decision:** earmark writes frontmatter with its own small emitter. It does not add
PyYAML to the runtime dependencies. Tests check the output with PyYAML (already a dev
dependency).

Why:

- earmark's frontmatter is a flat mapping. Keys are fixed ASCII identifiers. Values are
  strings, `null`, dates, local date-times, and lists of strings. Every string is written
  as a YAML **double-quoted scalar**, so no title can become a key, a comment, a
  document marker (`---`), a tag (`!x`), an anchor or alias, a flow collection, or a
  boolean or number (`yes`, `null`, `1e3`).
- `yaml.safe_dump` output varies with the PyYAML version and its defaults. By default it
  folds long strings at 80 columns, and it picks quoting styles. Byte-identical
  regeneration would depend on those details.
- A runtime dependency only for this is not worth it. The emitter is about 30 lines.

Checked in the sandbox with PyYAML 6.0.3 (pure and `CSafeLoader`). A double-quoted
scalar that escapes only `"`, `\`, U+0000–U+001F, U+007F–U+009F, U+2028, U+2029,
U+FEFF, U+FFFE, and U+FFFF as `\uXXXX`, and writes every other character raw, round-trips
exactly. The test cases were: colons, `"`, `---`, embedded `\n---\nb: c`, `#`, `- item`,
NEL, LS, PS, BOM, a ZWJ emoji, tab, NUL, ESC, DEL, U+009F, `yes`, `null`, `~`,
`2026-01-01`, `123`, `!tag`, `&anchor *alias`, `{flow: x}`, `[a, b]`, `%YAML`, backticks,
NBSP, and U+E0001. A lone surrogate cannot be encoded as UTF-8, so the emitter replaces
it with U+FFFD. Unquoted `date: 2026-01-01` loads as `datetime.date`,
`shared_at: 2026-01-01T09:30:00` as a naive `datetime`, `[]` as an empty list, and
`null` as `None`.

Escapes are restricted to `\uXXXX` because YAML 1.1, YAML 1.2, and JSON all accept that
form. Raw NEL, LS, and PS are line breaks in YAML 1.1, so they must be escaped. PyYAML's
reader rejects raw C1 controls.

**Obsidian** (checked at `https://obsidian.md/help/properties`, 2026-11-20): Date
properties are `date: 2020-08-21`. Date & time properties are `2020-08-21T10:30:00`,
with no offset. Links in text properties "must be surrounded with quotes", as in
`"[[Link]]"`. Tags are a list, one per line, `- tag`. Nested properties are not
supported. The frontmatter must start at "the very beginning of a file". So `date` is
an unquoted date, `shared_at` is an unquoted local date-time without an offset (5.6),
and link properties are quoted wikilinks.

Not verified: Obsidian itself, which cannot run here, and its YAML parser's handling of
`\u` escapes. Escapes appear only for control and separator characters, which the
resolver already removes from titles. A user prompt could still contain them.

### 2.3 Links and filenames in Obsidian

Checked at `https://obsidian.md/help/links` (2026-11-20). "A string which contains the
following characters may not work as a link: `# | ^ : %% [[ ]]`." Folder paths in links
"start at the vault root and use forward slashes". Markdown links "URL encode the link
destination", for example `Projects/Three%20laws%20of%20motion.md`. Obsidian also refuses
`\ / :` in file names, and Windows refuses `* " < > ? |`. The sanitizer (5.1) removes or
maps all of these, so generated paths are always valid wikilink targets. Links use the
full vault-relative path without `.md`, for example
`[[Earmark/Podcasts/Show/transcripts/2026-01-02 Title (transcript)|Transcript]]`.
`StoreResult` paths are already relative to `VAULT_DIR`, which the architecture treats
as the vault root (the `obsidian://open?file=` argument).

### 2.4 Symlink-safe filesystem calls (checked in the sandbox, Linux, Python 3.13.5)

- `os.open`, `os.mkdir`, `os.rename`, `os.stat`, and `os.unlink` are in
  `os.supports_dir_fd`. `os.scandir` accepts a directory fd (`os.supports_fd`).
- `os.replace(src, dst, src_dir_fd=d, dst_dir_fd=d)` **works**, but `os.replace` is
  **not** listed in `os.supports_dir_fd`. Do not assert membership for it. On POSIX
  `os.rename` behaves the same and is listed, so either is fine. The plan uses
  `os.replace`.
- Renaming a temp file onto a name that is a symlink replaces the **symlink itself**.
  The symlink's target outside the vault was not created or touched. Rename never
  follows the destination. earmark still never renames onto a symlink: such a name
  counts as occupied (5.3).
- `os.open(name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW, dir_fd=d)` on a symlink to a
  directory fails with **`ENOTDIR`** (20) on Linux, not `ELOOP`. On a symlink to a file,
  `os.open(name, O_RDONLY | O_NOFOLLOW, dir_fd=d)` fails with `ELOOP` (40). macOS
  returns `ELOOP` for both. Treat `ENOTDIR` and `ELOOP` alike, then use
  `os.stat(name, dir_fd=d, follow_symlinks=False)` to say which case it was.
- With `follow_symlinks=False`, `os.fwalk` **lists a symlinked directory in `dirs`** but
  does not descend into it. It lists symlinked files in `files`. That is a trap, so the
  scan uses `os.scandir(fd)` with `DirEntry.is_dir(follow_symlinks=False)` and
  `is_file(follow_symlinks=False)` instead of `fwalk`.
- `fcntl.flock` is available on Linux and macOS.

### 2.5 Case and normalization on the filesystems a vault lives on

Linux ext4 is case-sensitive and does not normalize names: `Café` in NFC and in NFD are
two files. APFS (macOS, and the iCloud Drive that Obsidian vaults are often synced
through) is case-insensitive by default and normalization-insensitive, though it keeps
the name as written. A vault written on the Linux homelab and synced to a Mac
therefore must not contain two names that differ only in case or normalization. On the
Mac they would be one file, which means a sync conflict or a silent overwrite. Hence
5.2: names are NFC, collisions are judged on a fold key that is coarser than APFS's
(over-folding only costs an unneeded suffix), and an existing directory whose fold key
matches is reused instead of creating a near-duplicate.

### 2.6 Dead ends to avoid

- Do not use `Path.resolve()` and `is_relative_to` as the containment check. It is a
  check-then-use race, and it follows symlinks the user planted. Walk with directory
  fds (4.2).
- Do not use `yaml.safe_dump` at runtime (2.2), and do not hand-roll a YAML **parser**.
  To read existing frontmatter, the code only splits lines and matches top-level keys
  (5.5).
- Do not split note text with `str.splitlines()`. It also splits on `\x0b`, `\x1c`–`\x1e`,
  `\x85`, U+2028, and U+2029, so user text would be rewritten. Split on `\n` only and
  keep line endings.
- Do not decode existing notes strictly. Use `errors="surrogateescape"` on decode and on
  encode, so the user's bytes, invalid UTF-8 included, survive byte for byte.
- Do not open an existing file for writing. Always write a new temp file and rename it,
  so a hard link or symlink at the destination is never written through.
- Do not open vault files without `O_NONBLOCK`. A FIFO named `x.md` would block the
  worker. After opening, `fstat` must show a regular file.

## 3. Files

| File | Purpose |
|---|---|
| `src/earmark/vault/__init__.py` | New, docstring only. |
| `src/earmark/vault/sanitize.py` | New. Untrusted text to file names, fold keys, Markdown inline escaping, YAML double-quoted scalars, link-target checks (4.1, 5.1). Stdlib only. |
| `src/earmark/vault/fs.py` | New. `Vault`, `VaultDir`: fd-based, symlink-refusing directory walk, reads, atomic writes, temp cleanup; `vault_lock` (4.2, 5.3). Stdlib only. |
| `src/earmark/vault/document.py` | New. The note file format: frontmatter emit, split, merge, and identity; managed region find, digest, and replace (4.3, 5.5, 5.7). Steps 13 and 18 reuse it. Stdlib only. |
| `src/earmark/vault/notes.py` | New. `VaultStore`, `build_vault_store`, path planning, the identity scan, episode and transcript note rendering (4.4, 5.2–5.9). |
| `src/earmark/pipeline/factory.py` | Register `"vault": _vault_store` in `STORES` (lazy import). |
| `tests/test_vault_sanitize.py` | New. |
| `tests/test_vault_fs.py` | New. |
| `tests/test_vault_document.py` | New. |
| `tests/test_vault_notes.py` | New. Includes the pipeline test with the real store. |
| `tests/test_factory.py` | Update the unbuilt-defaults test. Add vault builder tests. |
| `docs/ARCHITECTURE.md` | Module layout, a new "Storing" contract section, data layout, notes, security, config row (section 8 lists the changes). |

No change to `models.py`, `runner.py`, `fakes.py`, `config.py`, `.env.example`, or
`pyproject.toml`.

## 4. Interfaces

Signatures are the contract. Private helpers are up to the implementer. Python 3.13
syntax.

### 4.1 `earmark.vault.sanitize`

```python
MAX_NAME_BYTES: Final = 200  # any file or directory name earmark creates
TITLE_BYTES: Final = 120  # title part of a note name
SHOW_BYTES: Final = 100  # show directory name
ELLIPSIS: Final = "…"


def clean_unicode(text: str) -> str: ...
def fit_bytes(text: str, max_bytes: int) -> str: ...
def file_component(text: str, *, max_bytes: int, fallback: str) -> str: ...
def fold_key(name: str) -> str: ...
def is_safe_component(name: str) -> bool: ...
def escape_markdown(text: str) -> str: ...
def yaml_quote(text: str) -> str: ...
def wikilink_target_ok(target: str) -> bool: ...
```

- `clean_unicode`: lone surrogates (U+D800–U+DFFF) become U+FFFD, then NFC.
- `fit_bytes(text, max_bytes)`: returns `text` unchanged if its UTF-8 length is at most
  `max_bytes`. Otherwise it cuts at a code point boundary so the result plus `ELLIPSIS`
  is at most `max_bytes`, then applies the boundary repair in 5.1 step 7, strips
  trailing spaces and dots, and appends `ELLIPSIS`. `max_bytes` must be at least 8,
  else `ValueError`.
- `file_component`: the full algorithm is 5.1. It always returns a non-empty name with
  `is_safe_component(result)` true and at most `max_bytes` UTF-8 bytes. `fallback` must
  itself be safe, else `ValueError`.
- `fold_key(name) = NFKC(casefold(NFKC(name)))`. Used only for comparisons, never as a
  name.
- `is_safe_component(name)`: non-empty; NFC; UTF-8 length ≤ `MAX_NAME_BYTES`; no `/`,
  `\`, NUL, or other `Cc`/`Cf` character except U+200C and U+200D; none of
  `: * ? " < > | # ^ [ ]`; does not start with `.` or a space; does not end with `.` or
  a space; contains no `%%`.
- `escape_markdown(text)`: collapse all whitespace runs (`str.split()`) to one space,
  replace `\` with `\\`, backslash-escape each of `` ` * _ [ ] | ~ = $ % ^ # ``, and
  replace `<` with `&lt;`. Used for every untrusted value placed in note bodies by this
  step: title, show, model names, warnings, transcript text. It is not applied to
  `render_analysis` output, which has its own escaping (section 9).
- `yaml_quote(text)`: `"` + body + `"`. The body is `clean_unicode(text)`, with `\`
  written as `\\`, `"` as `\"`, and each of U+0000–U+001F, U+007F–U+009F, U+2028, U+2029,
  U+FEFF, U+FFFE, and U+FFFF as `\u` followed by 4 lower-case hex digits. Everything else
  is raw. The result is always one line.
- `wikilink_target_ok(target)`: true when `target` has no character from
  `# | ^ : [ ]`, no `%%`, no `\n`/`\r`, and is non-empty.

### 4.2 `earmark.vault.fs`

```python
type EntryKind = Literal["file", "dir", "symlink", "other"]


@dataclass(frozen=True, slots=True)
class Entry:
    name: str  # decoded with surrogateescape
    kind: EntryKind


@dataclass(frozen=True, slots=True)
class FileStamp:
    ino: int
    size: int
    mtime_ns: int


class SymlinkRefused(PermanentError): ...  # message names the vault-relative path


class NoteChanged(TransientError): ...  # the file changed between our read and our write


class VaultDir:
    """An open directory inside the vault subdir. Close it, or use it as a context manager."""

    rel: PurePosixPath  # relative to the subdir; PurePosixPath(".") for the subdir itself

    def __enter__(self) -> Self: ...
    def __exit__(self, *exc: object) -> None: ...
    def close(self) -> None: ...
    def entries(self) -> list[Entry]: ...
    def child(self, name: str, *, create: bool) -> "VaultDir | None": ...
    def read(self, name: str, *, max_bytes: int) -> tuple[bytes, FileStamp] | None: ...
    def write_atomic(self, name: str, data: bytes, *, expect: FileStamp | None) -> None: ...
    def remove_stale_temps(self, *, now: float, older_than_s: float = 3600.0) -> int: ...


class Vault:
    def __init__(self, vault_dir: Path, subdir: str) -> None: ...

    @property
    def subdir_parts(self) -> tuple[str, ...]: ...  # PurePosixPath(subdir).parts

    def open_subdir(self) -> VaultDir: ...  # creates the subdir components if missing


@contextmanager
def vault_lock(path: Path, *, timeout_s: float) -> Iterator[None]: ...


TEMP_NAME: Final = re.compile(r"\.earmark-[0-9a-f]{16}\.tmp")
MAX_NOTE_BYTES: Final = 16 * 1024 * 1024
```

Behaviour (details in 5.3):

- `Vault.open_subdir()` opens `vault_dir` with `O_RDONLY | O_DIRECTORY | O_CLOEXEC`. It
  follows symlinks at this one level, because `VAULT_DIR` is operator configuration. A
  missing `vault_dir` raises `TransientError` (an unmounted share can come back), and a
  `vault_dir` that is not a directory raises `PermanentError`. Both messages name
  `EARMARK_VAULT_DIR`. Then each part of the subdir is opened with
  `O_NOFOLLOW | O_DIRECTORY` relative to its parent fd, and created (`0o755`) when
  missing. A symlink or non-directory raises `SymlinkRefused`. The subdir parts are
  operator configuration and need not pass `is_safe_component` (for example
  `Notes#1`). They must still not be empty, `.`, or `..`, or contain NUL, which the
  settings validator and `PurePosixPath` already guarantee.
- `SymlinkRefused` message: `f"refusing to write through a symlink or non-directory at
  {rel} in the vault subdir; replace it with a real folder"`. `rel` is vault-relative
  (it starts with the subdir).
- `child(name, create=)`: `name` must pass `is_safe_component`, else `ValueError` (a
  bug). If an entry with exactly that name exists, it is used. Otherwise the code looks
  for an entry with the same `fold_key`; if there is exactly one, and it is a real
  directory, that one is reused (there is no fold reuse for the subdir's own parts).
  Otherwise, with `create=True`, the directory is made (`0o755`, `os.mkdir(...,
  dir_fd=)`). `FileExistsError` from a race is fine: open again. With `create=False` and
  nothing found, it returns `None`. A symlink, or anything that is not a directory, at
  the chosen name raises `SymlinkRefused`.
- `read(name, max_bytes=)`: `os.open(name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK |
  O_CLOEXEC, dir_fd=)`. `ENOENT` returns `None`. `ELOOP`, or an `fstat` that is not
  `S_ISREG`, raises `SymlinkRefused`. A size above `max_bytes` raises `PermanentError`
  ("… is larger than 16 MiB"). It returns the bytes and the stamp from `fstat`.
- `write_atomic(name, data, expect=)`: create `.earmark-<16 hex>.tmp`
  (`secrets.token_hex(8)`) with `O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC`,
  mode `0o644`, `dir_fd=`, retrying the name on `FileExistsError`. Write all bytes, then
  `fsync`, then `fchmod(0o644)`, then close. Then `os.stat(name, dir_fd=,
  follow_symlinks=False)`: if `expect is None` the name must not exist, otherwise its
  `(st_ino, st_size, st_mtime_ns)` must equal `expect`. A mismatch raises `NoteChanged`.
  A symlink at the name raises `SymlinkRefused`. Then `os.replace(tmp, name,
  src_dir_fd=, dst_dir_fd=)` and `fsync` of the directory fd (ignoring `OSError` there).
  On any exception, `BaseException` included, the temp file is unlinked (ignoring
  `FileNotFoundError`) and the exception re-raised.
- `remove_stale_temps(now=)`: unlinks entries matching `TEMP_NAME` that are regular
  files (not followed) with `st_mtime` more than `older_than_s` before `now`. It returns
  the count and never raises for `OSError` (it logs at DEBUG).
- `vault_lock(path, timeout_s=)`: `mkdir` the parent, open `path`
  (`O_RDWR | O_CREAT | O_CLOEXEC`, `0o644`), and try `flock(LOCK_EX | LOCK_NB)` every
  50 ms until `timeout_s`. Then raise `TransientError("another earmark process is
  writing to the vault; try again later")`. It unlocks and closes on exit.
- OS error mapping, applied by `VaultStore` around its whole body (5.10).

### 4.3 `earmark.vault.document`

```python
type YamlValue = str | None | date | datetime | tuple[str, ...]  # datetime is naive local


BEGIN_MARKER: Final = re.compile(r"<!-- earmark:begin(?: hash=([0-9a-f]{16}))? -->")
END_MARKER: Final = "<!-- earmark:end -->"


def begin_marker(digest: str) -> str: ...  # f"<!-- earmark:begin hash={digest} -->"
def region_digest(region: str) -> str: ...
def emit_frontmatter(fields: Sequence[tuple[str, YamlValue]]) -> list[str]: ...


@dataclass(frozen=True, slots=True)
class NoteText:
    bom: str  # "﻿" or ""
    frontmatter: tuple[str, ...] | None  # lines between the delimiters, endings kept
    body: tuple[str, ...]  # lines after the closing delimiter, endings kept


def split_note(text: str) -> NoteText: ...
def note_identity(text: str) -> tuple[str, str] | None: ...  # (kind, episode_key)


class DamagedNote(Exception):
    reason: str  # "missing" or "damaged"


@dataclass(frozen=True, slots=True)
class Updated:
    text: str
    edited: bool  # the old region was changed by hand
    old_digest: str  # region_digest of the old region


def new_note(
    owned: Sequence[tuple[str, YamlValue]],
    seeds: Sequence[tuple[str, YamlValue]],
    region: str,
) -> str: ...


def update_note(
    existing: str,
    owned: Sequence[tuple[str, YamlValue]],
    seeds: Sequence[tuple[str, YamlValue]],
    region: str,
) -> Updated: ...  # raises DamagedNote
```

- `region` is the generated region text. It is empty or ends with `\n`, and it never
  contains a marker line (render escaping guarantees that; `new_note` and `update_note`
  assert it).
- `region_digest(region)`: `sha256(region.replace("\r\n", "\n").encode("utf-8",
  "surrogateescape")).hexdigest()[:16]`.
- `emit_frontmatter`: one entry per field, in order. A key must match
  `[a-z][a-z0-9_]*`, else `ValueError`. A `str` value becomes `key: <yaml_quote>`,
  `None` becomes `key: null`, a `date` becomes `key: YYYY-MM-DD`, a naive `datetime`
  becomes `key: YYYY-MM-DDTHH:MM:SS` (an aware one is a `ValueError`), an empty tuple
  becomes `key: []`, and a non-empty tuple becomes `key:` followed by one `  - <yaml_quote>`
  line per item. Lines end with `\n`.
- `new_note` = `"---\n"` + `emit(owned)` + `emit(seeds)` + `"---\n"` +
  `begin_marker(region_digest(region)) + "\n"` + `region` + `END_MARKER + "\n"`.
- `update_note`: 5.5 and 5.7.
- `note_identity(text)`: from `split_note(text).frontmatter`, the first top-level
  `earmark:` line whose unquoted value is `episode` or `transcript`, and the first
  top-level `episode_key:` line whose unquoted value matches `[0-9a-f]{20}`. Either one
  missing gives `None`. "Unquoted" means stripped, then one pair of matching surrounding
  `"` or `'` removed.

### 4.4 `earmark.vault.notes`

```python
LOCK_TIMEOUT_S: Final = 60.0
SCAN_HEAD_BYTES: Final = 64 * 1024
BACKUPS_DIR: Final = "Backups"
PODCASTS_DIR: Final = "Podcasts"
TRANSCRIPTS_DIR: Final = "transcripts"


class VaultStore:  # satisfies the Store Protocol
    def __init__(
        self,
        vault_dir: Path,
        subdir: str,
        *,
        data_dir: Path,
        tz: tzinfo | None = None,
        lock_timeout_s: float = LOCK_TIMEOUT_S,
        now: Callable[[], float] = time.time,
    ) -> None: ...

    def store(
        self,
        episode: Episode,
        transcript: Transcript,
        analyses: Sequence[Analysis],
        shared_at: datetime,
        /,
    ) -> StoreResult: ...


def build_vault_store(settings: Settings) -> VaultStore: ...


def note_stem(episode: Episode, shared_at: datetime, tz: tzinfo | None) -> str: ...
def show_dir_name(episode: Episode) -> str: ...
def render_episode_region(
    episode: Episode,
    transcript: Transcript,
    analyses: Sequence[Analysis],
    transcript_link: str | None,
    tz: tzinfo | None,
) -> str: ...
def render_transcript_region(
    episode: Episode, transcript: Transcript, episode_link: str | None, tz: tzinfo | None
) -> str: ...
def note_link(vault_rel_path: str, label: str) -> str: ...


def _after_read(vault_rel_path: str) -> None: ...  # no-op test seam, see A12
```

`_after_read` is called after each read of an existing note and before the merge.
It does nothing in production, and tests monkeypatch it.

- `tz=None` means the system's local zone, applied as `dt.astimezone()` (DST-correct),
  never a fixed offset taken once.
- `now` is used only for temp-file cleanup ages. The note contents use no clock.
- `build_vault_store(settings)`:
  - `vault_dir` must be set, else `ConfigError(["EARMARK_VAULT_DIR: required when
    EARMARK_STORE_BACKEND=vault"])`.
  - It must exist and be a directory (symlinks followed), else `ConfigError` with
    `"EARMARK_VAULT_DIR: <path> does not exist"` or `"... is not a directory"`.
  - `tz` is `ZoneInfo(settings.tz)` if set, else `None`.
  - It creates nothing: no subdir, no lock file.
  - It returns `VaultStore(settings.vault_dir.absolute(), settings.vault_subdir,
    data_dir=settings.data_dir, tz=tz)`.
- `factory.STORES["vault"] = _vault_store`, which does
  `from earmark.vault.notes import build_vault_store` inside the function.
- `note_link(path, label)`: if `wikilink_target_ok(target)` with `target = path`
  without its final `.md`, it returns `f"[[{target}|{label}]]"`. Otherwise it returns
  `f"[{label}]({quote(path, safe='/')})"` (`urllib.parse.quote`, a percent-encoded
  vault-relative Markdown link). `label` is a fixed ASCII word from this module.

## 5. Behaviour and edge cases

### 5.1 File names from untrusted text (`file_component`)

In order:

1. `clean_unicode` (surrogates to U+FFFD, NFC).
2. Character classes. Characters in `Cc`, `Zl`, or `Zp`, or for which `str.isspace()` is
   true, become a space. Characters in `Cf`, `Cs`, `Cn` (which includes noncharacters
   such as U+FFFE and U+FDD0), and U+FFFD are removed, except U+200C (ZWNJ) and U+200D
   (ZWJ), which are needed for Persian, Indic scripts, and emoji sequences. This
   removes bidi overrides such as U+202E, which could disguise a name, and the BOM.
3. ASCII mapping: `/`, `\`, and `|` become `-`. `": "` becomes `" - "`, and any other
   `:` becomes `-`. `"` becomes `'`. `[` becomes `(` and `]` becomes `)`. The characters
   `* ? < > # ^` are removed. A run of `%%`… becomes one `%`.
4. Collapse runs of spaces to one. Then strip, repeatedly, leading spaces and `.`, and
   trailing spaces and `.`. So `..`, `.`, `.hidden`, and `../../x` cannot produce a
   hidden or parent name (`../../x` becomes `-..-x`).
5. Windows device names: if the part before the first `.`, compared case-insensitively,
   is one of `CON PRN AUX NUL COM1`–`COM9` or `LPT1`–`LPT9`, append `_` to that part.
6. If the result is empty, use `fallback`.
7. `fit_bytes(result, max_bytes)`. Boundary repair after a cut: repeatedly drop the
   last character while it is a combining mark (`Mn`, `Mc`, `Me`), U+200C, U+200D, a
   variation selector (U+FE00–U+FE0F, U+E0100–U+E01EF), or an emoji modifier
   (U+1F3FB–U+1F3FF). Then, if the last character is preceded by U+200D, drop both and
   repeat. Then, if the trailing run of regional indicators (U+1F1E6–U+1F1FF) has odd
   length, drop one. The result does not end inside an accent, a ZWJ emoji, or a flag.
   It is not full grapheme segmentation, which the stdlib lacks; a rare sequence may
   still be cut.
8. NFC again, assert `is_safe_component`, and return.

Fallbacks are `"Untitled"` for titles and `"Unknown show"` for shows.

### 5.2 Paths

- Date `D`: `episode.published_at` converted to `tz`, as `YYYY-MM-DD`. When
  `published_at` is `None`, `shared_at` converted to `tz` is used instead.
- Episode stem: `f"{D} {file_component(title, max_bytes=TITLE_BYTES, fallback='Untitled')}"`.
- Show directory: `file_component(show_title, max_bytes=SHOW_BYTES, fallback="Unknown show")`.
- Episode note candidate: `Podcasts/<show>/<stem>.md`.
- Transcript note candidate: `<dir of the episode note>/transcripts/<tstem>.md`, where
  `tstem` is the episode note's actual file stem plus `" (transcript)"`, shortened with
  `fit_bytes(stem, MAX_NAME_BYTES - len(" (transcript).md"))` when needed. Use the
  *actual* stem, so a collision suffix or a user rename carries over.
- **Collision.** A name is free in a directory when no entry there (files, directories,
  symlinks, anything) has the same `fold_key`. If the candidate is not free, try
  `f"{stem} ({key[:8]}).md"`, then `f"{stem} ({key}).md"`. If all three are taken, raise
  `PermanentError("no free note name for episode <key> in <rel dir>")`. Suffixed names
  are fitted so the whole name stays within `MAX_NAME_BYTES`.
- Directories `Podcasts`, `<show>`, and `transcripts` are opened with `child(...,
  create=True)`, which reuses a fold-equal existing directory (4.2). For example,
  `le podcast ☕` already on disk is reused for show `Le Podcast ☕`.
- The returned `note_path` is `"/".join(vault.subdir_parts + rel_parts)`, where
  `rel_parts` are the names actually on disk. It is validated by `StoreResult`.

Example (`tests/factories.make_episode()`, `tz=UTC`): show `Le Podcast ☕`, title
`Épisode 1: «Tests»`, published `2026-01-02T03:04:05-08:00`, so the date is `2026-01-02`.

```
Earmark/Podcasts/Le Podcast ☕/2026-01-02 Épisode 1 - «Tests».md
Earmark/Podcasts/Le Podcast ☕/transcripts/2026-01-02 Épisode 1 - «Tests» (transcript).md
```

### 5.3 Symlinks and containment

- `VAULT_DIR` may itself be a symlink (operator configuration). It is opened once,
  following symlinks.
- Everything below it is reached only by `openat`-style calls on directory fds, with
  `O_NOFOLLOW`. Those are the subdir parts, `Podcasts`, show directories,
  `transcripts`, `Backups`, and note files. A symlink anywhere on the write path raises
  `SymlinkRefused`, a `PermanentError` naming the vault-relative path. The job fails
  with a message telling the user to replace the symlink with a real folder. Because the
  fds are held, swapping a directory for a symlink after the check changes nothing.
- A symlink at a note's **candidate name** is not an error. The name is occupied, so
  the note gets a suffix (5.2). Nothing is written through the symlink or onto it.
- The identity scan (5.4) never follows symlinks, so a symlinked note outside the vault
  that carries a matching `episode_key` is ignored.
- Hard links: earmark never opens an existing file for writing (rename only), so a
  hard-linked file elsewhere is never modified.
- Every name passed to `child`, `read`, or `write_atomic` must pass
  `is_safe_component`, or be the name of an entry found by the identity scan, which
  excludes `/`, `\`, NUL, and `.`/`..` by construction. A path from the scan is
  re-opened one component at a time through `child(create=False)`, so it gets the same
  checks.
- earmark never deletes or renames anything in the vault except its own
  `.earmark-*.tmp` temp files older than one hour, in the directories it is about to
  write.

### 5.4 Finding a note again: identity by frontmatter

Each `store` call scans the subdir once. The scan:

- walks the subdir recursively with `scandir` on directory fds, depth at most 16;
- skips entries whose name starts with `.`, the top-level `Backups` directory, symlinks,
  and names that contain `\` or a control character, or that do not decode as strict
  UTF-8;
- reads regular files whose names end in `.md` (case-sensitive), first
  `SCAN_HEAD_BYTES` only, through `read`;
- records `note_identity(head)` → vault-relative path.

A directory it cannot list (`PermissionError`) is skipped with a WARNING log.

From the result:

- The **episode note** is the path recorded for `("episode", episode.key)`, else the
  candidate allocated by 5.2. When several files carry the same identity (sync conflict
  copies, a duplicated note), the code picks the one whose fold key equals the
  candidate's, if any. Otherwise it picks the smallest path by code point. It adds the
  warning `f"found {n} notes for episode {key}; updated {path}"`.
- The **transcript note** is found the same way with `("transcript", key)`.
- So a note keeps its path when the episode is retitled. When the user renames a note,
  or moves it anywhere inside the subdir except `Backups/`, the next store updates it in
  place, and the links between the two notes are regenerated from the real paths. If
  the user removes the note's frontmatter, earmark no longer recognises the note. The
  next store then creates a new one, with a suffix if the old name is still there.
- Cost: one open and a small read per `.md` file in the subdir. That is negligible for
  thousands of notes, next to a job that takes minutes.

### 5.5 Frontmatter: owned keys, seeded keys, user keys

Episode note, in this order. These are the **owned** keys:

| Key | Value |
|---|---|
| `earmark` | `"episode"` |
| `show` | `episode.show_title` |
| `episode` | `episode.title` |
| `date` | published date in `tz` (a YAML date), or `null` when `published_at` is `None` |
| `url` | `episode.url` |
| `guid` | `episode.guid` |
| `episode_key` | `episode.key` (quoted, so an all-digit key stays a string) |
| `duration` | `"HH:MM:SS"` (`format_hms`) of `transcript.duration_s` if > 0, else of `episode.duration_s`, else `null` |
| `prompts` | the non-default prompts of the analyses, newest first, whitespace-collapsed, each capped at 200 characters (199 + `…`); `[]` when none |
| `shared_at` | `shared_at` in `tz`, naive, `YYYY-MM-DDTHH:MM:SS` (5.6) |
| `model` | the newest analysis's `model`, or `null` with no analysis |
| `transcript` | `"[[<transcript note path without .md>]]"` when `wikilink_target_ok`, else `null` |

**Seeded** key: `tags` = `("earmark", "podcast")`.

Transcript note: owned `earmark` (`"transcript"`), `show`, `episode`, `date`, `url`,
`guid`, `episode_key`, `duration`, `language` (`transcript.language` or `null`), `model`
(`transcript.model`), and `episode_note` (`"[[<episode note path without .md>]]"` or
`null`). Seeded `tags` = `("earmark", "transcript")`.

Merge in `update_note`, on the existing frontmatter lines:

1. A **top-level key line** matches `^([A-Za-z_][A-Za-z0-9_-]*)[ \t]*:(?:[ \t]|\r?$)`.
2. An owned key line, together with its continuation lines, is dropped. Continuation
   lines are the following lines that start with a space or tab, or are `-` or start
   with `- `. That is how block lists look, both earmark's `  - "x"` and Obsidian's
   `- x` at column 0. Every owned key that appears is dropped, so a duplicate the user
   added is gone too.
3. All other lines are kept verbatim, in order: user keys, comments, blank lines,
   quoted keys, and any `tags` entry.
4. Output: `---\n` + `emit(owned)` + kept lines + `emit(seeds not present)` + `---\n`. A
   seed is present when a kept line is a top-level key line for that key. On the first
   write the seeds come last, so the second write (seed kept, nothing re-seeded) gives
   the same bytes.

So the user can add properties, which survive. They can edit `tags`, and their version
wins from then on. If they delete `tags`, it is seeded again. They cannot change owned
keys, which are earmark's managed frontmatter and are rewritten on every store. When
Obsidian re-serializes the frontmatter after a property edit (unquoting strings,
changing list indentation), the owned keys are still found by rule 1 and replaced.

`split_note`:

- An optional BOM is kept aside and written back.
- The first line, with `\r`, spaces, and tabs stripped from the right, must be `---`.
- The closing delimiter is the next such `---` line.
- Without both delimiters, `frontmatter` is `None` and every line is body.
- Lines split on `\n` only, with endings kept.

### 5.6 Dates and times

- Note date and `date`: the published instant in `tz`. Step 10's prompt uses
  `published_at.date()` in Apple's offset instead. The two differ only near midnight,
  and the note follows the architecture's `TZ` rule.
- `shared_at`: Obsidian's Date & time format has no offset (2.2), so the value is the
  first-share time in `tz`, without an offset. The exact instant stays in `shared.json`,
  and step 18 selects by that, not by the note.
- Per-analysis "Created" in the details callout: `created_at` in `tz`, `YYYY-MM-DD
  HH:MM`.

### 5.7 The managed region: markers, hand edits, damage

The region sits between `<!-- earmark:begin hash=<16 hex> -->` and `<!-- earmark:end
-->`. Each must be alone on its line; trailing `\r`, spaces, and tabs are ignored. The
hash is `region_digest` of the region text earmark wrote. In the body (not the
frontmatter):

- **Normal.** Exactly one begin line and exactly one end line, begin first: the text
  between them is replaced. Lines before the begin and after the end are kept byte for
  byte. A begin line without a hash, typed by the user, is accepted too.
- **Edited inside.** The old region's digest differs from the hash, or there is no
  hash, **and** the old region text (CRLF normalised) differs from the new one. The
  whole previous file is first copied verbatim to `Backups/<name>` in the subdir
  (`Backups` created with `child(create=True)`). The name is
  `f"{file_component(old_stem, max_bytes=MAX_NAME_BYTES - 22, fallback='note')}
  ({old_digest}).md"`, where `old_stem` is the note's current file stem and 22 is the
  length of `" (<16 hex>).md"`. If a file of that name exists (fold
  match), it is the same backup and is not rewritten. Then the region is replaced, with
  the warning `f"{note_path}: text inside the earmark markers was edited by hand and has
  been replaced; the previous version was saved as {backup_path}"`. A regenerated
  region whose digest matches is never "edited", so this happens once per hand edit.
  The identity scan skips `Backups/`, so a backup is never mistaken for the note.
- **Missing or damaged.** No marker at all, more than one begin or end line, or the end
  before the begin. The note is **not written** (frozen), and gets the warning
  `f"{note_path}: the earmark markers are missing or damaged, so the note was not
  updated; restore the <!-- earmark:begin --> and <!-- earmark:end --> lines, or delete
  the note to regenerate it"`. The returned paths still name it, so the job result and
  links point at the user's note. Deleting the markers is how a user takes over a note:
  earmark will never duplicate content or overwrite text it cannot tell apart from the
  user's.
- A frozen episode note does not stop the transcript note from being written, and the
  reverse holds too.
- Only marker lines in the body count. Generated content cannot contain one, because
  every untrusted text has `<` escaped.

After building the new text: if it equals the existing text, nothing is written (no
mtime change, no sync traffic). Otherwise `write_atomic(name, data,
expect=<stamp from the read>)`. On `NoteChanged`, the code re-reads, re-merges, and
retries, up to 3 attempts in all, then lets the `NoteChanged` (transient) propagate.
This narrows, but cannot close, the window in which an edit made in Obsidian between
earmark's read and its rename would be lost (section 9).

### 5.8 Note bodies

Episode region (`render_episode_region`). Blocks are joined by `"\n\n"`, and the
region ends with one `"\n"`. `E` is `escape_markdown`.

1. `f"# {E(title) or 'Untitled'}"`.
2. A meta line, with parts joined by `" · "`: `f"**Show:** {E(show)}"`, then
   `f"**Published:** {D}"` when `published_at` is known, then `f"**Duration:** {hms}"`
   when known (same rule as the `duration` key).
3. A links line, with parts joined by `" · "`: `f"[Apple Podcasts]({episode.url})"`
   only if the URL matches `https://[!-~]+` and has no `(`, `)`, `<`, or `>`; then
   `note_link(transcript_path, "Transcript")`. If both are absent, the block is left
   out.
4. For each analysis, newest first by `(created_at, prompt_hash)` descending:
   - `render_analysis(a, heading_level=2)` with its final `\n` stripped;
   - then a details callout block of lines joined by `"\n"`:
     `"> [!info]- Analysis details"`, `f"> - Model: {E(a.model)}"`,
     `f"> - Created: {created}"`, and one `f"> - {E(w)}"` per `a.warnings` entry, in
     order. The callout is folded by default (`-`). This is where map-reduce,
     fallback-model, and dropped-timestamp warnings reach the reader.
5. With no analyses: `"*No analysis yet.*"`.

Transcript region (`render_transcript_region`):

1. `f"# {E(title) or 'Untitled'} (transcript)"`.
2. Parts joined by `" · "`: `note_link(episode_path, "Episode note")`,
   `f"**Show:** {E(show)}"`, and `f"**Published:** {D}"` when known.
3. A callout: `"> [!info]- Transcript details"`, `f"> - Model: {E(model)}"`,
   `f"> - Language: {E(language)}"` when set, and `f"> - {E(w)}"` per transcript
   warning.
4. One block per segment: `f"[{format_hms(seg.start)}] {E(seg.text)}"`, or the bare
   marker when the text is empty. Paragraphs separated by blank lines render one line
   per segment.
5. With no segments: `"*The transcript is empty.*"`.

The episode region links to the transcript note's final path, and the transcript
region links back. Both paths are settled (5.4, 5.2) before either is rendered. The
transcript note is written first, so the episode note's link resolves as soon as the
note appears.

### 5.9 `store` step by step

1. Check that `transcript.episode_key` and every `analysis.episode_key` equal
   `episode.key`, else `PermanentError("store received data for another episode")`.
2. `with vault_lock(data_dir / "vault.lock", timeout_s=...)`. This serializes all
   earmark processes on the host, which share the data dir.
3. `vault.open_subdir()`. Scan identities (5.4).
4. Settle the episode note directory and name (5.4, 5.2). Settle the transcript note
   directory and name (5.4, 5.2).
5. `remove_stale_temps(now=self._now())` in both note directories.
6. Write the transcript note, then the episode note, each through the update procedure
   of 5.5 and 5.7. For a new file, `new_note` and `write_atomic(expect=None)`. For an
   existing file, `read`, `update_note`, an optional backup, then `write_atomic(expect=
   stamp)`. A file found by the scan must still carry the same identity when it is
   re-read under the lock; if not, it is treated as not found and a new name is
   allocated.
7. Return `StoreResult(note_path=..., transcript_note_path=..., warnings=(...))`. The
   warnings are in the order they arose, without duplicates.

Inputs to the contents are `episode`, `transcript`, `analyses`, `shared_at`, `tz`, the
subdir, and the paths on disk. No clock, randomness, locale, or environment is involved.
So two fresh vaults given the same inputs get byte-identical files, and a second store
into the same vault writes nothing.

### 5.10 Errors

`VaultStore.store` wraps its body:

- `SymlinkRefused`, `PermanentError`, and `TransientError` (the lock, `NoteChanged`,
  an unmounted vault) pass through unchanged.
- `PermissionError`, or `OSError` with `EROFS`, `EACCES`, `EPERM`, or `ENAMETOOLONG`:
  `PermanentError(f"cannot write to the vault (check EARMARK_VAULT_DIR permissions):
  {errno name} at {rel}")`.
- `ENOSPC` or `EDQUOT`: `TransientError(f"the vault is full: {errno name}")`.
- Any other `OSError`: `TransientError(f"vault write failed: {errno name or class name}")`.

Messages carry vault-relative paths and errno names only, never the absolute vault path,
note contents, or library text. `rel` is the deepest vault-relative path known when the
error happened.

## 6. Tests

All tests use `tmp_path`, no sockets, and `tz=UTC` unless they say otherwise. Helpers in
`tests/test_vault_notes.py`:

- `make_store(tmp_path, **kw) -> tuple[VaultStore, Path]` creates `tmp_path/"vault"`
  and returns the store with `data_dir=tmp_path/"data"`;
- `store_once(...)` stores `make_episode()`, `make_transcript()`, and
  `[make_analysis()]`, with `shared_at=T0`.

YAML is parsed with `yaml.safe_load` on the text between the first two `---\n` lines.
There are no external fixtures; everything is built with `tests/factories.py`.

### Brief acceptance criteria

| # | Criterion | Tests |
|---|---|---|
| A1 | Notes land at the architecture's paths. Filenames are sanitized, length-capped, keep Unicode, and collisions get a short key suffix. | `test_vault_notes.py::test_paths_match_layout`: the two paths of the 5.2 example exactly, both files exist, and `StoreResult` carries them. `::test_unknown_publish_date_uses_share_date`. `::test_date_uses_tz` (published `23:30-08:00`: `tz=UTC` gives the next day, `tz=ZoneInfo("America/Los_Angeles")` the same day). `::test_collision_gets_key_suffix`: a foreign `2026-01-02 Épisode 1 - «Tests».md` with no frontmatter gives `… ({key[:8]}).md`, the foreign file is byte-unchanged, and the transcript name carries the suffix. `::test_second_collision_uses_full_key`. `::test_collision_is_case_and_normalization_insensitive`, parametrized over an all-lower-case occupant and an NFD-spelled occupant: both give the suffix. `::test_show_dir_fold_match_reused`: an existing `le podcast ☕/` is used, and no second show directory appears. `test_vault_sanitize.py::test_file_component_cases`, parametrized over input, `max_bytes`, and expected: `../../x` → `-..-x`, `.hidden` → `hidden`, `..` → fallback, `a/b\c` → `a-b-c`, `Ep. 1: Intro` → `Ep. 1 - Intro`, `12:30` → `12-30`, `[Live] #42 ^x` → `(Live) 42 x`, `say "hi"` → `say 'hi'`, `What?*<>` → `What`, `100%% sure` → `100% sure`, `CON` → `CON_`, `con.txt` → `con_.txt`, `a\x00b‮c` → `a bc`, NFD `Café` → NFC, `trailing. ` → `trailing`, empty or whitespace → fallback, a lone surrogate → removed. `::test_file_component_keeps_unicode` (CJK, emoji, Arabic, and ZWJ sequences unchanged when they fit). `::test_file_component_byte_cap` (properties over random-ish inputs: ≤ `max_bytes`, `is_safe_component`, NFC, ends with `…` when cut). `::test_cut_never_splits_graphemes`: `é` as `e`+U+0301 at the boundary, `👨‍👩‍👧` at the boundary, `🇫🇷🇩🇪` cut between flags, and a skin-tone modifier; none of the outputs ends with a combining mark, ZWJ, modifier, or lone regional indicator. `::test_fold_key` (`AI` vs `ai`, NFC vs NFD, `ﬁ` vs `fi` equal; `a` vs `b` not). `::test_is_safe_component` |
| A2 | Frontmatter parses as YAML and carries the documented fields, including for titles containing colons, quotes, `---`, and newlines. | `test_vault_notes.py::test_frontmatter_fields`: parsed keys of the episode note, in order, equal `[earmark, show, episode, date, url, guid, episode_key, duration, prompts, shared_at, model, transcript, tags]`. Values: `date == datetime.date(2026, 1, 2)`, `shared_at == datetime(2026, 1, 1, 12, 0)` (naive, UTC tz), `duration == "00:02:05"`, `prompts == ["Quels articles sont cités ?"]`, `model == "fake-llm"`, `transcript == "[[<transcript path without .md>]]"`, `tags == ["earmark", "podcast"]`. `::test_transcript_frontmatter_fields`. `::test_hostile_titles_in_yaml`, parametrized over `Part 1: the "real" story`, `---`, `a\n---\nshow: evil`, `# comment`, `- item`, `yes`, `null`, `!!python/object:os.system`, `&a *a`, `{x: 1}`, `[a]`, `' quote`, `back\slash`, `tab\tand\x1bESC`, and ` \x85﻿`, as both the title and the show: the frontmatter parses, `episode`/`show` equal the input exactly, no extra key appears, and the body's first line is the begin marker. `test_vault_sanitize.py::test_yaml_quote_round_trip` (the 2.2 list through `yaml.safe_load` and `yaml.load(..., Loader=yaml.CSafeLoader)`; a lone surrogate gives U+FFFD; always one line). `test_vault_document.py::test_emit_frontmatter_shapes` (exact lines for str, None, date, naive datetime, an empty tuple, and a two-item tuple; an aware datetime and a bad key raise `ValueError`) |
| A3 | The body has one section per analysis, newest first, each showing its prompt, with timestamps as `[HH:MM:SS]`. | `test_vault_notes.py::test_sections_newest_first`: three analyses (default at `T0`, prompt A at `T0+1h`, prompt B at `T0+2h`); the region contains `render_analysis(B)`, `render_analysis(A)`, and `render_analysis(default)` (each without its final newline) in that order, each followed by its details callout. A's prompt quote `> …` is present. `## Default analysis` is present. `[00:00:03]` (a claim at 3 s) appears. `frontmatter.prompts == [B, A]`. `::test_details_callout_has_model_and_warnings` (`an analysis warning` and `Model: fake-llm` in the callout; warnings escaped). `::test_no_analyses_placeholder`. `::test_episode_region_exact`: the exact region text for one analysis, given in the test; it pins 5.8 |
| A4 | The transcript is a separate note, linked from the episode note. | (`\|` in this cell is table escaping for a plain `\|`.) `test_vault_notes.py::test_transcript_note_and_links`: the episode region contains `[[<transcript path without .md>\|Transcript]]`; the transcript region contains `[[<episode path without .md>\|Episode note]]`; each link target plus `.md` exists under the vault; the transcript body has `[00:00:00] Bonjour à tous` and `[00:01:05] Merci` as separate paragraphs. `::test_transcript_text_escaped` (segment text `$5 and $10 #ad [[x]] <b>` → `\$5 and \$10 \#ad \[\[x\]\] &lt;b>`). `::test_link_falls_back_to_markdown_link` (subdir `Notes#1`: the link is `[Transcript](Notes%231/Podcasts/…)` and the `transcript` property is `null`). `test_vault_sanitize.py::test_wikilink_target_ok` and `::test_escape_markdown` |
| A5 | Text the user adds outside the managed markers survives regeneration. | `test_vault_notes.py::test_user_text_survives`: after the first store, insert a paragraph between the frontmatter and the begin line, append text after the end line, add a property `rating: 5` and a comment line to the frontmatter, and change `tags` to `[mine]`. Store again with a second analysis: the inserted paragraph, the appended text, `rating: 5`, the comment, and `tags: [mine]` are byte-identical in place; the region has two sections; `episode` was rewritten. `::test_owned_keys_rewritten` (the user changed `episode:` to `"mine"` and added a duplicate `show:`: both are replaced, and one `show` key remains). `::test_obsidian_reformatted_frontmatter` (owned keys rewritten in plain style with `- x` lists at column 0 are still replaced; user keys kept). `::test_deleted_tags_reseeded`. `::test_crlf_and_invalid_utf8_user_text_preserved` (user text with `\r\n` and the bytes `b"\xff\xfe"` survives byte for byte). `test_vault_document.py::test_update_note_keeps_outside_lines` and `::test_split_note` (no frontmatter, an unclosed `---`, a BOM, `\r\n` delimiters) |
| A6 | Storing the same inputs twice yields byte-identical files. | `test_vault_notes.py::test_store_twice_identical`: the second store returns an equal `StoreResult`, every file's bytes and `st_mtime_ns` are unchanged (nothing written), and the directory listing is unchanged. `::test_two_vaults_identical`: two fresh vaults, same inputs, byte-identical trees. `::test_store_after_user_edit_outside_is_stable` (after a user edit outside the markers, storing twice gives the same bytes both times) |
| A7 | A hostile title such as `../../x` cannot produce a path outside the vault subdir. | `test_vault_notes.py::test_hostile_title_stays_inside`, parametrized over `../../x`, `/etc/passwd`, `..`, `.`, `.obsidian`, `a/../../b`, `\..\..\x`, `x\x00y`, `CON`, 1000 × `é`, and the same values as the show title: every file created under `tmp_path` is inside `vault/Earmark/` (walk `tmp_path`, excluding `data/`); no name starts with `.` except earmark's temp pattern, and none exist after the store; `StoreResult` paths start with `Earmark/Podcasts/`. `::test_symlinked_show_dir_refused`: `Podcasts/Le Podcast ☕` is a symlink to `tmp_path/"outside"`, and the result is `SymlinkRefused` (a `PermanentError`) with the relative path in the message; `outside/` stays empty. `::test_symlinked_subdir_refused` (`vault/Earmark` is a symlink). `::test_symlink_at_candidate_name_is_occupied`: the note name is a symlink to `outside/x.md`; the note gets the suffix, `outside/x.md` is never created, and the symlink is unchanged. `::test_scan_ignores_symlinked_notes` (a symlink inside the subdir to an outside file carrying this episode's identity is not used, and the outside file is unchanged). `::test_vault_dir_itself_may_be_a_symlink`. `test_vault_fs.py::test_child_refuses_symlink_and_file`, `::test_read_refuses_symlink_and_fifo` (`os.mkfifo`; returns quickly, no hang), `::test_write_atomic_never_follows` (a destination symlink raises `SymlinkRefused`, the target is untouched, no temp left) |
| A8 | Writes are atomic, and the note path is recorded in the job result. | `test_vault_fs.py::test_write_atomic_failure_leaves_old_content` (monkeypatch `earmark.vault.fs.os.fsync` to raise `OSError` on the first call: old content intact, no `.earmark-*.tmp` left; same for `os.replace`). `::test_write_atomic_mode_0644`. `::test_write_atomic_expect_mismatch` (`NoteChanged`; nothing replaced). `::test_remove_stale_temps` (an old temp is removed; a fresh temp and a user file `.x.tmp` are kept). `test_vault_notes.py::test_pipeline_records_note_paths`: `make_env(tmp_path, backends=replace(fakes.backends(), store=VaultStore(...)), fakes=fakes)`; enqueue a share and `run_once()`; the job is `done`, `job.result.note_path` and `transcript_note_path` equal the files' vault-relative paths, and both exist under `env.vault`. A second job with a new prompt gives the same `note_path` and two sections, newest first. A third job with the same prompt leaves the bytes unchanged. `::test_pipeline_store_warning_reaches_job` (frozen note: the job is `done`, and the warning is in `job.warnings`) |

### Added criteria

| # | Criterion | Tests |
|---|---|---|
| A9 | `vault` is built, configured, and light. | `test_factory.py::test_default_settings_list_all_unbuilt` updated: the expected unbuilt fields are `{"index_backend", "notify_backend"}`, and the error count is 2. `::test_vault_store_requires_vault_dir` (no `vault_dir`: `ConfigError` whose only line starts with `EARMARK_VAULT_DIR: required when EARMARK_STORE_BACKEND=vault`). `::test_vault_store_dir_must_exist` (missing → `does not exist`; a file → `is not a directory`; the error does not create the path). `::test_vault_store_built` (an all-fake setting with `store_backend="vault"` and an existing vault: `isinstance(b.store, VaultStore)`; nothing created under the vault or data dir by the build). `::test_build_worker_runs_a_job_with_vault` (`build_worker` with `store_backend="vault"`: done, note under `vault/Earmark/Podcasts/Fake Show/`). `test_vault_notes.py::test_vault_modules_import_light` (subprocess: importing `earmark.vault.notes` loads none of `fastapi`, `httpx2`, `anthropic`, `yaml`, `faster_whisper`, `lancedb`) |
| A10 | Notes are found by identity, not by title. | `test_vault_notes.py::test_retitled_episode_keeps_path` (same key, new title: same note path; the `episode` key and `#` heading show the new title). `::test_user_renamed_note_is_updated` (renamed and moved to `Earmark/Archive/x.md`: the next store updates it there, makes no new note, and the transcript link points at the moved path in both directions). `::test_duplicate_identity_warns` (a conflict copy with the same frontmatter: the candidate-named one is updated, the copy is byte-unchanged, and the warning names the count). `::test_backups_dir_not_scanned`. `test_vault_document.py::test_note_identity` (quoted, single-quoted, and plain values; a wrong kind; a non-hex key; identity outside the frontmatter ignored) |
| A11 | Hand edits inside the markers are backed up; damaged markers freeze the note. | `test_vault_notes.py::test_edit_inside_region_backed_up`: change one line in the region, then store a new analysis. `Earmark/Backups/<stem> (<digest>).md` holds the previous file byte for byte; the note has the new region; the warning names both paths. Storing again creates no second backup and adds no warning. `::test_missing_markers_freeze_note` (both markers deleted: the bytes are unchanged after the store, the warning text is as in 5.7, the result still names the path, and the transcript note is still updated). `::test_damaged_markers_freeze_note`, parametrized over two begin lines, an end before a begin, and only a begin. `::test_begin_without_hash_accepted` (the user-typed `<!-- earmark:begin -->`: the region is replaced; a backup happens only if the content differs). `test_vault_document.py::test_region_digest_crlf_insensitive` and `::test_marker_lines_with_trailing_whitespace` |
| A12 | Concurrent writers do not corrupt or lose edits. | `test_vault_notes.py::test_lock_timeout_is_transient` (hold `vault_lock` in the test, then call `store` with `lock_timeout_s=0.2`: `TransientError`, nothing written). `::test_user_edit_during_store_not_lost`: monkeypatch `earmark.vault.notes._after_read`, a module-level no-op hook called after each note read and before the write, so that it appends user text to the file once. The final file holds both the user text and the new region; `_after_read` ran twice for that note |
| A13 | Errors carry retry intent. | `test_vault_notes.py::test_vault_dir_gone_is_transient` (remove the vault dir after building the store). `::test_read_only_dir_is_permanent` (`chmod 0o555` on the show directory; `skipif(os.geteuid() == 0)`; the message names `EARMARK_VAULT_DIR` and contains no absolute path). `::test_disk_full_is_transient` (monkeypatch `os.write` in `earmark.vault.fs` to raise `OSError(ENOSPC)`). `::test_store_rejects_foreign_key` (an analysis for another episode: `PermanentError`, nothing written) |

`test_stages.py` already checks the Protocols with typed assignments. Add
`store: Store = VaultStore(...)` to it so mypy checks conformance.

## 7. Verification

```bash
cd /home/sandy/dev/earmark
make check
uv run pytest tests/test_vault_sanitize.py tests/test_vault_fs.py tests/test_vault_document.py \
  tests/test_vault_notes.py tests/test_factory.py tests/test_stages.py -v
# Nothing outside docs/, src/earmark/vault/, factory.py, and the tests changed
git status --short
# No new runtime dependency
git diff --stat pyproject.toml uv.lock   # expect: no changes
# Light import
uv run python -c 'import sys, earmark.vault.notes; print(sorted(m for m in ("yaml","fastapi","httpx2","anthropic") if m in sys.modules))'
# expect: []
```

Live smoke: every backend `fake` except `STORE_BACKEND=vault`. No network. Run it from
`$SCRATCH`, a fresh scratch directory.

```bash
cd "$SCRATCH"
E=/home/sandy/dev/earmark/.venv/bin/earmark
mkdir -p vault
export EARMARK_API_TOKEN=smoke-token-12 EARMARK_DATA_DIR="$SCRATCH/data" \
  EARMARK_VAULT_DIR="$SCRATCH/vault" EARMARK_PORT=18792 EARMARK_TZ=Europe/Paris
for v in RESOLVER_BACKEND FETCH_BACKEND ASR_BACKEND LLM_PROVIDER INDEX_BACKEND \
  NOTIFY_BACKEND; do export "EARMARK_$v=fake"; done
export EARMARK_STORE_BACKEND=vault
URL='https://podcasts.apple.com/us/podcast/x/id1200361736?i=1000802907981&uo=4'
$E submit "$URL" --prompt 'What papers are cited?'
$E worker --once                       # job <id> done
$E jobs --json | python3 -m json.tool | grep -E 'note_path|warnings'
find vault -type f | sort              # Earmark/Podcasts/Fake Show/2026-01-01 Fake episode ….md
                                       # and transcripts/… (transcript).md
cat vault/Earmark/Podcasts/Fake\ Show/*.md
/home/sandy/dev/earmark/.venv/bin/python -c 'import sys, yaml; t=open(sys.argv[1]).read(); print(yaml.safe_load(t.split("---\n")[1]))' vault/Earmark/Podcasts/Fake\ Show/*.md
sha256sum vault/Earmark/Podcasts/Fake\ Show/*.md > before.txt
$E submit "$URL" --prompt 'What papers are cited?'   # same prompt again
$E worker --once
sha256sum -c before.txt                # OK: unchanged
echo "my own note" >> vault/Earmark/Podcasts/Fake\ Show/*.md
$E submit "$URL"                       # default prompt: a second section
$E worker --once
grep -c '^## ' vault/Earmark/Podcasts/Fake\ Show/*.md   # 2 sections
tail -1 vault/Earmark/Podcasts/Fake\ Show/*.md          # my own note
# symlink refusal
rm -rf vault/Earmark/Podcasts/Fake\ Show && mkdir outside && ln -s "$SCRATCH/outside" "vault/Earmark/Podcasts/Fake Show"
$E submit "$URL" --prompt 'another'; $E worker --once   # job … failed: refusing … symlink …
ls outside                              # empty
```

The step-05 HTTP smoke (`serve` + `curl`) still applies with `STORE_BACKEND=fake`.
Verifiers may run it with `vault` too.

Cannot be verified in the sandbox: rendering in Obsidian, Obsidian's YAML parser,
macOS/APFS and iCloud behaviour (simulated with fold-equal names on ext4), and
`flock` across NFS. The data dir must be local anyway.

## 8. Deviations

1. **Frontmatter is partly managed.** CLAUDE.md says earmark writes "only inside its
   managed-region markers", but frontmatter must sit at the top of the file, outside
   them. The architecture already lists frontmatter fields that earmark writes. This
   plan makes the owned keys (5.5) earmark's managed frontmatter: they are rewritten on
   every store, while every other line, user properties included, is kept. `tags` is
   seeded once and then belongs to the user. The architecture's Notes paragraph says
   this now.
2. **The begin marker carries a digest:** `<!-- earmark:begin hash=<16 hex> -->`. It
   is needed to tell hand edits from changed inputs (5.7). The end marker is unchanged.
   A bare `<!-- earmark:begin -->` is still accepted. Steps 13 and 18 use the same
   functions.
3. **Policies the brief left open:** edits inside the markers are backed up to
   `<subdir>/Backups/` and replaced; damaged or missing markers freeze the note with a
   warning. Both are written into the architecture.
4. **Added frontmatter fields:** `earmark` (`episode`/`transcript`) and `transcript`
   on the episode note; the transcript note has its own set (5.5). `prompts` lists only
   non-default prompts, capped at 200 characters. `date` is `null` when the publish
   date is unknown, and the file name then uses the share date. `shared_at` is local
   time without an offset (Obsidian's format). `duration` is `HH:MM:SS`.
5. **Identity by frontmatter, no renames.** A note keeps its first path. A retitled
   episode is not renamed, so the user's links keep working. The architecture's path
   pattern describes where a *new* note goes.
6. **Two extra modules**, `vault/fs.py` and `vault/document.py`, next to the brief's
   `notes.py` and `sanitize.py`. They hold the symlink-safe filesystem layer and the
   note format, which steps 13 and 18 reuse.
7. **Analysis warnings are rendered** in a folded callout per section (step 10 left
   this to step 12).
8. **A host-wide lock** `<DATA_DIR>/vault.lock` (`flock`) serializes vault writes
   across worker processes. A 60 s wait gives a `TransientError`.
9. **No new dependency.** PyYAML stays dev-only, used to check the emitter's output
   (2.2).
10. `tests/test_factory.py::test_default_settings_list_all_unbuilt` changes its
    expectation, because `vault` is now built.

Changes made to `docs/ARCHITECTURE.md` in this planning step:

- the module layout (vault/);
- a new "Storing" subsection under Stage contracts;
- the data layout (vault tree, suffix rule, `Backups/`, `vault.lock`);
- the Notes paragraph (fields, markers, ownership, edit and damage policy, identity);
- the Security "Vault writes" bullet;
- the `STORE_BACKEND`, `VAULT_DIR`, and `TZ` rows.

## 9. Out of scope

- Entity notes and wikilinks to them (step 13). `render_analysis` keeps leaving
  `[[`/`]]` alone for that step.
- `obsidian://` links in notifications (step 15). `doctor` checks of vault writability
  (step 15).
- A CLI to regenerate every note from artifacts (`earmark notes rebuild`). Not in the
  brief. `VaultStore.store` is idempotent, so a later step can add it in a few lines.
- Escaping `$`, `%%`, `==`, and `#tag` inside `render_analysis` output (step 10's
  renderer). This step escapes only the text it renders itself.
- Deep links from analysis timestamps to transcript lines (`#^t…` block ids).
- Episode descriptions in the note.
- Windows as a worker host (the fd-based layer is POSIX). Vaults *synced* to Windows
  get Windows-safe names.
- Closing the last race window: an edit saved in Obsidian between earmark's final
  `stat` and its `rename` can still be lost. Obsidian's own sync and editors have the
  same window. Retries narrow it (5.7).
