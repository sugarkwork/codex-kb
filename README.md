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

## Update a record

Use `show` first, then replace the record with its current complete state.
`update` intentionally replaces the artifact list as well as the descriptive
fields, so it cannot silently preserve stale paths or rationale.

```powershell
codex-kb show 12 --json

codex-kb update 12 `
  --kind implementation `
  --title "OAuth refresh token を単一フライト化" `
  --summary "期限切れ時の重複更新を抑える" `
  --purpose "ユーザーのリクエスト失敗を減らす" `
  --background "複数タブが同時に更新していた" `
  --rationale "共有 Promise で競合を防ぐ" `
  --outcome "並行更新を1回に抑えた" `
  --tag auth --tag oauth `
  --path src/auth/refresh.ts --symbol refreshToken
```

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

## Shared web service for multiple PCs

`https://kb.sugar-knight.com` is an optional authenticated service for
multiple PCs. It is separate from the local database: do **not** copy or
cloud-sync `codex-kb.sqlite3` or its WAL file.

Registration does not need an invitation code. The command prompts for an
account password and a separate encryption passphrase, then saves only this
PC's bearer token and E2E key material to
`~/.codex-kb/remote-credentials.json`. The password and passphrase are never
saved. On Windows the credential file is ACL-restricted to the current user;
on Unix it is mode `0600`.

```powershell
# First PC: creates the account and the default profile credential file.
codex-kb remote register --username alice --display-name 'Alice'

# Later commands automatically use the saved credential file.
codex-kb remote whoami
codex-kb remote record --kind note --title 'example' --summary 'remote note'
codex-kb remote list --query 'VPS file transfer'
codex-kb remote file upload .\plan.pdf

# Copy existing local records.  It adds an encrypted origin marker, so a
# failed import can safely be rerun without duplicating completed records.
codex-kb remote import-local --dry-run
codex-kb remote import-local

# Second PC: prompts for the same account password and encryption passphrase,
# then creates a distinct bearer token for that PC.
codex-kb remote login --username alice
```

To keep credentials in a repository or project profile instead, explicitly
choose the path during registration/login. A present
`.codex-kb-credentials.json` in the current directory is selected
automatically; it is git-ignored by this repository.

```powershell
codex-kb remote login --username alice --credentials .\.codex-kb-credentials.json
```

For a complete install-and-handoff procedure for another PC or another Codex
instance, including the safe handling of the two required secrets, see
[the remote-PC setup manual](docs/REMOTE_SETUP.md). Do not copy a bearer token
or `remote-credentials.json` between PCs: each PC logs in and receives its own
revocable token. The manual also includes `scripts/setup-remote.ps1`, which a
trusted Codex session can use for clone/install/login/verification.

Knowledge records are encrypted on the client with AES-256-GCM. The server
only sees opaque ciphertext, so remote keyword search is performed locally
after the authenticated client downloads and decrypts the user's records.
Files are encrypted before upload, including their filename and checksum.
Sharing a file encrypts its random file key for the selected user's X25519
public key, so only that user's authenticated clients can decrypt it.

```powershell
codex-kb remote file list
codex-kb remote file download <file-id> --output .\plan.pdf
codex-kb remote recipient-key bob        # compare this value with Bob out of band
codex-kb remote file share <file-id> bob --recipient-fingerprint <fingerprint>
codex-kb remote file revoke <file-id> bob
```

For an E2E threat model, keep the encryption passphrase and at least one
credential file recoverable outside the VPS. If all copies of both are lost,
the data cannot be recovered. A recipient public-key fingerprint should be
verified out of band before sharing highly sensitive files; use
`--recipient-fingerprint` to enforce an expected fingerprint. `logout`
revokes the current PC's token and removes its credential file.
