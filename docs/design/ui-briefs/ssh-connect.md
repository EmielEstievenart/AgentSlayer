# UI brief: SSH connect flow

Status: **design-forward brief, not yet agreed**. Unlike the other UI briefs,
this surface does not exist in the TUI as an in-app flow — today it is
launch-time CLI flags plus blocking terminal prompts (`cli.py:471-551`). This
document proposes what the pywebview GUI replaces it with. The TUI keeps its
current CLI-flag flow unchanged (see "Out of scope").

Binding constraints come from `docs/design/remote-ssh.md` ("remote-ssh.md"
below); this brief does not revisit any decision recorded there — it proposes
UI for the semantics that document already settled, and flags where the GUI
forces a new decision remote-ssh.md left open (§6).

## 1. Purpose

Today, going remote means: type `agentclip --ssh <target> --remote-root
<path>` and read stderr. The whole connect sequence — resolve target, dial,
authenticate (up to 3 password prompts via `getpass`), confirm an unknown host
key, probe the OS, check the remote root, capture environment, load remote
config — runs **before the TUI starts** (`cli.py:471-478`, design decision 7
in remote-ssh.md). Every failure is fatal: the process prints one line to
stderr and exits with code 2. There is no retry, no partial-progress UI, and
no path back into the flow short of re-running the command with different
flags.

Concretely, the current UX has these failure modes:

- **Any error aborts pre-UI.** A typo in `--remote-root`, a wrong password, a
  rejected host key, a target with no `.agentclip.toml` root configured — all
  of them exit the process. The user edits a shell command and re-runs the
  whole binary from scratch (`cli.py:496-497, 510-512, 517-519, 522-526`).
- **No retry within a step.** Password entry gets 3 attempts *inside*
  `SshHost._authenticate` (`ssh.py:424-443`, `_PASSWORD_ATTEMPTS = 3`), but a
  wrong host name, a dead network, or a bad `--remote-root` gets none — one
  shot, then the process exits.
- **Blocking terminal prompts.** `ask_password` uses `getpass.getpass`
  directly on the controlling terminal (`cli.py:403-408`); `confirm_host_key`
  uses `input()` (`cli.py:411-419`). Neither works once the TUI (or a GUI
  webview) owns the screen — this is explicitly why these callbacks exist as
  injectable functions in `SshHost.__init__` (`ssh.py:267-268`) rather than
  being hardcoded.
- **No 2FA path.** Keyboard-interactive authentication is "whatever paramiko's
  own fallback does" — remote-ssh.md "Auth, in practice" (line ~170) says
  plainly: "a dedicated prompt path for it was not built and is untested."
  Any target requiring keyboard-interactive 2FA has no supported UX today.
- **Saved targets require editing a TOML file by hand.** `[remote.<name>]`
  tables (`config.py:380-394`) are the only persistence for connection
  details; there is no UI for creating, editing, or browsing them.

The GUI removes the "abort and retype a shell command" cost from every one of
these: recoverable failures become retryable dialog states, secrets get an
in-app prompt that doesn't require a visible terminal, and saved targets get a
picker instead of a text file.

**What this brief does NOT change:** the connect sequence itself — order,
fatality, exit semantics at the protocol level, what "the target owns its
policy" means. Those are `SshHost`/`remote_launch` behavior and stay exactly
as specified. The GUI changes how a human interacts with that sequence, not
what the sequence does.

## 2. Current behavior contract (MUST be preserved)

The exact sequence in `remote_launch` (`cli.py:471-551`), which the GUI must
reproduce step for step — reordering is not a UX nicety here, it's the
design's ordering (`cli.py:472-478`, remote-ssh.md decision 7: "connect,
authenticate, probe `uname` and remote root existence *before* the TUI
starts"):

| # | Step | Source | Failure mode today | Exit code |
|---|------|--------|---------------------|-----------|
| 1 | Resolve target: `--ssh` value against `[remote.<name>]` saved targets, else `~/.ssh/config` alias, else `[user@]host[:port]` | `config.py:413-434` (`RemoteConfig.selected`) | target has no `root` and none was passed | `cli.py:492-497` → **2** |
| 2 | Parse `~/.ssh/config` for the resolved alias (host, user, port, IdentityFile) | `ssh.py:383-396` | silently falls back to literal alias/defaults on any read error (not fatal) | n/a |
| 3 | Dial + authenticate: agent keys → default keys → up to 3 password prompts | `ssh.py:398-443` | `SshError` (auth failed / unreachable) | `cli.py:510-512` → **2** |
| 3a | Unknown host key: SHA256 fingerprint confirm, never auto-added | `ssh.py:138-149`, `ssh.py:445-448` | declined → `SshError` from within connect | folds into step 3 → **2** |
| 4 | `probe_os()`: run `uname -s` | `ssh.py:452-461` | non-zero exit or empty output → `SshError` | `cli.py:510-512` → **2** |
| 5 | `realpath(remote_root, strict=True)` + `stat()` | `cli.py:515-527` | path doesn't resolve, or resolves but isn't a directory | `cli.py:517-519` / `522-526` → **2** |
| 6 | `home_dir()` — remote user's home, via SFTP normalize of `.` | `ssh.py:463-474` | never fails the launch; falls back to `/home/<user>` or `/root` | n/a (non-fatal) |
| 7 | `_remote_environment()` — `printenv` over `bash -lc`, parsed conservatively | `cli.py:446-468` | never fatal; empty environment + a stderr note if `printenv` answers unusably | n/a (non-fatal) |
| 8 | Second `load_config()` call, this time `host=`, `home=`, `environ=` set — reads the **remote** project's `.agentclip.toml`, permission ruleset, MCP config | `cli.py:536-546`, remote-ssh.md "Revision: the target owns its policy" | config load itself never raises (`load_config` "never raises on bad user config"); problems become `Config.warnings` | n/a |

Points that MUST hold in any GUI reproduction:

- **Steps 1–5 are all-or-nothing per remote-ssh.md line 478: "a half-connected
  session is not a thing."** The GUI may show progress through them, but must
  not let a partially-connected state be treated as usable — no tool call,
  no chat turn, until step 8 completes.
- **Order matters**: local config/flags select the target *before* any
  network call; the connection is fully live and the root verified *before*
  the remote `.agentclip.toml`/ruleset/MCP config is read (`cli.py:472-476`).
  This is what makes "the target owns its policy" (remote-ssh.md line 191)
  safe to implement — the ruleset that governs a tool call always comes from
  the actual connected target, never a guess made before dialing.
- **The password prompt gets exactly 3 attempts**, hardcoded
  (`ssh.py:68` `_PASSWORD_ATTEMPTS = 3`), and the password that works is
  cached in `SshHost._password` in memory for the life of the process, reused
  silently on reconnect (`ssh.py:290`, `ssh.py:420-422`).
  **The GUI must not add a 4th visible retry inside this step** without
  changing `SshHost` itself; if the GUI wants a different attempt count, that
  is a code change to `_PASSWORD_ATTEMPTS`, not a GUI-level retry loop around
  it (a GUI-level loop would call `connect()` again from scratch and pay the
  agent/key-lookup steps a second time, which is harmless but misleading, and
  would double-count `reconnects`).
- **Host key confirm must never auto-accept.** No "always trust" checkbox
  that persists without writing to `known_hosts` through the same path
  OpenSSH itself would use. `_AskPolicy.missing_host_key` (`ssh.py:141-149`)
  only proceeds past a decline by raising `SshError` — declining must
  terminate the connect attempt, not silently downgrade to unauthenticated
  browsing.
- **Every one of steps 1–5 failing is fatal to that connect attempt** (not
  necessarily to the GUI process — see §3 "error states with in-app retry").
  Today's exit code is always **2** for a validation/argparse-level failure or
  a `SshError`; the GUI has no process exit to signal, so it must translate
  "would have been exit 2" into a dialog error state, not a partial success.

## 3. Proposed GUI flow

### 3.1 Entry point

A "Connect to remote" action (menu item / button) opens the **Connect
dialog**, replacing the requirement to know CLI flags exist. Available both at
GUI launch (in place of "open a local project") and, per §4, as the single
action that also covers "switch target."

### 3.2 Saved-target picker

- List sourced from `[remote.<name>]` tables merged from global +
  (if a local project is already open) project config, i.e. exactly what
  `_load_remote` builds into `RemoteConfig.targets` (`config.py:825-850`) —
  reuse that reader; do not reimplement TOML parsing in the GUI.
- Each row shows: name, `user@host:port` (blank port omitted, matching
  `SshHost.target`'s own formatting, `ssh.py:303-308`), and the saved `root`
  if any.
- A second source, **not currently read by any AgentClip code**: `~/.ssh/config`
  aliases. `SshHost._read_ssh_config` (`ssh.py:383-396`) parses this file, but
  only for the alias the user already typed — nothing today *lists* aliases.
  The GUI listing them for the picker is new: parse `~/.ssh/config` with the
  same `paramiko.SSHConfig` machinery, list `Host` patterns that don't contain
  wildcards, and show them in a second section ("From ~/.ssh/config") with no
  saved `root` — selecting one drops the user straight into manual entry with
  host/user/port pre-filled and root empty. See §6 open question.
- Selecting a saved target pre-fills the manual-entry form below (§3.3); it
  does not connect immediately — this keeps one "Connect" action doing one
  thing (matches remote-ssh.md's general aversion to hidden multi-step
  actions, e.g. decision 4's "no mid-session switching").

### 3.3 Manual entry

Fields mirror `RemoteTarget` (`config.py:380-393`) plus the CLI's own
target-string grammar (`config.py:426-433`, `[user@]host[:port]`):

- **Target**: single combo field accepting `user@host:port`, a saved name, or
  a bare `~/.ssh/config` alias — same free-form grammar `--ssh` accepts today,
  so muscle memory and any copy-pasted value from a teammate's command line
  keeps working. Parse client-side using the identical `rpartition`/`partition`
  logic in `RemoteConfig.selected` (`config.py:426-433`) so the dialog's
  preview of "connecting as `user@host:port`" never disagrees with what the
  backend will actually resolve.
- **Remote root**: text field, required unless a saved target supplies one
  (`cli.py:491-497`). Validate only for non-emptiness client-side — real
  validation (does it exist, is it a directory) cannot happen until after
  connect (step 5 needs a live SFTP session), so the field's "invalid" state
  before connecting is just "empty when the target has no saved root."
  Post-connect validation failure is a retry state, not a form error (§3.5).
- No password field on this screen — password is asked only if key-based
  auth is refused, and only after a real dial is attempted, matching
  `_authenticate`'s try-agent-then-keys-then-password order (`ssh.py:407-422`).
  Asking for a password up front would contradict that order and would ask
  for a secret in cases (agent/key auth working) where none is needed.

### 3.4 Async connect with step-by-step progress

Drive the same sequence as §2's table, but off the UI thread (pywebview's
Python backend can run `remote_launch`'s steps and push progress via
whatever IPC the GUI shell uses — implementation detail out of scope for this
brief). Progress UI shows the 6 stages as a checklist, each transitioning
pending → active → done/failed:

1. **Connect** — dialing, resolving `~/.ssh/config`, agent/key auth attempts
   (`ssh.py:407-414`)
2. **Auth** — only shown as its own visible step if agent/key auth was
   refused and a password (or host-key confirm) is needed; otherwise step 1
   and 2 collapse into one "Connect" tick for the common key-based case, so
   the user isn't shown a step that took zero time and asked nothing
3. **Probe** — `uname -s` (`ssh.py:452-461`)
4. **Root check** — `realpath` + `stat` on the remote root (`cli.py:515-527`)
5. **Env capture** — `printenv` (`cli.py:446-468`) — always shown as
   succeeding even when it yields nothing (non-fatal per §2 row 7); a small
   "environment unavailable, `{env:...}` in MCP config will be blank" note
   inline rather than as an error state, since it never blocks connect
6. **Remote config load** — the second `load_config()` call
   (`cli.py:536-546`); surfaces `Config.warnings` (e.g. a rejected
   `[approval]` table in the remote project's `.agentclip.toml`,
   `config.py:896-901`) as non-blocking notices attached to this step, not as
   failures — `load_config` itself never raises

Each stage that can fail (1, 3, 4; 5/6 are non-fatal per the table) shows its
failure inline and halts the checklist there — later stages stay pending, not
skipped-with-a-checkmark. This directly answers the "no partial-progress UI"
complaint in §1 while preserving the "half-connected session is not a thing"
rule from §2: the checklist visualizes progress, but no tool/chat surface
unlocks until stage 6 finishes clean.

### 3.5 Password prompt dialog (3 attempts)

Modal, appears mid-checklist when key/agent auth is refused
(`ssh.py:415-431`). Must reproduce `_PASSWORD_ATTEMPTS = 3` exactly:

- Attempt counter visible ("Attempt 1 of 3") so a wrong password doesn't feel
  like an unbounded loop.
- Cancel/give-up on any attempt maps to `ask_password` returning `None`/empty
  today (`cli.py:403-408`, `ssh.py:429-430` "if not answer: break") — the
  callback contract is `PasswordPrompt = Callable[[str], str | None]` where
  `None`/empty means "give up" (`ssh.py:75`). A GUI Cancel button must return
  that same signal, not raise, so `_authenticate` falls through to its normal
  "authentication failed" `SshError` rather than crashing.
- On the 3rd wrong attempt, the dialog closes and the connect checklist shows
  stage 1/2 as failed with "authentication failed for `<target>`: ..." — the
  same message `SshError` carries today (`ssh.py:443`) — and offers **Retry**
  (see §3.7), not just Close.
- The password is held in memory only (`SshHost._password`, `ssh.py:290`),
  never written to disk by this dialog — matches existing behavior, worth
  stating explicitly since a GUI form makes "remember this" a tempting
  checkbox to add. Not proposed here (see §6).

### 3.6 Host-key fingerprint confirm dialog

Modal, appears when `_AskPolicy.missing_host_key` fires (`ssh.py:141-149`).
Must show exactly what OpenSSH itself would show, since that's the design
intent — "OpenSSH's own question, asked in OpenSSH's own words"
(`cli.py:412`):

- Hostname, key type, SHA256 fingerprint (`ssh.py:106-109` `_fingerprint`) —
  displayed as `SHA256:<base64>`, matching OpenSSH's own `ssh-keygen -lf`
  output format so a user who wants to verify it against another channel
  (`ssh-keyscan` on a known-good box, a fingerprint posted by the sysadmin)
  is comparing like with like.
- Accept/Reject. Accept → `client.get_host_keys().add(...)` runs exactly as
  today (`ssh.py:149`) and the connect proceeds; Reject → `SshError` raised
  from inside `connect()` (`ssh.py:148`), checklist stage 1 shows failed,
  offer Retry (which will show the same prompt again, since nothing was
  written to `known_hosts` on reject).
- No "don't ask again for this session" shortcut — there is exactly one
  connect per session (design decision 4: one session, one host), so there is
  no second occasion within a session for this prompt to repeat.

### 3.7 Keyboard-interactive / 2FA prompt dialog (NEW)

This has no existing implementation to mirror — remote-ssh.md is explicit
that "a dedicated prompt path for it was not built and is untested" (Auth, in
practice). Proposed design, since the GUI is the first surface that could
plausibly support it:

- Paramiko's `Transport.auth_interactive` (the underlying mechanism
  `client.connect()` would need to be told to try, or fail over to, when
  password auth is refused with the server offering `keyboard-interactive`)
  takes a **handler callback** that receives a title, instructions, and a
  list of `(prompt_text, echo: bool)` pairs, and must return one string per
  prompt. A TOTP/2FA challenge is exactly this shape: one prompt
  ("Verification code: "), `echo=False`.
- GUI dialog: render the server's title/instructions text verbatim (servers
  vary — some send nothing, some send a banner), then one input field per
  prompt tuple, masked when `echo=False`. Submit sends all answers as a list
  in order, matching paramiko's contract.
- This is a **code change**, not just a UI addition: `SshHost._authenticate`
  today never calls `client.connect(..., auth_interactive_handler=...)` or
  drops to `transport.auth_interactive` — it only tries
  `allow_agent`/`look_for_keys`, then password (`ssh.py:407-443`). Wiring
  keyboard-interactive support is out of scope for this brief (a UI document),
  but the brief records the shape so the eventual implementation and the
  dialog design agree on the callback contract up front rather than the
  dialog being built against a guess.
- Attempt/retry semantics for this path are an open question — see §6.

### 3.8 Error states with in-app retry (no relaunch)

Every fatal step in §2's table becomes a **retryable dialog state**, not a
process exit:

- **Retry** re-runs the failed step (and everything after it) without
  re-asking information that didn't change — e.g. a failed root check
  (stage 4) retries stages 4 onward with the same live connection, not a
  fresh dial; a failed dial (stage 1) retries from stage 1, since there is no
  connection yet to preserve.
- **Edit** takes the user back to the manual-entry form (§3.3) with the
  attempted values pre-filled, for fixing a typo'd root path or hostname —
  covers the single most common real failure (`cli.py:517-527`, root doesn't
  exist or isn't a directory) without re-typing everything.
- **Cancel** discards the in-progress `SshHost` (call `close()`,
  `ssh.py:321-323`) and returns to the picker (§3.2), same as today's process
  exit but without losing the GUI session itself.

### 3.9 Surfacing "target owns its policy"

remote-ssh.md line ~205-207 ("A target with no `opencode.json` behaves
exactly like a local machine with none... no fallback to the host PC's
file") plus the broader "the target owns the rules, the host owns the gate"
section is a real footgun if invisible: a user who has a carefully tuned
`~/.config/opencode/opencode.json` on their host PC will see **none of it**
apply the moment they connect remotely, silently, unless told.

Proposed surfacing, all sourced from what `load_config`/`Config.warnings`
already computes in stage 6 (§3.4) — no new backend logic needed for the
happy-path notice, only for wiring it to the dialog:

- A persistent, dismissible-per-session banner in the connect success screen
  (shown once, right after stage 6 completes, before the dialog closes):
  "Permissions and MCP servers for this session come from
  `<target>:~/.config/opencode/opencode.json`" (or the resolved
  `opencode_config` path) — using the target-qualified naming the remote-ssh.md
  "Consequences to handle" section already mandates for the TUI ("the
  permission-source string shown... must name the machine, not just the
  path", line ~291-292). The GUI reuses that same string, wherever it ends up
  being computed, rather than inventing a second rendering of the same fact.
- If the target has **no** `opencode.json` (legacy allowlist mode), the same
  banner instead reads "No permission ruleset found on `<target>`; falling
  back to the allowlist gate" — makes the absence a stated fact, not a silent
  default.
- The banner also names anything relegated to the host by "what stays on the
  host PC" (remote-ssh.md line ~257-260): "`[approval]` (mode, yolo, command
  rules) stays on this PC" — one line, so a user who *did* set `[approval]` in
  the remote project's `.agentclip.toml` and sees it ignored (per the warning
  at `config.py:896-901`) has an explanation surfaced in the GUI rather than
  buried in a warnings list they have to go find.
- Any refused stdio MCP server (remote-ssh.md "MCP stdio servers are not
  supported in a remote session") is listed by name in this same
  post-connect summary — today it only reaches the TUI's MCP status pane
  (`/mcp`, sidebar, statusbar count) with no toast (remote-ssh.md line
  ~249-255); the GUI's connect-summary screen is a natural place to surface
  it once, up front, rather than requiring the user to go find the MCP panel
  to discover a server silently didn't attach.

## 4. Mid-session semantics

- **Reconnect-transparently (remote-ssh.md decision 5).** A dropped link
  self-heals on the next operation using cached credentials, without
  prompting — "by then the terminal that could ask is underneath the TUI"
  (remote-ssh.md "Reconnect, in practice", line ~144-148). The GUI, being a
  standing surface rather than a terminal, *could* prompt mid-session — but
  this brief does not propose changing that behavior, only how it's shown:
  a small connection-status indicator (e.g. in a status bar) flips to
  "reconnecting to `<target>`..." when `SshHost.reconnects` increments, and
  back to normal once a command completes. No modal, no interruption — the
  existing design's whole point is silence unless something needs a human.
  If a re-dial itself needs a password (credentials changed on the target
  since launch), that's currently unhandled by any caller (the prompt
  callbacks are the caller's per remote-ssh.md line ~147, "a future UI can
  supply ones that work mid-session") — this is a real gap the GUI could
  close by supplying live prompt callbacks instead of the CLI's `getpass`
  ones, reusing the exact password dialog from §3.5. Flagged as open in §6.
- **Exit-255 "outcome unknown."** A command in flight when the link dies
  returns `CONNECTION_LOST_EXIT` (255) with a body stating the outcome is
  unknown — "it may have completed, half-completed, or still be running"
  (`ssh.py:26-32`, `ssh.py:240-249`). This is a **tool-result** the model
  sees, not a connection-dialog concern — the GUI's run/tool-output surface
  (whatever renders `run_command` results) must render this message verbatim
  and not decorate it with a false-confidence icon (no red "failed" X, since
  it may not have failed) — a neutral "connection interrupted" treatment,
  distinct from both success and ordinary failure. Out of scope for the
  connect dialog itself; noted here because the connect-status indicator from
  the previous bullet is the natural place a user would look to understand
  *why* a command came back this way.
- **No mid-session host switching (decision 4).** "Host-hopping = new
  session" (remote-ssh.md line ~43). The GUI does not offer a "switch target"
  action distinct from ending the session — instead, a single **"Reconnect
  to a different target"** action in the same menu as "Connect" does both
  steps as one: close the current `SshHost` (`ssh.py:321-323`), end the
  session (whatever the GUI's session-closing path is — likely the same one
  "close project" already uses), and immediately open the connect dialog
  (§3.1) pre-populated with nothing (a fresh manual-entry/picker), rather
  than the current target's values, since it's a genuinely new session, not
  an edit of the old one. This keeps the one-session-one-host invariant
  intact while giving the user a single click instead of two separate
  "disconnect" then "connect" actions to remember.

## 5. Out of scope

- **The TUI's connect flow stays exactly as built** — CLI flags
  (`--ssh`/`--remote-root`), blocking terminal prompts
  (`ask_password`/`confirm_host_key`, `cli.py:403-419`), fatal-exit-2 on any
  failure. This brief does not propose changing `cli.py`'s `remote_launch` or
  its callbacks; the GUI is an **additional** entry point (presumably calling
  the same `remote_launch`/`SshHost` machinery with GUI-native callbacks
  substituted for `ask_password`/`confirm_host_key`), not a replacement for
  the CLI one.
- **`[remote.<name>]` config-file editing.** The picker (§3.2) reads saved
  targets; this brief does not propose an in-app editor for writing new
  `[remote.<name>]` tables back to `config.toml`. A user who wants a target
  saved for next time still edits the TOML file by hand, or (§6) the GUI
  could offer "save this connection" as a follow-up — deferred.
- **Wiring keyboard-interactive/2FA into `SshHost._authenticate`.** §3.7
  designs the *dialog*; making `SshHost` actually call
  `auth_interactive`/pass an `auth_interactive_handler` to paramiko is a
  code change to `ssh.py`, not a UI concern, and is left for whoever
  implements this brief.
- **Live reconnect prompt callbacks.** Per §4, supplying `SshHost` with GUI
  callbacks that work *mid-session* (as opposed to only at initial connect)
  is flagged as a gap remote-ssh.md leaves for "a future UI" — this brief
  notes it but does not design the mid-session prompt dialog itself, since
  today no caller exercises that path and its trigger conditions (which
  credential change scenarios actually need a live prompt vs. silently
  failing the operation) aren't yet specified anywhere.
- **MCP HTTP-transport machine-mismatch errors** (remote-ssh.md "MCP
  transport stays on the host", the `localhost` footgun) — real, and the doc
  mandates the connection error "must say which machine dialed it," but that
  is an MCP-status-pane concern, not the connect dialog's.
- **Accelerated bulk-search / stdio-server-on-target** — both explicitly
  deferred in remote-ssh.md itself (decision 3's escape hatch; "MCP stdio
  servers are not supported," "a plausible later wave"), so out of scope
  here too.

## 6. Open questions

remote-ssh.md settles the backend semantics; it does not (and mostly should
not) settle GUI presentation. Where the GUI genuinely forces a decision the
binding doc doesn't make, listed here with a proposed default:

1. **Should a successful manual connection offer "save as `[remote.<name>]`"?**
   remote-ssh.md's config section only describes reading saved targets, never
   writing them from a session. *Proposed default:* yes — a checkbox/prompt
   in the connect-success screen ("Save this target as..."), writing to the
   **global** `config.toml` only (never the project's, since the project
   config lives on the remote box per decision 6/its revision, and a target
   definition is inherently a host-PC fact — it's how the host PC finds the
   target, not a project setting). Deferred from this brief's proposal (§5)
   but the default is worth recording so a future implementer doesn't have to
   re-derive it.
2. **Should the password ever be persisted (OS keychain), or is in-memory-only
   (`ssh.py:290`) the permanent answer for the GUI too?** Today's answer is
   "no persistence, ever" and that's a reasonable security default to keep.
   *Proposed default:* keep it in-memory only, matching current behavior
   exactly — do not add a "remember password" option. If this changes later,
   it is a security decision that deserves its own doc, not a line item here.
3. **Does `~/.ssh/config` alias-listing (§3.2) need to filter out
   `Host *`/wildcard/`Match` blocks more carefully than "skip anything with a
   `*`?** Paramiko's `SSHConfig.get_hostnames()` exists and returns exactly
   the set of literal patterns; using it rather than hand-rolling the filter
   avoids re-deriving paramiko's own notion of "which entries are real hosts."
   *Proposed default:* use `SSHConfig.get_hostnames() - {"*"}`, letting
   paramiko's own logic define "listable."
4. **What retry/attempt count applies to the keyboard-interactive dialog
   (§3.7)?** Password auth gets exactly 3 attempts by explicit constant
   (`ssh.py:68`); keyboard-interactive has no such precedent since it's
   unimplemented. A TOTP code is also time-boxed in a way a password isn't
   (a stale code fails even if correctly typed a moment later), which argues
   for a different retry shape than password's. *Proposed default:* mirror
   password's 3-attempt cap for consistency and to bound how long a hung
   connect dialog can stay open, but treat each attempt as a fresh
   `auth_interactive` round-trip (a new code prompt each time, not a retry of
   the same stale value) rather than reusing password's "hold the value and
   retry" shape.
5. **Does the mid-session reconnect status indicator (§4) need a manual
   "reconnect now" action**, or does it only ever trigger lazily on the next
   operation (as designed)? remote-ssh.md's model is purely lazy/reactive —
   nothing dials until something needs the connection. *Proposed default:*
   no manual reconnect button — adding one would be a second, redundant way
   to trigger the same `_ensure()` path (`ssh.py:339-348`) with no behavioral
   difference except impatience, and would invite the misconception that a
   dead-but-not-yet-noticed link can be proactively fixed rather than simply
   noticed on next use.
6. **Where does the "target owns its policy" banner (§3.9) live after the
   connect dialog closes** — is it purely a one-time toast at connect time,
   or does it need a persistent affordance (e.g. re-openable from a status
   bar) for a session that's been running a while and where the user forgot?
   *Proposed default:* both — the one-time banner at connect, plus the
   permission-source string (already mandated to be shown "in the TUI" per
   remote-ssh.md's "Consequences to handle" section) rendered in whatever the
   GUI's persistent per-session info panel turns out to be, so it's always
   one glance away without needing a toast history.
