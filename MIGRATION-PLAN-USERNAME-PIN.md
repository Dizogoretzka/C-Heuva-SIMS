# Username and PIN migration plan

## Pre-migration snapshot

- Database: SQLite at `db.sqlite3`
- Backup: `C:\Users\riley\.copilot\session-state\7d449643-d4e5-4ad4-a979-905aa52b7801\files\pre-username-pin-db.sqlite3`
- Backup SHA-256: `101EF7C5DEBCC25C49A01F276FAD28869CBDE43F65184017CC325F34386CCBB7`
- Applied `main_app` migrations: `0001_initial` through `0011_alter_customuser_user_type_attendance_and_more`
- Existing account rows at snapshot time: 3
- Existing user identity columns: `email` is unique; `username` does not exist.

## Planned schema changes

1. Add a nullable username column first so existing accounts remain available for the configured email sign-in fallback.
2. Make email nullable, blank by default, and non-unique without changing stored email values.
3. Add a database uniqueness constraint on `Lower(username)` for non-null usernames.
4. Add PIN hash, PIN-issued timestamp, consecutive failed-attempt count, and lock-until timestamp fields.
5. Preserve all profile, role, result, attendance, and login-activity tables and data.

## Migration and recovery procedure

1. Keep the pre-migration SQLite backup unchanged.
2. Create and inspect Django migrations before applying them.
3. Apply the migrations to the working database only after the code changes and migration checks succeed.
4. Verify the applied migration list, user row count, retained email data, and the new account fields.
5. If migration verification fails, stop and restore the database from the backup only with explicit operator approval; do not overwrite the backup.

## Existing-account username issuance

The schema migration intentionally leaves usernames null for existing rows. The separate `create_usernames` management command will derive and export usernames from email local-parts, so the generated CSV reflects the actual values that need to be handed out.
