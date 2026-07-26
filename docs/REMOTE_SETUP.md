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
git clone --depth 1 https://github.com/sugarkwork/codex-kb.git $source
Set-Location $source
Set-ExecutionPolicy -Scope Process Bypass
.\install.ps1
```

Gitを使えない場合は、GitHubの **Code → Download ZIP** で取得・展開してから、
同じ `install.ps1` を実行します。

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
Account username: <account-name>
Task: install codex-kb, run remote login, then verify remote whoami and remote list.
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

## 動作確認

```powershell
codex-kb remote whoami
codex-kb remote list
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
- PCを失った: 別PCから `remote logout` して、そのPCのトークンを失効します。パスフレーズと全資格情報を失うと復旧できません。
