# AgentRelay

Self-hosted Telegram bridge with two independent modes: **Codex CLI** for work
in a local Git checkout and **ChatGPT web** for ordinary questions. Discussion
continues the selected persistent ChatGPT chat and receives public Telegram context.
Codex requests use `codex exec` and the relay's explicit session ID.

## Telegram formatting

Telegram replies render ordinary Markdown locally: headings, bold/italic text,
lists, inline code, fenced code with language, links, strikethrough and spoilers.
The bridge sends plain text plus Telegram entities (not raw MarkdownV2/HTML),
so punctuation, shell commands and HTML-like code need no Telegram escaping.
Long answers and code blocks are split with UTF-16 entity offsets preserved;
the host-generated source label is repeated on each part. Unsupported/broken
markup stays readable text. No formatting model call or dependency is added.

## Two names, two modes

```dotenv
CODEX_ALIASES=Сударь
CHAT_ALIASES=чувак
DEFAULT_BACKEND=chat
CWA_CONVERSATION_ID=https://chatgpt.com/c/YOUR_CHAT_UUID
CWA_AUTH_FILE=auth_data.json
```

"Сударь, fix the calculator" uses Codex and the connected repository.
Publication runs after verification when explicitly requested by the human in ordinary language; `/publish <task-id> <branch>` remains an alternative.
"Чувак, explain percentages" continues the selected ChatGPT chat.
For a development request such as "Юки, добавь калькулятор", ChatGPT can return
a bounded internal task plan. The relay validates it and queues Codex under the
original human's identity in the same topic and configured repository. No second
name or repeated planning request is needed. Questions and idea discussions stay
in chat; ambiguous requests should receive a clarification. The host never treats
an ordinary mention of Codex, a prose answer or a bot reply as an internal task.
Malformed routing responses do not start work. Human `/task <instruction>` is
the direct path: Codex inspects and plans the implementation without a ChatGPT
planning round trip. Both paths retain independent verification, bounded
corrections, `/status`, `/cancel` and explicit human authorization for publication.
Names match whole words without case sensitivity. If both names
occur, the first one selects a single mode. An explicit name overrides Reply.

A Telegram Reply to the bot continues the mode of that answer. A bounded index
of message IDs and modes survives restarts in `STATE_FILE`, without message text.
A bare @mention, generic `BOT_ALIASES` or peer handoff uses `DEFAULT_BACKEND`.
No request is silently moved to Codex when ChatGPT fails.

Humans can select a persistent mode with `/discuss` (ChatGPT) or `/task`
(Codex), or use the buttons shown by `/mode`. Commands also accept
`@YourBotUsername`. `/discuss <question>` and `/task <instruction>` select the
mode and submit the text in one message. Preferences belong to the person who
sent the command or clicked the button, in that Telegram topic; participants
and topics do not change each other's modes. A group without topics uses one
scope per person. With ChatGPT configured, a new preference defaults to discussion;
a Codex-only installation uses `DEFAULT_BACKEND` until ChatGPT is configured.

For each request, an explicit `/discuss` or `/task` takes precedence over a
name, then Reply, then the saved mode. Names and Reply do not change the saved
mode. A selected mode still requires an addressed message; ambient group text
does not start work. Mode preferences select routing, not private AI sessions:
the Codex workspace/session and the selected ChatGPT discussion remain shared.
Only the selected ChatGPT chat is continued; other account conversations are not
read. Recent public messages from the current Telegram topic are added as context.

ChatGPT and Codex have separate serial queues. Discussion can proceed while
Codex runs, and only one Codex request modifies the configured workspace at a time.
Switching modes does not cancel an existing task. No extra ChatGPT worker is used.

## Tasks and status

Each accepted Codex request gets a durable task number. In the running bot a
human receives a queue acknowledgement, and each result includes its task number.
New instructions create separate tasks rather than silently changing running work.

- `/status [number]`: show your task status in this topic, including while paused.
- `/cancel [number]`: cancel your queued/running task without pausing discussion.
- Without a number, an active task is selected; multiple active tasks are listed
  so you can choose. `/status` shows the most recent task if none is active.
- Completed tasks cannot be cancelled or overwritten by a late worker response.

The registry supports queued, running, checking, completed, failed, cancelled
and interrupted states. `/status` also shows whether a human task is being
planned, executed by Codex, or has returned an executor report. The attempt
counter counts Codex dispatches, not planning requests.

## Task orchestration

Human coding tasks follow planning, execution and a bounded verification/correction cycle:

1. Automatic discussion handoff reuses the bounded plan from the current
   ChatGPT answer. Direct `/task <instruction>` builds a minimal work order
   locally, leaving repository inspection and implementation planning to Codex.
   Legacy name/mode-based Codex requests still use an isolated temporary ChatGPT
   planning turn with only the current request and up to 1200 characters of its
   explicitly quoted public Telegram reply. The relay validates every plan
   before starting Codex; it never copies the entire personal discussion.
2. The relay saves the plan and adds the operator-configured repository and
   mandatory scope/privacy rules. Planner output cannot change the working
   directory, grant publishing permissions or supply host commands to execute.
3. Codex receives the original human request alongside that work order. Its
   final response is requested through `--output-schema`, including when an
   existing CLI session is resumed. See the official
   [structured-output documentation](https://learn.chatgpt.com/docs/non-interactive-mode#create-structured-outputs-with-a-schema).
4. The relay validates and saves a structured report: summary, repository-relative
   changed files, check commands/status/exit codes, and remaining work. Telegram
   receives a readable report rather than raw JSON or tool logs. Blocked work
   stops without retries; executor-declared failing checks cannot bypass
   verification and feed the bounded correction cycle described below.
5. The task enters `checking`. A fresh, ephemeral Codex session reviews the
   actual source/diff against the original request, constraints and every saved
   acceptance criterion, and runs suitable existing checks in a **read-only**
   sandbox. It receives the structured work order/report, not private chat
   history or the coding session. Publishing is disabled, user configuration
   and execpolicy rules are skipped, and the project is explicitly untrusted
   for this invocation so project MCP/configuration cannot grant extra tools.
6. The relay compares the review's check commands/exit codes with actual CLI
   command events. Executor claims, missing events and skipped checks never
   become confirmed passes. Nonzero observed check exits cause failure, even
   when the reviewer claims success. The reviewer also assesses code/criteria;
   this remains model review, not a mathematical proof of correctness.
   Known shell argv wrappers are decoded before exact comparison, including
   CLI-rendered PowerShell apostrophes; this does not match command substrings
   or treat an echo of a test command as evidence that the test ran.
7. The relay fingerprints tracked and nonignored source files plus Git
   HEAD/branch/index immediately before and after review. A changed or unavailable
   snapshot prevents verified completion. Private/runtime files, ignored build
   output and unsupported/oversized repositories are excluded or reported as
   incomplete; symlinks/reparse points are not followed. No raw source/diff or
   command output is stored in the task registry.

Planning and ordinary discussion use the same chat backend and serial queue,
with a fresh temporary context for planning and the selected persistent chat for
discussion. The discussion's ChatGPT history is not passed to Codex. Discussion continues while
Codex executes. No additional ChatGPT Worker is added. If a
configured planner fails, Codex is not started and there is no automatic retry.
An explicitly Codex-only installation builds a minimal local work order from
the human request without requiring ChatGPT credentials. Existing peer-bot
conversation turns retain their direct transport and do not invoke the planner.

The executor report and independent review are stored separately. Telegram and
`/status` distinguish passed, failed, incomplete and unavailable verification.
Only a complete review with all criteria passed, at least one observed passing
check, no remaining work/issues and a stable snapshot completes a human coding
task successfully. Missing dependencies, blocked writes/network or manual checks
remain explicit gaps; the bot does not loosen sandbox permissions to run them.
`/status`, `/cancel`, `/stop` and parallel discussion remain available during
verification. Cancellation discards a late verification result.

Execution and verification schemas use the same bounds as the host parser,
including up to 8000 characters per check command. Multiline test scripts are
preserved in full; CLI-rendered command evidence has a separate 32000-character
bound to accommodate quoting. The verification schema bounds criterion count
and indices; the prompt and host require every task criterion exactly once.
Invalid reports fail closed with field/length diagnostics,
without copying raw responses into the log or re-executing coding work.
`tests/report_protocol_probe.py` is an opt-in real CLI regression in an isolated
temporary repo: it verifies seven criteria and a check command longer than
1000 characters. It uses the existing login and never publishes changes.

When a valid independent review identifies a failure, the relay sends its
specific findings and the previous execution report back to Codex. The original
request, repository, constraints and acceptance criteria stay fixed; planning is
not repeated. Each correction receives another fresh independent verification.
The maximum is **one initial execution plus two correction attempts**. A lower
stored per-task limit is respected; neither model output nor a higher stored
value can raise the hard cap of two corrections. The store atomically reserves
each execution before dispatch, and verification turns do not consume correction
attempts.

An executor report with `status: blocked` stops immediately without a verifier
turn or correction attempts. Its report and any partial changes remain saved,
verification stays `not_run`, and `/status` explains the blocker. Missing task
details, denied commands and unavailable dependencies need their prerequisite
resolved before a new human request; they are not treated as code regressions.

The cycle stops as soon as verification passes. After the second unsuccessful
correction, the final report and `/status` say that the correction limit was
exhausted, show the number used, and retain the unresolved findings. Missing or
malformed verification, incomplete/manual checks, an unavailable or changed
source snapshot, and a verifier using file-write/MCP tools stop the cycle without
dispatching speculative repairs. An execution timeout/provider error or malformed
execution report is not replayed: partial edits may remain. If a correction fails
this way, the prior report is clearly labelled as preceding that failed run.

Coding, correction and verification turns never receive a publishing tool.
Findings cannot grant publication or
expand scope. Report commands are data; the relay never executes model-provided
shell strings on the host. `/status`, `/cancel`, `/stop`, Ctrl+C and parallel
discussion work throughout correction and rechecking. A restart interrupts the
unfinished cycle without resuming it or resetting its attempt count.

This completes stage 6. The independent Codex pass uses the configured model,
effort and per-request timeout. A task uses at most three execution turns and
three verification turns, plus its initial planner request. No new `.env`
settings are required. CLI versions must support `--ephemeral`,
`--ignore-user-config`, `--ignore-rules` and `--output-schema`.

Task metadata, original task text, repository path, plans/reports and mode preferences are
stored locally in SQLite, never in Git. Execution/review reports and error codes
are retained separately for each attempt, together with the final stop reason.
Existing databases preserve the last available pre-history report on migration.
`TASK_STATE_FILE` overrides its path;
by default it is derived from `STATE_FILE` (for example,
`.agent-relay-state.tasks.sqlite3`). The registry does not store full group
transcripts, ChatGPT history or raw tool logs. Status replies show task metadata,
not original instructions or local paths. Do not publish the database.

After a crash/restart, unfinished records are marked interrupted and are never
automatically executed again. An orderly Ctrl+C cancels the active work and
discards queued requests, retaining their status records. Mode preferences and
task numbering survive restarts. `/resume` does not replay cancelled work.
Existing task databases are migrated in place without resetting tasks or modes.

The ChatGPT mode uses the previous unofficial `chatgpt-web-adapter`, your local
`auth_data.json`. Its authorization is
separate from `codex login`; no OpenAI API key is used. Enable it by configuring
the chat fields above. It requires Node.js 24+ and curl on PATH. The launcher
installs its optional Python dependency from `requirements-chat.txt` when needed.
The HTTPS server-clock compatibility fix is retained.

## Windows quick start

Install Python 3.10–3.15, Git and the [Codex CLI](https://developers.openai.com/codex/cli/).
AgentRelay finds `codex` on PATH, or the native CLI bundled with the Codex VS Code
extension. You can also set `CODEX_BIN` to its full path.

```powershell
codex login
git clone https://github.com/YOUR-NAME/YOUR-PROJECT.git ../your-project
Copy-Item .env.example .env
# Fill the Telegram settings and CODEX_WORKDIR in .env.
.\start.ps1
```

Run `codex login` under the same Windows account that runs the bot. If the CLI is
not on PATH, use its full path for login. `codex login status` checks the account.
The Codex mode reuses CLI authentication. The optional ChatGPT mode uses its
own `auth_data.json` and an existing chat selected by `CWA_CONVERSATION_ID`.
Both a full `https://chatgpt.com/c/<UUID>` link and a bare UUID are accepted,
including links to conversations inside a ChatGPT Project. Other hosts, share
links, query parameters and invalid IDs are rejected without echoing their contents.

### Change the selected ChatGPT chat (stages 3 and 4)

Stop the bot, replace `CWA_CONVERSATION_ID` in `.env` with the new full chat link,
then run `start.ps1` again. Normal discussion always uses the configured chat;
there is no automatic anonymous-chat fallback or automatic chat creation.
Changing the file while the server runs takes effect only after restart, so
queued/in-flight requests cannot silently switch destinations. You may open the
same selected chat in ChatGPT to see the bridge's discussion turns.

The bot retains up to eight successful public question/answer notes locally in
`CHAT_STATE_FILE` (default: `STATE_FILE` with a `.chat.sqlite3` suffix). Each note
contains a bounded author/topic/request/answer, not a copy of GPT history, tool
logs or the full Telegram transcript. On the first discussion request after a
chat change, a compact handover of public notes from the CURRENT topic is included in the SAME
request as the current message. Old quoted requests are not executed again.
After success, subsequent turns use the new chat without repeating the handover.
No extra GPT request is made to generate the notes or inspect the old chat.
Notes from other topics are filtered before composing a handover. The selected
GPT conversation itself remains shared by the group; this is not a separate
remote conversation per Telegram topic.

Keep the same `STATE_FILE`/`CHAT_STATE_FILE` when replacing a full or overloaded
chat. If no notes have been saved yet, the new chat starts with its own history
and the current public prompt; earlier unsaved discussions cannot be recovered
by this feature. A failed/cancelled/blocked response does not confirm a switch,
and uncertain remote writes are not automatically retried. Saving notes can
fail without replaying an already successful remote request.

The selected chat's existing history can influence public discussion replies,
so use a chat dedicated to this Telegram group. Known credentials remain
filtered. Planning keeps a separate temporary context and does not use these
handover notes. Stage 7 below adds a separate store for observed Telegram context.

### First human discussion after launch (stage 5)

Starting the server does not send a greeting or call ChatGPT. Its first addressed
human discussion request includes a brief service introduction in the SAME
ChatGPT request as the question and normal context. It reminds the selected chat
of the public Telegram role, bot/chat/Codex names, discussion versus coding,
history continuity, task-control boundaries and privacy. It asks for an answer
to the current question, not a separate greeting. Changing chats can add the
bounded handover notes to that same request, without an extra model turn.

After a nonempty response is confirmed and not cancelled, the introduction is
consumed for this server process. Following questions do not repeat it. A real
restart re-arms it; changing modes or `/stop` followed by `/resume` does not.
Peer-bot traffic, status/control commands and isolated Codex planning do not
consume the first human discussion introduction. Failed/cancelled/empty responses
do not confirm it; the next explicit human discussion request can carry it again.
There is no automatic retry of an uncertain write. If the remote reply succeeded
but local note saving or Telegram delivery failed, the introduction stays consumed.

Optional `CHAT_STARTUP_PROMPT` in `.env` adds your own one-line reminder (up to
2000 characters) to the built-in briefing. Empty uses only the built-in text.
Restart after changing it. The service introduction is not stored as the public
question in handover notes and is not sent to Codex. An operational
`chat_startup_intro_confirmed` event records confirmation and a Telegram message
ID, never the message or prompt contents. Already-running Python processes need
a restart to load this feature; do not launch another poller beside them.

Stage 5 validation on 2026-11-26: all 279 offline tests passed, including one
combined first request, later requests, real restart versus pause/resume,
isolated planning, peer turns, chat handover, failures/cancellation, custom
reminders and concurrent requests. Live Telegram checks are tracked separately;
offline test success does not claim delivery of a real group message.

A subsequent live check confirmed the first discussion request, a successful
ChatGPT response, `chat_startup_intro_confirmed` and a one-part Telegram reply.
The participant separately confirmed that an unaddressed message containing
`7319` was understood when the next message asked for that code. This verifies
the running version; the changes below require a restart to load.

### Telegram context before invocation (stage 6)

Allowed participants' ordinary public text is observed even without a bot name
or mention. It does not start a model request by itself. On an invocation,
ChatGPT receives up to `GROUP_CONTEXT_LIMIT` previous messages from that topic,
oldest first, with message IDs, authors and text in quoted JSON records. The
current question is placed after the transcript. A name-only invocation can
refer to the nearest relevant question; ambiguous references may still need
clarification. Current text does not reduce the previous-message budget.

Each topic has its own bounded buffer, so activity in another topic does not
consume its message budget. Memory is capped at 32 recently active topics;
the least recently updated topic is evicted when that cap is exceeded.
Each saved text is truncated to 600 characters. `GROUP_CONTEXT_LIMIT=0`
disables this local transcript. No media contents are downloaded or interpreted.

With a fresh cursor (first installation or a deliberately removed cursor file),
startup reads the pending Telegram queue in batches rather than discarding it
with a negative offset. Allowed public text warms the buffer. Old commands,
callbacks and mentions do not launch tasks, change modes, publish, or generate
replies during catch-up. New normal polling starts after those updates. An
existing cursor keeps its usual at-most-once processing behavior.

The same group/identity checks and credential redaction protect startup and
live context. Private chats, other groups, unconfigured senders and other
topics are excluded from the current transcript. Codex planning still receives
the explicit task and relevant Reply, rather than the ambient discussion.

Stage 7 below persists this bounded buffer across restarts. Telegram must also
deliver unaddressed messages: the bot needs to be a
group administrator or have privacy mode disabled. See the official
[Telegram Bots FAQ](https://core.telegram.org/bots/faq#what-messages-will-my-bot-get)
and [getUpdates reference](https://core.telegram.org/bots/api#getupdates).

Stage 6 validation on 2026-11-26: 285 of 286 tests passed in the full run.
The process-tree cancellation test timed out under restricted permissions;
its isolated rerun with permission to terminate its own temporary processes
passed in 0.476 seconds. All seven new context tests passed, including a
name-only invocation after an unaddressed request, topic isolation, exact
history limits, structured quotes, bounded memory, multi-batch startup catch-up
and the prohibition on replaying old commands. No live publication was performed.

### Telegram context across restarts (stage 7)

Context is now saved automatically to `GROUP_CONTEXT_FILE`, defaulting to
`STATE_FILE` with a `.context.sqlite3` suffix. With the default cursor this is
`.agent-relay-state.context.sqlite3`. No new `.env` setting is required. To use
another location, set `GROUP_CONTEXT_FILE=private/context.sqlite3` and restart.
The filename must end in `.sqlite3`; SQLite databases and sidecars are ignored
by Git and rejected by the host publisher and task-report path checks.

Keep this file across launches and when replacing `CWA_CONVERSATION_ID` with a
new ChatGPT chat. On startup the relay loads the saved context without starting
a model request or replaying old tasks, commands or replies. The first new
invocation includes the restored messages from its own topic. A change of GPT
chat preserves this group context independently of the eight exchange notes
in `CHAT_STATE_FILE`; no old GPT conversation is copied into this database.

Snapshots are separated by numeric bot account ID and Telegram group ID, then
by topic. Each scope retains at most 32 recently updated topics. Each topic
stores up to `GROUP_CONTEXT_LIMIT + 1` entries (the extra slot is for the current
invocation); prompts receive at most `GROUP_CONTEXT_LIMIT` previous entries.
Texts are bounded to 600 characters and names to 64. Delivered bot replies are
retained too. This is a recent-context buffer, not a complete group archive.

Only allowed public text enters the store, after credential redaction. On each
load the current human/peer allowlists are applied again, removed senders are
pruned, and known credentials are redacted again. Actor IDs and peer usernames
are stored locally for those checks and are not added to the model transcript.
Private chats, other groups and service startup prompts are not copied into
the current scope. Codex planning still uses the explicit task and relevant
Reply rather than this ambient transcript.

Ordinary text is committed before its Telegram cursor advances. If saving
fails, polling stops without acknowledging that update or starting its model
request. A crash between the history commit and cursor commit can redeliver
the update; its message ID deduplicates the context entry. The cursor still
advances before generation, preserving the existing at-most-once task policy.
A failure to save a bot reply after successful Telegram delivery is logged;
it does not resend the answer or mark completed coding work failed.

`GROUP_CONTEXT_LIMIT=0` disables reads and writes of this history and does not
create a new database. An existing file is retained; re-enabling restores its
previous saved buffer. Reducing the limit trims saved entries on the next
launch. An unreadable database stops startup with a safe diagnostic and is
not silently deleted or reset. To start with empty context while retaining a
backup, stop the bot and choose a new `GROUP_CONTEXT_FILE`.

The update starts collecting durable ambient context only after the bot is
restarted with this code. Earlier messages held only in the old process's RAM
cannot be imported automatically, and acknowledged Telegram history cannot be
downloaded again. New pending updates and newly observed text are handled
normally. Messages while `/stop` is active are not collected, as before.

Stage 7 validation on 2026-11-26: all 302 tests passed in one full run,
including 16 new history tests. They cover restart restoration, topic/group/bot
isolation, own replies, allowlist changes, redaction on input and restore,
bounded storage, disabled history, custom paths, corrupt state, failed writes,
cursor interruption, deduplication, and delivered replies without retransmission.
Tests used temporary databases and fake Telegram/model transports. The full
run had permission to terminate its own test process tree; no real Telegram
messages, model requests or external publication were performed by these tests.

### Context restoration acceptance (stage 8)

The final acceptance scenario runs the relay in separate fresh Python
interpreters, reads a temporary `.env`, keeps real SQLite/cursor files between
processes, and exercises the normal ChatClient request builder with fake
external transports. It verifies a pending queue without executing its old
commands, an unaddressed message before invocation, process restart, selected
chat replacement, one combined first-turn introduction/handover/question,
subsequent turns without repeated introduction, isolated planning, mode scopes,
pause/resume, failed chat requests without fallback, and source labels.

Acceptance found and fixed a handover defect: successful exchange notes from
other topics could enter the first request after a GPT-chat change. Handover
notes are now filtered by the current topic, matching the ambient transcript.
The new test covers both ordinary host calls and actual interpreter restarts.

| Check | Evidence |
| --- | --- |
| Recent Telegram text is visible before the first invocation | Automated coverage; participant confirmed the live `7319` response. |
| Ambient context survives a real server restart | Participant sent `8426` without addressing the bot, restarted, then received `8426`; a read-only database check found the marker and the log confirmed a delivered ChatGPT reply. |
| Selected chat, replacement and one first-turn briefing | Real local process restarts with fake transports; exact conversation ID and `temporary=False` checked. |
| Excluded canaries and known credentials absent from newly composed prompts/forwarded responses | Persistence, handover, planning and response-boundary tests; synthetic data only. This does not isolate history already held by the shared remote GPT chat. |
| Failed discussion does not become an anonymous chat or a coding task | Explicit failure and subsequent manual request tested, with zero Codex/publication calls. |
| Status, cancellation, correction limit and explicit publication | Existing regression scenarios, including local process termination and local bare-Git publication/rejection. |

The working-user check confirmed Codex CLI login, `gpt-6-sol` with medium
effort, and the configured `MirriMmann/test-codex-cli` checkout on `main` with
explicit publication enabled. Sandbox-only identity checks are insufficient:
Git may reject a checkout as owned by another SID, and the CLI may not see
the working user's login. These checks did not change Git ownership exceptions,
authentication, `.env`, the connected project, or any GitHub permissions.
Real GitHub publication is not claimed: acceptance pushes use local temporary
bare repositories and confirm their exact branch and commit.

To use the result:

1. Run `start.ps1` in the relay folder; stop with Ctrl+C.
2. Use a configured chat name (for example `юки`) or `/discuss <question>` for discussion;
   explicit development requests to Yuki can be handed to Codex internally.
   Use `/task <goal>` for the direct path without ChatGPT planning. The old
   Codex name (for example `Сударь`) remains supported for compatibility.
3. Keep `STATE_FILE`, `CHAT_STATE_FILE` and `GROUP_CONTEXT_FILE` between launches.
   The last 30 messages per topic are the default context budget, not full history.
4. Replace `CWA_CONVERSATION_ID` while stopped and restart when a new GPT chat is needed.
5. Use `/status`, `/cancel` and `/stop` as appropriate. Publication requires the
   explicit human publication request after verification, in ordinary language or via `/publish`.

The handover-topic fix requires a restart of a process started before this
acceptance run. The live `8426` result proves restart persistence; a real new
GPT chat was not created or substituted just for the tests.

Stage 8 result on 2026-11-26: all 306 tests passed in 211.776 seconds. Four new
tests cover the topic-handover regression and three process-level acceptance
scenarios. The live restart restored one saved message before the invocation,
then confirmed one introduction and one delivered ChatGPT reply, with zero
failed requests in that launch. Both the original human marker and bot reply
were found by a read-only check without printing their message contents.

Earlier stages 3/4 validation on 2026-11-26: all 269 offline tests passed, including chat-link
parsing, persistent discussion/isolated planning, handover, restart, failures,
cancellation and parallel discussion. A separate authenticated read-only check
confirmed access to the selected chat without sending a message. The bot was
left stopped; no live reply or external publication was performed for this check.

`start.ps1` creates or reuses `.venv`, checks Codex and its login, then starts the
bot. Codex mode uses only the Python standard library. ChatGPT mode also installs
its optional adapter when enabled. The launcher does not download Node.js.
`start.ps1 --setup-only` checks Python and the CLI without starting Telegram or
requiring login.

Ctrl+C cancels active generation and exits without a confirmation prompt.
Use `start.ps1` in PowerShell to avoid CMD's built-in batch termination question.
`start.cmd` remains available for compatibility and no longer pauses after errors.

Manual launch from this folder also works:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe agent_relay.py
```

## Local settings

Create a bot with [@BotFather](https://t.me/BotFather), add it to your group and
configure `.env`. Keep this file and all authentication files out of Git.

| Setting | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | Your Telegram bot token. |
| `TELEGRAM_CHAT_ID` | Numeric ID of the one allowed group. Private chats are ignored. |
| `ALLOWED_USER_IDS` | Comma-separated numeric IDs of humans allowed to invoke and control the bot. |
| `CODEX_WORKDIR` | Existing local Git checkout. Relative paths are resolved from the bot folder on startup. |
| `CODEX_BIN` | Optional CLI executable path; blank enables automatic discovery. |
| `CODEX_MODEL` | Optional model; blank uses the built-in Codex default. |
| `CODEX_REASONING_EFFORT` | Optional effort supported by the chosen model; blank uses the Codex default. |
| `CODEX_SANDBOX` | `workspace-write` by default; `read-only` is also supported. |
| `CODEX_NETWORK_ACCESS` | `false` by default. Set `true` to permit network requests during development, including dependency installation. Independent verification stays offline. |
| `CODEX_ALLOW_PUSH` | `true` allows host publication explicitly requested by a human in ordinary language or via `/publish`; default `false`. |
| `CODEX_TIMEOUT_SECONDS` | Maximum duration of one request, default 1800 seconds. |
| `CODEX_SESSION_FILE` | Local session pointer, default `.agent-relay-codex-session.json`. |
| `TASK_STATE_FILE` | Optional local SQLite task/mode registry; empty derives the path from `STATE_FILE`. |
| `PROJECT_STATE_FILE` | Optional project metadata SQLite database; empty derives `*.projects.sqlite3` from `STATE_FILE`. |
| `CODEX_ALIASES` | Names that explicitly select Codex, e.g. `Сударь`. |
| `CHAT_ALIASES` | Names that explicitly select ChatGPT, e.g. `чувак`. |
| `DEFAULT_BACKEND` | `chat` or `codex` for generic addressing. Defaults to `codex` for existing installations. |
| `CWA_CONVERSATION_ID` | Persistent ChatGPT chat UUID or full chat link. Changing it requires a restart. |
| `CHAT_STATE_FILE` | Private SQLite file for bounded public handover notes; empty derives `*.chat.sqlite3` from `STATE_FILE`. |
| `CHAT_STARTUP_PROMPT` | Optional one-line reminder added to the first human discussion after each launch; up to 2000 characters. |
| `CWA_AUTH_FILE` | ChatGPT web authorization file, default `auth_data.json`. |
| `CWA_MODEL` / `CWA_REASONING_EFFORT` | ChatGPT settings, independent of the Codex model and effort. Blank uses adapter defaults. |
| `CHAT_TIMEOUT_SECONDS` | ChatGPT request time limit, default 180 seconds. |
| `BOT_ALIASES` | Comma-separated words/names that address this bot. |
| `PEER_BOT_USERNAMES` / `PEER_BOT_IDS` | Both allowlists are required to trust another bot. |
| `MIN_INTERVAL_SECONDS` | Minimum interval between request starts, default 2 seconds. |
| `GROUP_CONTEXT_LIMIT` | Previous trusted public messages per topic, default 30; 0 disables reading/writing history. Each group/bot keeps at most 32 recently active topics. |
| `GROUP_CONTEXT_FILE` | Private SQLite Telegram context snapshots; empty derives `*.context.sqlite3` from `STATE_FILE`. |
| `STATE_FILE` | Telegram update cursor; retained across restarts to avoid duplicate requests. |

For example, a sibling checkout can be connected with
`CODEX_WORKDIR=../test-codex-cli`. Changing the workspace starts a separate Codex
session. To start a fresh session in the same workspace, stop the bot and choose
a new `CODEX_SESSION_FILE`.

Codex runs without interactive approval prompts, within the selected sandbox.
Use `workspace-write` for coding requests or `read-only` for code review. Complete
any Windows sandbox setup requested by an interactive `codex` launch first.
The relay passes sandbox/cwd settings in the `exec` scope, repeats explicit
mode/approval overrides for resumed turns, and selects `windows.sandbox="elevated"`
on Windows. This is the Windows sandbox implementation, not full host access.
That setting must be explicit because `--ignore-user-config` also skips the
Windows sandbox choice in your personal Codex configuration; otherwise shell
commands may be rejected before execution even when login succeeds.
An optional real CLI probe is available with
`.\.venv\Scripts\python.exe tests\codex_permission_probe.py`. It uses the current
Codex login/model, creates an isolated repo under `.runtime`, checks new/resumed
file writes and independent read-only verification, then removes only its own
temporary directory. It never resumes the bot session or publishes changes.
The relay instructs Codex to keep credentials private and never commit, push or
publish itself. Publication is handled by the host after an explicit human request. Work done
before a cancellation, including file edits, is not rolled back.

### Publish to GitHub

Set `CODEX_ALLOW_PUSH=true` with `CODEX_SANDBOX=workspace-write`, then restart.
Ask e.g. “Юки, добавь змейку и сделай push”: the relay develops, verifies and
publishes the exact reviewed changes to the verified branch without a second command.
“Юки, сделай push” publishes your latest task in this group/topic without redeveloping it.
If that task is running, the request is saved until work with files stops.
A standalone push request publishes the configured repository's current Git state;
it does not need a previous coding task, a list of edits from Codex, or a successful
model report about the wording of the request. Existing source edits are committed,
clean local commits are pushed without an extra commit, and an already published
HEAD is confirmed without an empty commit. This operation checks Git/file safety,
not functional correctness; its report never claims functional tests were run.
Fresh development-plus-publication requests still use independent coding checks.
An explicit branch or GitHub URL must match the configured checkout's destination.
`/publish 12 main` and `/publish@YourBot` remain supported alternatives.
Plain coding requests, quoted examples, planner output and peer bots do not
authorize publication. Normal Codex requests explicitly disable the old
`agent_relay_git` MCP server, including resumed sessions.
All model turns skip user configuration and execpolicy rules, mark project
configuration untrusted and disallow extra writable roots. Shell network access
during development follows `CODEX_NETWORK_ACCESS`; enable it for dependency
downloads. Verification remains read-only and offline. Network-enabled coding
commands can reach external services; commit/push must still follow the host
publication contract. Set model/effort in `.env`;
custom CLI profiles/configuration are not loaded. Saved CLI authentication is
still used. See the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Before execution, the relay records the Git baseline. After successful verification
it seals the changed-file list, source fingerprint, HEAD, branch and HTTPS GitHub
origin. The actual file changes must match the report. Files already dirty before
the task cannot be silently included, and old tasks without this baseline require
a new task/check. The final report includes the exact publication command when
its manifest is eligible and publication was not already requested; otherwise it explains what prevents publication.

When a human explicitly asks for a separate task branch and the checkout is on
clean `main` or `master`, the relay creates `agentrelay/task-<id>` before Codex
starts and records that branch in the baseline. Codex works in the selected branch
without modifying `.git`; its sandbox cannot create branches. If another working
branch is already selected, the relay uses it. A dirty default branch blocks
automatic switching so existing changes are not carried across by surprise.

The host publication worker rechecks the manifest, checkout, files, index and
destination before staging. Only exact files are staged, including reviewed
deletions. Unrelated staged files, credentials, symlinks, custom Git filters,
changed destinations and stale snapshots are rejected. Repository hooks are not
executed by the host publisher; tests/checks belong to the verification stage.
Staged blob contents and
the created commit's tree/parent are checked. The target remote branch must still
point at the task's original HEAD; unreviewed local ancestors are not swept into
a push. A new empty repository/branch can start from an unborn HEAD.

The host pushes a pinned commit to the exact branch without force, extra tags,
remote changes or branch checkout. It then reads that remote branch with
`git ls-remote` and requires the full commit hash to match before reporting
success. The result contains repository, branch and full commit hash. A push exit
code alone is not proof. If push or confirmation fails, publication is explicitly
unconfirmed; a local commit/index or even a remote push may remain. There is no
automatic retry or rollback. Inspect/reconcile Git and reverify before another
explicit request when the baseline has changed.

Publication shares the serial coding queue; discussion and Telegram command
polling continue. `/status 12` includes publication state, `/cancel 12` cancels
queued/in-progress publication, and `/stop`/Ctrl+C stop its own process tree.
Stopping cannot retract a commit/push already performed. After restart, queued
publication is cancelled and in-progress publication is marked unknown, never
replayed. `/report 12` retrieves the saved concise task report without another
model request; full structured reports remain local. This completes stage 7.

Git uses the current Windows account's existing Git Credential Manager login.
If authentication is missing, sign in with Git Credential Manager and issue a new
explicit publication command after checking the baseline. ChatGPT sign-in and
GitHub sign-in are separate.
Publishing does not force-push, change remotes or delete branches. Secrets and
unrelated staged files are rejected. Failed pushes are not retried automatically.
Pushing code updates the repository; website hosting is a separate deployment step.

## Addressing and handoffs

### Project foundation (part 1)

Project metadata is stored in a separate private SQLite database. This first
part provides documents, round records, limits and an ordered event journal.
It does not yet schedule project agents or automatically turn plans into coding
tasks. Existing `/task`, verification and publication keep their own lifecycle.

```text
/project new Build a Guitar Pro import/export adapter
/project spec 1 Import GP7, preserve tuning and capo, verify export by reimport
/project plan 1 Implement adapter, add round-trip fixtures, document limitations
/project status 1
/project events 1
/project limits 1 rounds=3 agent_calls=12 corrections=6
/project member 1 123456789
/project pause 1
/project resume 1
/project cancel 1
```

`/project status` selects the latest project accessible to that person in the
current topic. `/project spec 1` and `/project plan 1` show the latest document.
The owner may add another allowed human using their numeric Telegram user ID;
membership never grants access from other groups or topics. Peers cannot invoke
these human controls.

Document revisions are immutable, carry SHA-256 hashes and use optimistic
version checks. Plans reference the specification version they were written for;
changing the specification requires a new plan before the next round. A round
pins both versions and the plan hash. Its host state progresses through planning,
agreement, execution, review and integration, then completion or failure. Active
rounds prevent document replacement. Completing a round makes the project ready
for another round; completing the project is a separate host decision.

Default limits are 10 rounds, 40 agent calls, 20 corrections, 30 discussion turns
and 86,400 elapsed seconds. The wall-clock limit starts with the first reserved
operation and includes pauses and restarts. Counters reserve work before launch;
they are not token measurements or billed currency. New project consumers must
reserve a budget entry with a unique operation key. Repeating the same key does
not charge twice; concurrent reservations cannot overspend the last slot. Limits
are recorded with events in the same transaction. Exceeding a limit puts the
project in `limited`; an explicit limit increase and resume are required.

Paused, waiting-for-input, budget-limited, completed and cancelled projects have
distinct persisted states. Opening the database does not resume work. Commands
for metadata remain available while the bot is stopped, but creating or resuming
a project requires `/resume` first. Project-level execution/cancellation wiring
and automatic recovery will be connected with the later scheduling parts.
The journal contains document identifiers/hashes and state changes rather than
full chat transcripts. Known credentials are rejected at the storage boundary;
the Telegram input boundary also redacts them. Database files are excluded from Git.

Allowed humans can address the bot by `@username`, a word in `BOT_ALIASES`, a
command addressed to the bot, or a Telegram reply to its message. Trusted peers
can use mentions, addressed commands and aliases; a bare reply from a bot does
not trigger generation. Aliases match whole words without case sensitivity.

Recent trusted messages from the current topic provide context but do not themselves trigger a
request. The relay passes a bounded snapshot of those messages to Codex when an
authorized human starts a coding task, including agreements made with peer bots.
Only the human's current request authorizes work. Codex can append `[[handoff:OtherBotUsername]]`; the relay converts an
allowed peer marker into `/relay@OtherBotUsername`. A peer response can hand off
again, including back to its sender. The relay has no fixed handoff count limit.
Telegram must actually deliver the peer's messages to the bot for this to work.

## Stop and resume

An allowed human in the configured group can send:

- `/stop` or `/stop@YourBotUsername`: cancel the current local generation process tree,
  clear queued requests and pause responses, aliases and peer handoffs.
- `/resume` or `/resume@YourBotUsername`: accept new requests again.
- `/stoprelay`: alias for `/stop`.

Polling continues while either mode runs. Long responses are sent one part at a time
with command polling between parts. `/stop` suppresses unsent output; messages
already posted and file changes already made remain. It does not stop unrelated
Codex sessions. Cancellation also terminates local ChatGPT curl/Node workers;
it cannot retract messages or roll back work already done remotely.
The next request tells Codex that an unfinished previous task
was interrupted and must not be continued automatically. Pause state resets when
the bot restarts.

## Errors and delivery

Only one process can poll a bot token. A local OS lock blocks duplicate launches.
Telegram HTTP 409 means a competing poller or webhook; stop other copies, including
ones running on other machines. HTTP 429 polling errors respect `retry_after`.

Updates are recorded before generation. Failed or interrupted Codex turns and
Telegram sends are never replayed automatically; send a new request when ready.
CLI errors show safe diagnostics without raw tool output, credentials or private
logs. Only Codex's final answer is posted to Telegram.

For login failures run `codex login`; for account limits check your Codex usage.
For workspace errors check `CODEX_WORKDIR` and `git status` in that directory.

Use exact model IDs such as `gpt-6-sol`, not display names such as
`GPT-6 Sol`. For example, set `CODEX_MODEL=gpt-6-sol` and
`CODEX_REASONING_EFFORT=medium`, then restart the bot.
The current installation and `.env.example` select GPT-6 Sol with medium effort.
ChatGPT discussion uses its separate `CWA_MODEL` setting.

### Context restoration audit: stages 1 and 2 (2026-11-26)

The upstream `feat/telegram-cwa-mvp` implementation was inspected directly;
no pull, merge or replacement of this local implementation was performed.
Upstream ordinary discussion uses the configured persistent ChatGPT conversation
and buffers trusted ambient Telegram messages before invocation. Its startup
catch-up drains available pending updates into context without replaying old
tasks. At that audit, the local implementation used temporary ChatGPT turns
and a bounded, topic-scoped RAM buffer. Stages 3 and 4 now restore persistent
discussion and bounded handover notes. Stage 5 adds the per-launch introduction;
stage 6 improves topic buffers and first-launch catch-up. Stage 7 adds bounded
durable Telegram context, separated by bot account, group and topic.

The branch restrictions differ. Upstream's
[GitHub worker](https://github.com/kymuco/agent-relay/blob/feat/telegram-cwa-mvp/github_work.py)
requests exactly `agentrelay/work-<task-id>` and checks that its report names that
branch; it does not authorize pushing the default branch. This is one branch per
task, not one permanent branch for the whole project. The local Codex/host
publisher uses the branch captured at task admission and sealed after review;
the explicit `/publish` branch must match it, and a later branch/HEAD/origin
change blocks publication. There is no separate permanent allowed-branch setting.
The connected `test-codex-cli` checkout is currently on `main`; the relay's own
checkout is on `feat/telegram-cwa-mvp`. These are distinct repositories.
Application checks do not establish GitHub account/connector permissions or
server-side branch rules. No such external write permissions were changed.

The active `.env` was switched to `CODEX_MODEL=gpt-6-sol` with
`CODEX_REASONING_EFFORT=medium`. A live CLI request accepted those settings and
returned `SOL_MODEL_OK` in a fresh ephemeral read-only temporary repository,
without tool calls. The connected checkout and saved coding session were not
used for this check, and the Telegram bot was left stopped.

A bounded local event log records accepted updates, Codex tool stages, safe
errors and successful Telegram deliveries. It excludes message text, answers,
command lines and credentials. Watch it from a second PowerShell:

```powershell
Get-Content .agent-relay-events.jsonl -Tail 20 -Wait
```

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Tests use fake Telegram and CLI transports; they do not contact services or use
your credentials. Process cancellation tests launch a local child process and
need permission to terminate that process tree on Windows.

### Acceptance coverage (stage 8)

Acceptance run on 2026-11-26: **258 tests passed**. The bot was left stopped;
no external publication or Telegram message was sent by these tests.

The suite checks personal/topic mode switching and buttons, successful work,
deliberately failing checks, the initial execution plus at most two corrections,
status/cancellation during planning/execution/review/correction/publication,
parallel discussion, restart without replay, and publication success/refusals.
`tests/test_acceptance.py` also runs a complete router/orchestrator lifecycle:
an actual failing Python calculator check, a correction and passing recheck,
explicit publication through real Git to a temporary local bare repository,
remote branch/hash confirmation, then restart and report retrieval. Wrong-branch
and wrong-owner requests are refused before invoking the publisher. Other Git
tests cover rejected pushes and missing confirmation. No real GitHub push is
performed by the suite.

Privacy tests use synthetic canaries. Private Telegram updates and other topics
must not enter a request. Human coding tasks receive their current request and
up to 16,000 characters of recent trusted messages from the same public topic.
Temporary planning
requests contain no personal conversation ID and set the adapter's
`history_and_training_disabled` flag; normal discussion continues the selected
persistent ChatGPT chat. Legacy Codex session pointers without the
new context version start fresh once, so old planning history is not resumed.

The host removes known long configured credential values (12+ characters) and recognizable token/key
formats from incoming context before dispatch/storage. Credential-bearing model
responses and verifier command evidence are blocked before reports are saved or
passed to another agent. Saved legacy reports are redacted on Telegram delivery.
Authorization values are loaded locally for matching, never logged or added to
model requests. Worker environments exclude Telegram/CWA credentials that they
do not need. The local registry still contains task requests and reports; it is
private runtime data and must not be committed or shared.

These checks do not prove semantic secrecy for every arbitrary/encoded secret,
personal detail, malicious repository/test or compromised model. Source files
needed for development are visible to Codex; keep unrelated credentials outside
the connected checkout. External Telegram delivery, live ChatGPT/Codex account
availability and actual GitHub authentication are separate live smoke checks;
the offline acceptance suite does not claim to have exercised them.

CLI integration follows the official
[non-interactive mode documentation](https://developers.openai.com/codex/noninteractive/).
