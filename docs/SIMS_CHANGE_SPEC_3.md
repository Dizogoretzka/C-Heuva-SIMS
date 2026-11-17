# C. Heuva Junior Secondary School Information System (SIMS) — Change Specification 3

I have a Django project, the C. Heuva Junior Secondary School Information
System (SIMS), with three roles (administrator, teacher, learner), main code
in `main_app`. Make the two changes below. Do not add anything that is not
listed.

## Rules

- Work on a copy of the database and keep existing data in every migration,
  except where a section says to remove data.
- Use the word "class" (for example Grade 8A), never "course". Attendance
  stays removed.
- Save this text to `docs/SIMS_CHANGE_SPEC_3.md` first and re-read it before
  each part.
- Run the full test suite after each part and fix what you break. Do not
  weaken, skip, or delete a test to make it pass. If tests still fail, stop
  and tell me.
- After the last part, stop. Give me the summary and wait. No extra features,
  no refactoring, no deployment.

## PART 1: Security question and self-service reset

### 1. Security question on My Profile (all three roles)

- Add a "Security question" section to the profile page of the administrator,
  teacher, and learner. The user chooses one question from a list of about 8
  fixed questions (for example "What was the name of your first school?",
  "What is your mother's maiden name?", "What is the name of the town where
  you were born?", "What was the name of your first teacher?") or writes
  their own, and types the answer.
- The answer must be at least 4 characters. Normalise it before storing (trim
  spaces, lower case, collapse repeated spaces, remove accents) and store
  only a hash of it, using Django's password hasher. Never store, log,
  display, or export the answer. The saved question is shown on the profile,
  the answer never is.
- To change the question or answer, the user must confirm with their current
  secret (the password for learners, the PIN for administrators and
  teachers).
- Existing users have no security question. Show a short banner on their
  dashboard and profile ("Set a security question so you can reset your own
  password"), which disappears once it is set. A user without a question
  cannot use the self-service reset and sees "Ask your teacher or
  administrator to reset it."

### 2. "Forgot your password or PIN?" on the sign-in page (replace the existing note, keep the same position)

- Step 1, find the account. The form asks for the full name (first name and
  last name) plus: for learners, the grade and class (example "8A", using
  the shared class parser); for administrators and teachers, who have no
  class, the account ID. Match names ignoring case, spaces, and accents. If
  more than one account matches, ask for the account ID as well. If it is
  still not unique, show "We cannot reset this account online. Ask your
  teacher or administrator."
- Step 2, the security question. Show the matched account's question and ask
  for the answer. A name and class are easy for other people to find out, so
  the answer is always required. Do not skip this step.
- Step 3, new secret. After a correct answer: a learner types a new password
  and its confirmation (checked with Django's password validators). For
  administrators and teachers, who use a PIN that the system generates, the
  system creates a new random PIN that follows the PIN rules, shows it once,
  and stores only its hash.
- Do not reveal whether an account exists. If no account matches, or the
  account has no question, show a plausible question chosen deterministically
  from the list based on the typed name, and always fail at step 2 with the
  same generic message ("The details did not match.") as a wrong answer.
- Protect it: count wrong answers per account and block self-reset for that
  account for 1 hour after 5 wrong answers in a row, and limit attempts per
  IP address with a short-lived cache entry (not stored in the database or
  the activity log). Use the project's existing lockout style and CSRF
  protection. The step 3 page works only with a short-lived server-side
  token (about 10 minutes) created after a correct answer, and cannot be
  opened by URL.
- After a successful reset: sign out every existing session of that account,
  clear its sign-in lockout counters, clear the "not yet activated" flag for
  an imported learner, and then send the user to the sign-in page with a
  success message. Do not sign them in automatically.
- On that account's next successful sign-in, show once: "Your password (or
  PIN) was reset on [date and time] using your security question. If this
  was not you, tell your administrator."
- Self-service reset is a new route to the account, so write the code
  carefully. Administrators and teachers can still reset others through the
  existing administrator pages, and administrators can still reset learners
  and teachers. That does not change.
- Add the crest loading state and the same look as the sign-in page. Add the
  page to the role middleware as public, and keep the Django Admin superuser
  out of this flow.

### 3. Activity log

- Record in the activity log: question set or changed, reset started, wrong
  answers (with the name that was typed and the role, never the answer),
  self-service reset completed (highlighted so administrators can spot
  them), and blocked attempts. Use detailed sentences like the other
  entries.

### 4. Tests for part 1

- Question and answer saved and hashed, answer never appears in the database,
  logs, activity log, pages, or exports; changing it needs the current
  secret; the answer rules.
- The full flow for each role, including the learner password change and
  the generated PIN shown once.
- Wrong answer, five wrong answers block the account for an hour, a
  non-existent name behaves like an existing one (same message, similar
  timing), an account without a question, two learners with the same name
  and class, matching ignores case and accents.
- The step 3 page cannot be opened without the token, and the token expires.
- Existing sessions end, lockout counters clear, the one-time notice appears,
  and the reset is recorded in the activity log.
- The Django Admin superuser cannot use the flow.

## PART 2: Remove the IP address from the activity

- Remove the IP address from the activity log and from everything that shows
  or stores it: the model field, the Activity page and its filter or search,
  the per-user timeline, the detailed sentences, the CSV and PDF exports,
  templates, tests, and the SRS wording.
- Create a migration that drops the IP address column, so all stored IP
  addresses are deleted. Do not back them up.
- Do not log or store IP addresses anywhere else in the database. The sign-in
  lockout and per-IP limit may still read the request's address, but only to
  build a short-lived cache key (hash it), which must expire by itself and
  never reach the database, the log, or a page.
- Add tests: no page, export, or log entry contains an IP address; the column
  no longer exists; the per-IP limits still work.

## FINISH

- Update the SRS wording for both parts and list the older requirements these
  replace.
- Run the full suite once more and give me the results, the files changed,
  and any manual step for me. Then STOP and wait.
