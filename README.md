# Codex CLI test project and AgentRelay

- `index.html`: Snake game used to exercise Codex development and publication.
- `agent-relay/`: Telegram relay with ChatGPT discussion, Codex CLI tasks, persistent group context, cancellation, verification and explicit human-authorized Git publication.

See [AgentRelay setup and configuration](agent-relay/README.md).
Run the relay from `agent-relay/` with `./start.ps1` on Windows.
Create your own `.env` from `.env.example`; for this directory layout set `CODEX_WORKDIR=..` to target this repository.
Local credentials, runtime state and conversation history are intentionally excluded.

AgentRelay attribution and license are preserved in [agent-relay/LICENSE](agent-relay/LICENSE).
