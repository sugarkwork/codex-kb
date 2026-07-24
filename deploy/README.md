# kb.sugar-knight.com deployment

This deploys the authenticated opaque-storage service, not the local
`~/.codex-kb` database. Each registered account gets an isolated remote
knowledge area.  The service stores AES-GCM ciphertext, encrypted metadata,
public X25519 keys, password verifiers, and hashes of bearer tokens; it never
receives a usable content-decryption key.

Run the following as `root` on the Japan VPS after copying this repository to
`/opt/codex-kb`:

```bash
useradd --system --home-dir /var/lib/codex-kb-server --shell /usr/sbin/nologin codexkb
install -d -o codexkb -g codexkb -m 700 /var/lib/codex-kb-server
python3 -m venv /opt/codex-kb/.venv
/opt/codex-kb/.venv/bin/pip install --upgrade pip
/opt/codex-kb/.venv/bin/pip install -r /opt/codex-kb/requirements-web.txt
install -m 600 /opt/codex-kb/deploy/codex-kb-web.env.example /etc/codex-kb-web.env
```

Bootstrap TLS before enabling the service publicly:

```bash
install -m 644 /opt/codex-kb/deploy/nginx-kb.sugar-knight.com.http.conf /etc/nginx/sites-available/kb.sugar-knight.com
ln -s /etc/nginx/sites-available/kb.sugar-knight.com /etc/nginx/sites-enabled/kb.sugar-knight.com
nginx -t && systemctl reload nginx
certbot certonly --webroot -w /var/www/html -d kb.sugar-knight.com
install -m 644 /opt/codex-kb/deploy/nginx-kb.sugar-knight.com.conf /etc/nginx/sites-available/kb.sugar-knight.com
nginx -t && systemctl reload nginx
```

Then install and start the service:

```bash
install -m 644 /opt/codex-kb/deploy/codex-kb-web.service /etc/systemd/system/codex-kb-web.service
systemctl daemon-reload
systemctl enable --now codex-kb-web.service
curl --fail https://kb.sugar-knight.com/healthz
```

The host only receives HTTPS through nginx. Uvicorn listens on `127.0.0.1:8100`.
For routine checks use `systemctl status codex-kb-web` and
`journalctl -u codex-kb-web -n 100`.

Users register and sign in from their own PCs with `codex-kb remote register`
and `codex-kb remote login`. Account passwords and encryption passphrases are
entered interactively and are never written to this server configuration.
