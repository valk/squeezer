---
description: Enroll a TOTP secret (Google Authenticator or similar) for squeezer's /elevate Telegram command.
---

Set up 2FA for squeezer's `/elevate` command. Idempotent — safe to re-run
(it won't regenerate an existing secret unless the user explicitly asks to
replace it). Use the Bash tool for each step.

1. `SQUEEZER_HOME="${SQUEEZER_HOME:-$HOME/.config/squeezer}"`.
2. Check whether `$SQUEEZER_HOME/.env` already has a `TOTP_SECRET=` line
   with a non-empty value. If it does, tell the user 2FA is already set up
   and ask whether they want to replace it (only proceed past this point if
   they say yes — replacing it invalidates their existing authenticator
   entry).
3. Generate a secret:
   `python3 -c "import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/daemon'); import totp; print(totp.generate_secret())"`
4. Write (or replace) the `TOTP_SECRET=<value>` line in `$SQUEEZER_HOME/.env`
   (create the file from `${CLAUDE_PLUGIN_ROOT}/templates/env.example`'s
   shape first if it doesn't exist yet — but don't clobber other existing
   values in it).
5. Print the provisioning URI for manual entry, reading the secret back
   from `$SQUEEZER_HOME/.env` (already written in step 4) rather than
   carrying it through the conversation again:
   `python3 -c "import sys; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/daemon'); import config; secret = config.load_env()['TOTP_SECRET']; import totp; print(totp.provisioning_uri(secret))"`
   Show the user both the raw base32 secret and this URI, and tell them to
   add it to Google Authenticator (or any TOTP app) via manual key entry —
   paste the secret in, algorithm SHA1, 6 digits, 30-second period (Google
   Authenticator's defaults already match this).
6. Ask the user to read back the current 6-digit code from their
   authenticator app, then confirm it verifies — again reading the secret
   back from `$SQUEEZER_HOME/.env` rather than interpolating it, with only
   the user-given code passed through directly:
   `python3 -c "import sys, time; sys.path.insert(0, '${CLAUDE_PLUGIN_ROOT}/daemon'); import config; secret = config.load_env()['TOTP_SECRET']; import totp; print(totp.verify_code(secret, '<code the user gave>', None, time.time())[0])"`
   If this prints `False`, the code was wrong or the secret was mistyped
   into the app — ask the user to re-check and try again rather than
   silently continuing.
7. Once confirmed, tell the user 2FA is active and they can now send
   `/elevate <code> <hours>` (hours: 2, 4, 8, or 24) over Telegram.
