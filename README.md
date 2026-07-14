# codex-kb

`codex-kb` is a local, cross-project knowledge base for Codex. It stores the
*why* of an implementation—purpose, background, and rationale—alongside source
paths, Git revisions, session IDs, and research links. Its database lives at
`~/.codex-kb/codex-kb.sqlite3` by default, independently of Codex's internal
state.

## What it records

- reusable implementation notes, decisions, research, incidents, and general notes;
- user value (`purpose`), motivating problem (`background`), and the selected
  approach (`rationale`) in searchable fields;
- optional session ID, repository, URL, observed/effective dates, and source
  artifacts (path, symbol, line range, commit, and a short non-secret excerpt).

Source code stays in Git. The database stores a pointer and optional short
excerpt rather than a second unversioned copy of the code.

## Install on Windows

Python 3.10+ is the only runtime dependency. From this repository, run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

The script copies the program under `~/.codex-kb/app`, creates
`~/.codex-kb/bin/codex-kb.cmd`, adds that directory to the user `PATH`, and
initializes the database. Open a new terminal afterwards.

```powershell
codex-kb status
```

To develop without installing, run `python .\codex-kb.py ...` from this
repository. Set `CODEX_KB_HOME` to use an isolated database for tests or demos.

## Record the why, not just the change

```powershell
codex-kb record `
  --kind implementation `
  --title "OAuth refresh token を単一フライト化" `
  --summary "同じ期限切れに対する重複更新を防ぐ" `
  --purpose "ユーザーのリクエスト失敗を減らす" `
  --background "複数タブが同時に更新し、片方のトークンが失効していた" `
  --rationale "サーバー側の再試行より、クライアントの共有 Promise が変更範囲を小さくできる" `
  --outcome "並行更新を1回に抑え、既存のリトライ仕様を維持" `
  --tag auth --tag oauth `
  --path src/auth/refresh.ts --symbol refreshToken
```

Use `--stdin-json` when a script or an agent is producing the record:

```powershell
'{"kind":"decision","title":"例","summary":"要約","purpose":"目的","background":"背景","rationale":"理由","tags":["example"]}' |
  codex-kb record --stdin-json
```

`observed_at` means when a fact was confirmed; `effective_from` means when a
decision or behavior applies. Keeping both prevents an ambiguous single date.

## Search

```powershell
codex-kb search "ログイン障害を減らしたい"
codex-kb search "OAuth refresh" --current-repo
codex-kb show 12
```

Search uses SQLite FTS5 plus a substring fallback. The fallback is important
for vague partial Japanese terms such as `ログイン` matching `ログイン障害` even
when SQLite's default tokenizer cannot segment Japanese text. Vector embeddings
are deliberately not required for the first version; they can be added as a
hybrid ranking stage once the stored records demonstrate a need.

## Codex integration

### 1. Expose search and recording as MCP tools

Append the content of [`examples/config.toml`](examples/config.toml) to
`~/.codex/config.toml`, then restart Codex. It provides:

- `search_knowledge` — read-only retrieval by problem, intent, or keyword;
- `get_knowledge` — full record and source paths;
- `record_knowledge` — saves reusable findings with purpose/background/rationale.

### 2. Record session metadata automatically

Merge [`examples/hooks.json`](examples/hooks.json) into `~/.codex/hooks.json`
or an equivalent trusted `config.toml` hook configuration. The hooks write only
stable metadata supplied by Codex: session ID, working directory, model, and
best-effort Git branch/commit. They intentionally do **not** parse the Codex
transcript, whose format is not a stable integration contract.

Codex will ask you to review and trust a newly configured command hook. Keep
the command scoped to `codex-kb`; do not install a hook that uploads prompts or
transcripts unless you have explicitly reviewed its data handling.

### 3. Make retrieval routine

Add [`examples/AGENTS.md.snippet`](examples/AGENTS.md.snippet) to global
`~/.codex/AGENTS.md` or to chosen repositories. It tells Codex to search before
non-trivial work and record reusable outcomes after completion.

## Safety and retention

Never use this database for secrets. Do not record `.env` contents, API keys,
tokens, passwords, private keys, customer data, or full raw transcripts. The
database is local but unencrypted SQLite; back it up or synchronize it only
through storage you trust.

## Test

```powershell
python -m unittest discover -s tests -v
```
