# 別PC・別Codexのセットアップ

この手順は、既存アカウントの暗号化ナレッジを別PCから読むためのものです。
新しいPCにBearerトークンや `remote-credentials.json` をコピーしません。各PCが
自分専用の失効可能なBearerトークンとローカル鍵を作成します。

## 用意するもの

- GitHub URL: `https://github.com/sugarkwork/codex-kb.git`
- アカウント名（小文字・数字・`._-`、3〜32文字）
- アカウントパスワード
- 暗号化パスフレーズ
- Windows PowerShell、Python 3.10以上、Git

アカウントパスワードだけ、またはBearerトークンだけではE2E暗号化済みデータを
復号できません。暗号化パスフレーズも必要です。

## 新しいWindows PCでの導入

PowerShellで実行します。

```powershell
$source = Join-Path $HOME 'source\codex-kb'
# この手順の更新が main へマージされるまでは、手順と同じブランチを明示する。
$branch = 'agent/remote-setup-manual'
git clone --depth 1 --branch $branch https://github.com/sugarkwork/codex-kb.git $source
Set-Location $source
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

PRが main へマージ済みなら、`--branch $branch` は省略して構いません。

Gitを使えない場合は、GitHubの **Code → Download ZIP** で取得・展開してから、
同じ `install.ps1` を実行します。

## Codexが実行する一括セットアップ

別PCのCodexには、まずこの手順URLとアカウント名だけを渡します。Codexはまずこの
ブランチをcloneし、導入までを実行します。アカウントパスワードと暗号化パスフレーズは、
可能なら本人が新PCのPowerShellへ直接入力します。Bearerトークン、
`remote-credentials.json`、APIキーをチャットへ貼り付けません。

```powershell
$source = Join-Path $HOME 'source\codex-kb'
$branch = 'agent/remote-setup-manual'
git clone --depth 1 --branch $branch https://github.com/sugarkwork/codex-kb.git $source

Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
codex-kb remote login --username <account-name>
codex-kb remote whoami
codex-kb remote list
```

本人がプロンプトに入力できない特別な場合だけ、`scripts/setup-remote.ps1` に一時環境
変数を渡す方式を使えます。値はコマンド引数、Git、`.ps1`、永続環境変数へ書きません。

## 推奨: 本人が対話入力してログイン

```powershell
codex-kb remote login --username <account-name>
```

表示された2つのプロンプトへ、アカウントパスワードと暗号化パスフレーズを入力します。
成功すると次のいずれかにPC固有の資格情報が保存されます。

- カレントディレクトリに `.codex-kb-credentials.json` が既にある場合: そのファイル
- それ以外: `~/.codex-kb/remote-credentials.json`

資格情報ファイルにはBearerトークンと復号鍵が入ります。Windowsでは現在のユーザー
だけが読めるACLを設定します。Git、クラウド同期、チャットへ保存・貼り付けしないでください。

## Codexへ渡す場合

Codexへ渡すのは次だけです。

```text
Repository: https://github.com/sugarkwork/codex-kb.git
Branch: agent/remote-setup-manual
Account username: <account-name>
Task: install codex-kb from the stated branch. Stop at `codex-kb remote login`
so I can type the account password and encryption passphrase directly in the
new PC's PowerShell. Then run `remote whoami`, `remote list`, and `remote secret list`.
Never ask me to paste bearer tokens, remote-credentials.json, API keys, or a
secret value into chat, Git, a command argument, or a script.
```

パスワードと暗号化パスフレーズは、可能ならCodexのプロンプトではなく、起動済みの
PowerShellへ本人が直接入力します。Codexへ秘密を渡すことを明示的に許可する場合に
限り、親プロセスの一時環境変数から読ませることもできます。値をコマンド引数、Git、
`.ps1`、永続環境変数へ書かないでください。

```powershell
# 信頼できる一時セッションだけで値を設定する。値そのものは表示・コミットしない。
$env:CODEX_KB_ACCOUNT_PASSWORD = '<secret supplied in the trusted session>'
$env:CODEX_KB_ENCRYPTION_PASSPHRASE = '<secret supplied in the trusted session>'

try {
  codex-kb remote login --username <account-name> `
    --password-env CODEX_KB_ACCOUNT_PASSWORD `
    --passphrase-env CODEX_KB_ENCRYPTION_PASSPHRASE
} finally {
  Remove-Item Env:CODEX_KB_ACCOUNT_PASSWORD -ErrorAction SilentlyContinue
  Remove-Item Env:CODEX_KB_ENCRYPTION_PASSPHRASE -ErrorAction SilentlyContinue
}
```

この方式でも、秘密を受け取ったCodexの会話・実行ログの扱いはそのCodex環境の信頼境界に
従います。信頼できないエージェント、共有ログ、Issue、PR、Git履歴には渡しません。

## APIキーを別PCのCodexで使う

APIキーをナレッジ、ファイル、`.env`、Git、Codexへのチャットには入れません。最初のPCで
一度だけ、秘密専用のリモート領域へ登録します。値はコマンド引数に渡さず、隠し入力または
一時環境変数を使います。

```powershell
# 対話入力。値は画面に表示されない。
codex-kb remote secret set OPENAI_API_KEY

# 値を表示せず、名前だけ確認する。
codex-kb remote secret list
```

別PCが同じアカウントで `remote login` 済みなら、Codexへ渡すのは秘密の**名前**と、
必要なコマンドだけです。

```text
The other PC is authenticated with my codex-kb account. Use the remote secret
named OPENAI_API_KEY only through `codex-kb remote secret run`; do not print,
export permanently, store, or ask me for the value. Run only the command I
approve, with OPENAI_API_KEY injected into that one child process.
```

例えば、本人が内容を確認したスクリプトへだけ一時注入します。

```powershell
codex-kb remote secret run OPENAI_API_KEY --env-var OPENAI_API_KEY -- python .\use-api.py
```

`secret run` の子プロセスは値を利用できるため、未確認のコード、共有ログ、外部へ送信する
コマンドには使いません。API提供元がPC別キー・OAuth・短期トークンを発行できるなら、共有
静的キーよりそちらを優先し、端末紛失時には該当キーを失効・ローテーションします。

## 動作確認

```powershell
codex-kb remote whoami
codex-kb remote list
codex-kb remote secret list
```

`whoami` でアカウント名が返り、`list` が復号済みナレッジを表示すれば成功です。
必要ならファイルも確認できます。

```powershell
codex-kb remote file list
```

## 初回PCからの既存ナレッジ移行

既存ナレッジが入っている最初のPCでだけ実行します。

```powershell
codex-kb remote import-local --dry-run
codex-kb remote import-local
```

移行はローカルIDを暗号化した完了マーカーで管理します。中断しても同じコマンドを
再実行でき、完了済みレコードは重複登録しません。

## トラブル時

- `remote login` が失敗する: アカウント名、アカウントパスワード、暗号化パスフレーズを確認します。
- 復号に失敗する: 暗号化パスフレーズが異なる可能性があります。Bearerトークンだけでは解決しません。
- PCを失った: 別PCから `remote logout` して、そのPCのトークンを失効します。API提供元でも、そのPC用キーを失効・ローテーションします。パスフレーズと全資格情報を失うと復旧できません。
