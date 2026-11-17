# SIMS SRS Requirements Update

This document records the current requirements for the staged changes in
`SIMS_CHANGE_SPEC_2.md` and `SIMS_CHANGE_SPEC_3.md` and replaces conflicting
requirements in older project documentation. No separate earlier SRS
document was present.

## Roles, classes, and academic periods

- The system has administrator, teacher, and learner roles. Administrators
  have school-wide access. Teachers can access only their own subjects and
  learners accepted by them. Learners can access only their own information.
- Learners belong to grade-based classes (grades 1-12, optional letters A-G).
  Class labels use the shared parser and are displayed in numeric grade order.
  Learner lists are ordered by surname, then first name, ignoring case and
  accents. This order applies to lists, requests, dropdowns, results entry,
  assignments, report data, exports, import previews, and credentials sheets.
- Teachers configure their own classes and subjects. Learners request
  connections to teachers who teach their class. Teacher imports are accepted
  by the importing teacher; administrator imports do not select a teacher and
  follow the normal learner request flow.
- Administrators can inspect assignments but cannot create or change them.
- Results and assessment settings are scoped by class, subject, and academic
  period. Existing results are preserved as previous records and are not
  silently reinterpreted.
- Results use the fixed grading scale in `main_app/grading.py`. Marks must be
  between zero and the assessment maximum. Blank marks remain incomplete, not
  zero, and final marks are calculated only when all required marks are
  present and assessment weights total 100%.

## Manage Results

- After a teacher chooses a class, subject, and academic period, the results
  table shows every learner accepted by that teacher in the class, the
  assessment columns and settings, final percentages, and grades.
- A search field filters the displayed rows immediately by learner name or
  account ID without reloading the page or discarding entered marks. If there
  are no matches, the page says "No learner matches". Without JavaScript, all
  rows remain visible.
- Save controls appear above and below the table. Saving marks and assessment
  names, weights, and maximums is one database transaction. Invalid input
  saves nothing, displays the error beside the affected cell, and preserves
  the teacher's entries. Unsaved cells are identified, an unsaved-changes
  warning appears before leaving, and a successful save reports the number of
  marks saved.
- Each changed mark is recorded in the activity log with its old and new
  value. Typing in the results table counts as user activity for the session
  timeout.

## Learner import and first sign-in

- An import row with a class keeps that class. If rows have no class, the
  confirmation page requires a valid class choice and shows how many rows
  will receive it. The shared class parser validates the chosen class, and no
  learner records are saved before confirmation.
- Teachers may type or select the class. If it is not already configured for
  them, confirmation explains that it will be added to their classes and
  subjects. Learners imported by that teacher are accepted by that teacher.
- Administrators choose a class from configured classes or enter another
  valid class. They do not assign an importing teacher; learners follow the
  usual request flow.
- Imported learners are created without a usable password and are marked not
  yet activated. Their credentials sheet contains name, class, and account
  ID, plus the instruction: "First sign-in: enter your account ID and leave
  the password empty." It does not expose a generated password.
- On the imported learner's first sign-in only, the account ID with an empty
  password opens password setup. The learner sets and confirms a password
  validated by Django's password validators. Setup requires a short-lived
  server-side token (about 10 minutes) created at sign-in; the setup page
  cannot be opened directly by URL. Attempts count toward account lockout and
  the per-IP limit. Activation is logged without secrets.
- This activation flow applies only to newly imported learners. Existing
  learners, self-registered learners, and learners added by hand keep their
  usual sign-in behavior. Teachers and administrators can see imported
  learners who are not yet activated and reset the activation status so the
  learner must set a password again.

## Reporting and report-card publication

- Administrators can report on all learners and subjects. Teachers can report
  only on learners accepted by them and on their own subjects. Teachers and
  administrators can create and preview report cards in every publication
  state.
- Supported reports include learner report cards, class lists, results mark
  sheets, school summaries, assignments, learner lists, and teacher lists.
  Each PDF and CSV export uses the same filters, data, and role permissions.
  Before export, the report form shows how many records match and the
  selected academic period.
- For a learner and academic period, teachers publish or unpublish all
  report cards for a chosen class, or publish an individual learner's card.
  The status is shown as "Published" or "Not published". Administrators have
  the same class actions and can publish or unpublish school-wide for a
  period. Bulk confirmations show how many learners are affected.
- Learners see and download only results published for them and that academic
  period. If their report card is not published, the page says "Your report
  card has not been published yet." Unpublishing hides the affected results
  immediately. Every publish and unpublish is recorded in the activity log.

## Documents and exports

- School Profile information is administrator-managed. Reports use the saved
  school identity, omit empty contact or motto fields, and use the school
  crest when no logo is available.
- PDF and CSV exports identify the school, report, and generator and follow
  the selected class, academic period, subject, and permission scope. PDFs
  include the school header, generated date/time and user, and page numbers.
  Report-specific bodies include appropriate learner, class, results,
  assignment, activity, and list data. Table headers repeat, and data rows
  stay together.
- The report card includes learner identity, weighted subjects, totals,
  comments/signatures, next-period dates, and the grading key. The class list
  includes numbered learners and gender totals. The mark sheet includes
  learner marks, class statistics, and grade counts. The school summary
  groups learners by class and gender and summarizes teacher assignments and
  learner request status.
- The one-time credentials sheet has a confidential print layout and its
  response is not cached.

## Accounts, activity, and session timeout

- Account IDs replace user-entered usernames in account forms and sign-in.
  IDs are generated from the normalized lowercase surname and first initial,
  with the smallest available numeric suffix for collisions. Existing
  passwords and PINs are unchanged. Learner imports use the same ID scheme.
- Administrators and teachers can reset their own PIN from My Profile after
  entering the current PIN. Administrators can set learner passwords and
  reset PINs for teachers or other administrators. Administrators verify
  their current PIN to reset their own.
- Administrator, teacher, and learner profiles let the user set one of eight
  fixed security questions or a custom question. Setting or changing it
  requires the current password for learners or current PIN for staff. The
  normalized answer is stored only as a Django password hash and is never
  displayed, logged, or exported. Users without a question are prompted on
  their dashboard and profile to set one.
- The sign-in page offers self-service password or PIN reset. It matches the
  full name and, for learners, parsed grade and class; administrator and
  teacher lookups also require the account ID. Ambiguous learner matches
  require the account ID. A matched user must always answer their security
  question. Names are matched without case, spacing, or accent differences.
  Unknown accounts and accounts without a question receive a deterministic
  placeholder question and the same generic failed-answer message.
- Five consecutive wrong answers block self-service reset for that account
  for one hour. Per-IP limiting uses only a short-lived cache key derived
  from a hash of the request address. Correct answers lead to a short-lived,
  server-side reset token. Learners set a new password through Django's
  password validators; administrators and teachers receive a generated PIN
  shown once, with only its hash stored.
- A successful self-service reset ends all existing sessions for that
  account, clears sign-in lockout counters and imported-learner activation
  status, and returns the user to sign-in without signing them in. The next
  successful sign-in displays a one-time notice with the reset date and time.
  Django Admin superusers are not eligible for self-service reset.
- The administrator Activity Log includes authentication events and readable
  changes, with role and other filters and per-user timelines. Mark changes,
  learner activation, report-card publication, and inactivity sign-outs are
  logged. Security-question changes, reset starts, wrong answers, blocked
  attempts, and completed self-service resets are also logged; reset
  completions are highlighted. Reset logs include the typed name and role
  where applicable, but never the answer. Passwords, PINs, hashes, tokens,
  answers, and other secrets are never shown.
- Activity records, learner login records, their pages and filters, timelines,
  and PDF/CSV exports do not store or show IP addresses. Sign-in and
  self-service reset rate limits may read the request address only to create
  a short-lived hashed cache key; the address is not written to the database,
  activity log, or a page.
- Authenticated sessions use `SIMS_IDLE_TIMEOUT_SECONDS` (default 3600
  seconds). Administrators, teachers, and learners see an hours/minutes/
  seconds countdown in the top navigation; landing and sign-in pages do not
  show it. Mouse, keyboard, touch, scroll, and typing activity reset the
  timer, and open tabs share the same activity timestamp. The countdown is
  calculated from timestamps so it remains accurate across sleep and
  background tabs.
- The server rejects a request after the configured inactivity limit, signs
  the user out, and shows "You were signed out after 1 hour of inactivity."
  While a user is active on a page that makes no other requests, the page
  sends a small keep-alive request no more than once every five minutes. It
  does not show the crest loading screen. An inactivity sign-out is recorded
  in the activity log, and saved application data is not deleted.

## Other current requirements

- The administrator Activity Log includes learner sign-in events; there is no
  separate learner Login Activity page.
- Administrators can send notifications to one learner or all learners. The
  all-learners action confirms the number of recipients.
- Sidebar highlighting is selected server-side from the current URL name.
- Application dates and times use Namibia Central Africa Time
  (`Africa/Windhoek`, GMT+2).
- An administrator can create another administrator account. The new
  administrator receives a generated account ID and a one-time PIN.
- Deleted records are stored in a private, per-account recycle bin. Users can
  search and filter their own bin, restore records with related cascades and
  cleared links, or permanently delete a bin entry. Bulk deletion uses the
  current server-side search and filter scope. Teacher actions remain limited
  to accepted learners and their own results; teacher accounts and academic
  periods are administrator-only. Academic periods with results or assessment
  settings are protected from bulk deletion.

## Superseded requirements

The following prior requirements are retired and must not be reintroduced:

- Attendance tracking and attendance reports.
- Legacy non-class grouping; learner groups use classes.
- Editable grade bands.
- The old fixed assessment-weight page and maximum-total-score check command.
- Administrator assignment, reassignment, or teacher/subject/learner linking.
- A separate learner sign-in activity page; sign-ins appear in Activity Log.
- The former sign-in note that directed every user to staff for forgotten
  passwords or PINs; self-service reset is available when a security question
  is set, and users without one must ask their teacher or administrator.
- Any earlier requirement or implementation that stored, filtered, displayed,
  or exported IP addresses in activity or learner-login records. These IP
  columns and their stored values are removed; rate limiting uses hashed,
  expiring cache keys only.
- Learner access to unpublished report-card results.
- The former fixed inactivity policy; use the configurable server-enforced
  session timeout above.

