# Pure alert forwarding

The cron job searches every message in the configured source folders, regardless
of read/unread status. A local SQLite database records successful forwards by
mailbox and source message ID. Reading a message, or marking it unread again,
does not change whether it is forwarded. In production, the script marks the
source message read only after Zimbra returns a send receipt and the successful
send is saved in the history database.

If marking a confirmed forward read fails, the next run retries only the
read-status update, without resending the email. Previously confirmed forwards
that are still unread are also marked read when encountered. Failed, skipped,
and unconfirmed sends are not marked read by the script.

## Send history

`forwarding_state.sqlite3` lives beside `main.py` and is excluded from Git.
Back it up with the server configuration. Preserve it when deploying code;
removing the history can cause previously forwarded messages to be resent.

On the first production run, existing `OK id=...` entries in `error.log` are
imported as confirmed sends. Failed sends, test sends, and legacy dry-run sends
are excluded. This migration runs once for each mailbox. For an existing
installation without a complete success log, seed confirmed source IDs from
verified records before enabling the new job; do not infer them from read flags.

The state transitions are:

- No record: eligible to send, whether the source is read or unread.
- `pending`: persisted immediately before sending to Zimbra.
- `sent`: Zimbra returned a sent-message receipt, which is saved in the database.

An exception or interruption after `pending` is committed leaves the source
pending. Automatic resend is blocked because the server may already have accepted
the email. The existing SOC error notification explains that Sent must be checked.
After verifying the outcome, an operator can mark that specific mailbox/source
record sent with its receipt, or remove that pending record only if the server
did not accept the send. Failures while fetching/preparing a message do not create
a pending record and can retry on the next run.

`.forwarding.lock` prevents overlapping cron/manual runs. An overlapping run exits
without logging into the mailbox. Search pagination continues past recorded
messages; `forward.limit` limits new successful sends per folder, rather than
limiting the search to the first page.

## Modes and checks

Production, `--test`, and `--dry-run` have separate history namespaces, so a test
send cannot suppress a production notification. Test and dry-run modes preserve
source read status; only production marks confirmed forwards read.

The existing `--dry-run` flag **sends one real email** to `test_forward` recipients;
it retains that legacy behavior. `--test` also sends real emails, up to the limit.
`--error` sends the SOC error template. Do not use these modes for a no-send check.

Run the regression checks with the installed requirements:

```sh
python3 -m unittest -v test_send_history
```

These checks use fake mailbox clients and temporary databases. They do not connect
to Zimbra or send email.
