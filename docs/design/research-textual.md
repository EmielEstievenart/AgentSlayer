# Textual Capability Brief for AgentClip (verified June 2026)

## 1. Version, Python, extras
- Current stable: **Textual 8.2.7** (released 2026-05-19). Active release cadence (~weekly); pin `textual>=8.2,<9`.
- Python `>=3.9,<4.0` supported; AgentClip's 3.11+ target is fine. Note: the `syntax` extra requires **Python 3.10+** since Textual 5.0 (tree-sitter dependency).
- Extras:
  - `textual[syntax]` — installs `tree-sitter` + `tree-sitter-languages` (binary wheels) for **TextArea** syntax highlighting only. **Not needed for AgentClip**: diff/code coloring in the transcript uses Rich's `Syntax` (pygments), which ships with Textual's `rich` dependency. Skipping it also helps PyInstaller (see §7).
  - `textual-dev` (separate package) — dev console (`textual console`, `textual run --dev`); dev dependency only.
- Core deps are pure Python (rich, markdown-it-py, typing-extensions, platformdirs) — PyInstaller-friendly.

## 2. Widget choices

**(a) Transcript panel** — two viable designs:
- **Recommended: `VerticalScroll` container with one mounted widget per message** (`Markdown` for LLM prose, `Static` holding a Rich `Syntax` for diffs/code, `Collapsible` to fold raw tool-call payloads). This is the pattern real chat TUIs (Elia) use; gives per-message styling, collapsing, and focus. Keep pinned-to-bottom with `widget.anchor()` — **since Textual 4.0 `anchor()` means "anchor scroll to bottom while content streams; release when user scrolls up"**, exactly the transcript behavior you want.
- Simpler fallback: `RichLog(markup=True, highlight=False, auto_scroll=True, max_lines=N, wrap=True)`; `RichLog.write(content, scroll_end=None)` accepts strings or any Rich renderable (Syntax, Table, Panel). Loses per-message interactivity.
- Rejected: `Log` (plain text only, `write_line()`; for raw command output at most), `ListView` (selection-list semantics, awkward for mixed-height rich content), bare `Markdown` as whole transcript (one document, can't intermix non-markdown widgets).
- For streaming LLM prose into a `Markdown` widget: `Markdown.append(fragment)` and `Markdown.get_stream()` returning a **`MarkdownStream`** (batches updates; plain `append()` lags above ~20 appends/sec). Added in Textual 5.0.
- Bonus: Textual 8.2.x has built-in mouse text selection with auto-scroll across widgets — users can copy from the transcript natively.

**(b) Colored unified diffs** — `rich.syntax.Syntax(diff_text, "diff", theme="ansi_dark")` (pygments `diff`/`udiff` lexer), wrapped in a `Static` (set via `Static.update(...)`) or written to `RichLog`. No extras needed. Markdown ```` ```diff ```` fences also highlight but add markdown-parsing overhead and escaping hazards — use `Syntax` directly.

**(c) Approve/reject modals** — `class ApproveScreen(ModalScreen[bool])`; child calls `self.dismiss(True/False)`. Two consumption patterns:
- Callback: `self.push_screen(ApproveScreen(), check_result)`.
- Linear (recommended for AgentClip's sequential approval flow): `result = await self.push_screen_wait(ApproveScreen(...))` — **must be called from a `@work` worker**, which fits naturally since tool-call execution should be a worker anyway. New in-modal bindings (y/n/a) live on the ModalScreen's `BINDINGS`; ModalScreen blocks bindings of screens below it.

**(d) Settings form** — `Input` (with `validators=[Number(), Function(...)]`, `Input.Changed/Submitted`), `Switch` (`Switch.Changed`), `Select[str]((label, value) pairs)` — note **`Select.BLANK` was renamed `Select.NULL` in 8.0** — plus `RadioSet`, `Checkbox`, laid out in a `Vertical`/`Grid` on a separate `Screen`.

**(e) Status bar** — `Footer` only renders key bindings; it is not a general status bar. Use **both**: `Footer` for key hints (integrates with dynamic-binding dimming, §4) plus a custom `Horizontal` (or `Static` children) docked via CSS `dock: bottom; height: 1;`, with fields driven by reactives (watcher updates label text). This is the standard pattern.

**(f) Multi-line task input** — `TextArea(soft_wrap=True, tab_behavior="focus")`; `TextArea.code_editor()` classmethod if you want line numbers/indent behavior. **TextArea has no Submitted message** — add a binding (e.g. `ctrl+enter` or `ctrl+s`) on the widget/screen to submit. Events: `TextArea.Changed`, `TextArea.SelectionChanged`. `read_only=True` available.

## 3. Workers / threading (clipboard poller)
- `@work(thread=True)` decorator or `self.run_worker(callable, thread=True, exclusive=False, group="clipboard", exit_on_error=True)` → returns `Worker`.
- Inside the thread loop: `from textual.worker import get_current_worker`; poll until `worker.is_cancelled` (threads aren't force-cancelled; you must check).
- Posting back to the UI thread: **`self.post_message(MyMessage(...))` is documented thread-safe** — preferred for "clipboard payload detected" events. `self.app.call_from_thread(fn, ...)` for synchronous calls that must run on the event loop (blocks the worker until done). Never touch widgets/reactives directly from the thread.
- `exclusive=True` is the right flag for one-shot workers you restart (not for the long-lived poller). `worker.cancel()` to stop the poller; clean shutdown via `on_unmount`/`App.workers.cancel_all()`.

## 4. Key bindings, dynamic bindings
- `BINDINGS = [Binding("y", "approve", "Approve", show=True, key_display="y", priority=False, tooltip="...")]` on App/Screen/Widget. `priority=True` bindings are checked before the focused widget's.
- **Dynamic enable/disable**: implement `def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None` on the App/Screen. Return `True` = show key + allow; `False` = hide key + block; `None` = show **dimmed** + block. Footer reflects this automatically.
- Trigger re-evaluation with `self.refresh_bindings()`, or declare the driving state as `reactive(..., bindings=True)` so binding refresh is automatic. For AgentClip: `pending_approval: reactive[bool] = reactive(False, bindings=True)` and gate y/n/a in `check_action`. (Alternative: put approvals in a ModalScreen, where the bindings only exist while pushed — simpler.)
- Default quit changed: **`ctrl+q` quits since Textual 2.0** (`ctrl+c` is reserved for copy). Command palette is `ctrl+p`.

## 5. Windows specifics
- Official docs: "The new Windows Terminal runs Textual apps beautifully"; legacy conhost is explicitly called limited. On Windows 11 (your target) Windows Terminal is the default host, so this is the supported path; document "use Windows Terminal" and degrade gracefully (it still runs in conhost, with worse color and width bugs).
- Emoji: conhost/openconsole have known emoji width bugs (microsoft/terminal#17342 — wrong cell widths, cursor misposition). Even in Windows Terminal, stick to narrow BMP symbols (`●`, `▶`, `✓`, `…`, box-drawing) for status-bar/spinner chrome rather than multi-codepoint emoji; Rich's width tables can disagree with the renderer for newer emoji and corrupt layout.
- No special terminal settings required for Windows Terminal; truecolor and mouse work out of the box. 8.2.7 added Kitty keyboard-protocol support (irrelevant on WT, harmless).
- Clipboard: Textual's `App.copy_to_clipboard()` is OSC-52 **write-only**; AgentClip's read-and-write polling needs its own library (`pyperclip` uses native win32 — fine and PyInstaller-safe).

## 6. Testing
- `async with app.run_test(size=(100, 40)) as pilot:` — headless mode; `Pilot` methods: `pilot.press("y", "ctrl+enter")`, `pilot.click(selector_or_offset, times=2, control=True)`, `pilot.hover()`, `await pilot.pause(delay=...)` (flush message queue — needed after `post_message` from fake clipboard events).
- Tests are async: use `pytest-asyncio` (set `asyncio_mode = auto`).
- Visual regression: `pytest-textual-snapshot` plugin, `snap_compare(app_path, press=[...], terminal_size=(w,h), run_before=async_fn)` producing SVG snapshots.
- Architecture note: keep the clipboard poller injectable so tests post the "payload detected" Message directly instead of touching the real clipboard.

## 7. PyInstaller
- Textual is pure Python and works, but **`textual.widgets` lazy-loads submodules via module `__getattr__`**, so PyInstaller misses them (documented failures like missing `textual.widgets._tab_pane`). Fix with a custom hook: `hiddenimports = collect_submodules("textual.widgets")` (or the whole `textual` package), via `--additional-hooks-dir`.
- Bundle `.tcss` files with `--add-data "src/agentclip/app.tcss:agentclip/"` and resolve `CSS_PATH` relative to `__file__` (works with PyInstaller's extraction dir). Embedding CSS in the `CSS` class variable avoids the issue entirely.
- Avoid `textual[syntax]` in the frozen build (tree-sitter native libs complicate onefile); pygments-based `Syntax` rendering needs nothing extra.
- Community fallback if PyInstaller fights back: Nuitka `--onefile` is reported to work out of the box for Textual apps.

## 8. API changes that invalidate older tutorial code (old → new)
- `TextLog` → **`RichLog`** (0.32, 2023) — any tutorial using `TextLog` is stale.
- `App.dark = True` / dark-mode toggle → **theme system: `self.theme = "textual-dark"`** (0.86, Nov 2024).
- `@work` on a plain function without `thread=True` → error; **must write `@work(thread=True)`** (0.31+).
- Default quit `ctrl+c` → **`ctrl+q`** (2.0, Feb 2025); command palette `ctrl+\` → **`ctrl+p`** (0.77).
- `OptionList`: `Separator` objects → use **`None`** entries (2.0).
- `App.query` now queries the **default screen**, not the active screen (3.0) — use `screen.query` for screen-scoped queries.
- Widgets parse **Textual Content markup**, not Rich console markup, in Button/Tabs/etc. (3.0 content system).
- `Widget.anchor()` semantics changed to "pin scroll to bottom" (4.0).
- `Markdown.code_dark_theme/code_light_theme/code_indent_guides` removed; markdown component classes moved to `MarkdownBlock` (5.0).
- `Static.renderable` → **`Static.content`**; `Label(renderable=...)` → `Label(content=...)` (6.0).
- `Select.BLANK` → **`Select.NULL`** (8.0); `push_screen` gained a `mode` argument (8.0).
- Ancient-tutorial red flags: `Message(self, sender)` constructors, `post_message_no_wait`, `app.dark` watchers — all gone.

Sources: [PyPI textual](https://pypi.org/project/textual/), [CHANGELOG](https://github.com/Textualize/textual/blob/main/CHANGELOG.md), [Workers guide](https://textual.textualize.io/guide/workers/), [Testing guide](https://textual.textualize.io/guide/testing/), [Actions guide](https://textual.textualize.io/guide/actions/), [Screens guide](https://textual.textualize.io/guide/screens/), [Input guide](https://textual.textualize.io/guide/input/), [Getting started](https://textual.textualize.io/getting_started/), [Markdown widget](https://textual.textualize.io/widgets/markdown/), [RichLog widget](https://textual.textualize.io/widgets/rich_log/), [TextArea widget](https://textual.textualize.io/widgets/text_area/), [FAQ](https://textual.textualize.io/FAQ/), [PyInstaller discussion #4512](https://github.com/Textualize/textual/discussions/4512), [PyInstaller hooks docs](https://pyinstaller.org/en/stable/hooks.html), [conhost emoji widths #17342](https://github.com/microsoft/terminal/issues/17342), [Markdown streaming PR #5950](https://github.com/Textualize/textual/pull/5950)