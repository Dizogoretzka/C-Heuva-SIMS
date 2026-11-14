# SIMS SRS Requirements Update

This update records the revised requirements in `SIMS_CHANGE_SPEC.md`.
No earlier SRS document was present in the project, so this is a traceable
replacement section rather than an edit to a missing source document.

## Current requirements

- Learners belong to grade-based classes (grades 1–12, optional letters A–G).
  Class labels are parsed consistently and displayed in numeric grade order.
- Teachers configure their own classes and subjects. Learners request
  connections to matching teachers, and each teacher's learner, subject, and
  result access is limited to accepted connections.
- Administrators can inspect assignments but cannot create or change them.
- Results use assessment columns configured by class, subject, and academic
  period. Marks must be within each maximum; incomplete work is not treated as
  zero; a final mark is calculated only when assessment weights total 100%.
- Grades use the fixed scale in `main_app/grading.py`. Existing results are
  preserved as previous records and are not silently reinterpreted.
- School Profile information is administrator-managed. Reports use the saved
  school identity and omit empty contact or motto fields; an absent logo falls
  back to the school crest.
- PDF and CSV exports follow the same selected class, period, subject, and
  permission scope. PDF output includes a school header, watermark when the
  crest is available, generated date/time and user, and page numbering. CSV
  files identify the school, report, and generator and omit empty profile
  details.
- PDF bodies are report-specific: report cards include learner identity,
  weighted subjects, totals, comments/signatures, next-period dates, and the
  grading key; class lists separate learner rows from gender totals; mark
  sheets separate learner marks from class statistics and grade counts; and
  school summaries group their data by section. Assignment reports separate
  teacher setups, unmatched learners, and unconfigured subjects; activity
  reports include an overview and detail table; learner, teacher, and accepted
  learner lists include purpose-specific summaries. The one-time credentials
  sheet has a separate confidential print layout. Table headers repeat and
  data rows stay together.
- One-time learner import credentials are marked confidential and are not
  cached by the response.
- The administrator Activity Log includes authentication and legacy learner
  sign-in events, filters by role, shows readable changes without credential
  values, and retains per-user timelines.
- Administrators can send notifications to one learner or all learners.
- Administrators and teachers can reset their own PIN from My Profile after
  entering the current PIN. Administrators may set learner passwords and
  reset PINs for teachers and other administrators; an administrator must use
  My Profile and verify their current PIN to reset their own.
- Sidebar highlighting is selected server-side from the current URL name.
- Sessions expire after five minutes of inactivity. The next request signs the
  user out and displays a re-login message; saved application data is not
  deleted by the timeout.
- Application dates and times use Namibia Central Africa Time
  (`Africa/Windhoek`, GMT+2).
- An administrator can create another administrator account. The new
  administrator receives a generated account ID and a one-time PIN.
- Account IDs replace user-chosen usernames in account forms and sign-in
  screens. IDs are generated as the normalized lowercase surname followed by
  the first initial, with the smallest available numeric suffix for a
  collision. Existing IDs are migrated without changing passwords or PINs.
  Learner CSV imports generate the same IDs, and new account credentials show
  the generated ID once for distribution.
- A teacher can publish a report card for one learner they have accepted.
  Published cards appear in that learner's results page and can be downloaded
  by that learner as PDF or CSV. Other learners cannot access the publication;
  the existing self-service all-subject report card remains available.
- Deleted records are stored in a private, per-account recycle bin. Users can
  search and filter their own bin, restore records with related cascades and
  cleared links, or permanently delete a bin entry.
- Bulk deletion on learner, teacher, academic-period, and results lists uses
  the submitted server-side filters and search term. Teacher permissions are
  scoped to accepted learners and their own results; teacher accounts and
  academic periods are administrator-only. Academic periods with results or
  assessment settings are protected from bulk deletion.

## Superseded requirements

The following prior requirements are retired and must not be reintroduced:

- Attendance tracking and attendance reports.
- Course records and course-based learner grouping; use classes instead.
- Editable grade bands.
- The old fixed assessment-weight page and maximum-total-score check command.
- Administrator assignment, reassignment, or teacher/subject/learner linking.
- A separate learner Login Activity page; sign-ins appear in Activity Log.

