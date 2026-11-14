# C. Heuva SIMS Change Specification

This is the complete specification for the C. Heuva Junior Secondary School Information System (SIMS), with administrator, teacher, and learner roles. Do not guess any requirement that is not written here.

## Before stage 1

Inspect the project and give me a short list of the models, pages, menu items, reports, settings, and tests that each stage touches. Then start stage 1.

## Rules for this whole job

- Work on a COPY of the database and media files. Back up before any migration, and tell me which copy you used.
- Every migration must keep existing data (use renames and data migrations, not delete and recreate), except where a stage says to remove data.
- Use the word "class" (for example "Grade 8A") for a group of learners. The word "course" must not appear anywhere users can see it, nor in model, form, field, URL, or template names.
- Keep everything that already works: sign-in with the PIN and lockout, the crest and loading state, learner registration, PDF and CSV on every export, the activity log, and the role rules. Teachers see only their own records and the administrator sees everything.
- Do not weaken, skip, or delete a test to make it pass.
- After each stage: run the full test suite, fix what you broke, and give me a short report. If the tests still fail after you fixed them, STOP and tell me. Otherwise continue to the next stage.
- After stage 7, STOP. Do not start extra work, do not refactor, do not deploy, and do not suggest or build new features. Give me the final summary and wait for my next instruction.

## STAGE 1: Remove attendance everywhere

- Remove attendance completely: the models, forms, views, URLs, templates, menu items in every role's sidebar, dashboard totals and cards, the attendance summary report, the learner attendance page and percentage, import columns, Django Admin registration, activity log wording, tests, and SRS text.
- First export all existing attendance data to a CSV backup file in a folder I can find, and tell me where it is. Then remove the tables with a migration. Removed URLs must return 404, and no page may link to them.

## STAGE 2: Replace "course" with a class (grade and class letter)

- A learner has a class made of a grade number from 1 to 12 and an optional class letter from a to g (for example 8A, 9B, 5C, or just 9).
- On the learner registration page, replace the grade dropdown with one text field "Grade and class" with the hint "Examples: 8A, 8 a, 9". Accept any letter case and spaces or a hyphen between the number and letter. Parse it into a grade number and a class letter, show it as "Grade 8A", and reject anything outside grades 1 to 12 or letters a to g with a clear message. Make one shared parser and use it everywhere (registration, profile, import, teacher setup).
- A learner can change their class on My Profile (this resets their teacher requests, see stage 3).
- Remove the Course model, its pages, menu links, filters, report columns, dropdowns, and labels. Convert existing course data to a class where it can be read safely (for example "Grade 8" or "8A"), and leave the class empty where it cannot, without guessing. Tell me which records could not be converted.
- Bulk import: rename the column "course" to "class" and use the shared parser. Update the template download and the error messages.
- Sort classes in numeric order (Grade 2 before Grade 10).

## STAGE 3: Teachers and learners connect themselves; the administrator only views

- Remove every page and button where the administrator assigns, reassigns, or changes teachers, subjects, or learners (the assign-to-teacher pages, subject management for administrators, and the bulk assignment). The administrator keeps editing learner details (name, gender, class, photo, address) and deleting accounts.
- Teacher setup: add a "My Classes and Subjects" page where a teacher adds the classes they teach (using the shared parser) and, for each class, the subjects they teach it (name only). A teacher can teach several classes and the same subject in more than one class. A subject belongs to one teacher and one class, so a learner only ever sees subjects from their own class and their own accepted teachers. Subjects must not be visible to learners of other classes or to other teachers.
- Requests: when a learner registers (or changes class), the system automatically creates a pending request to every teacher who has set up that learner's exact class (a learner who typed only a grade number matches teachers who set up that grade without a letter). If no teacher has set up that class yet, the learner waits as "unmatched", and requests are created automatically when a teacher adds that class later.
- Teacher "Learner Requests" page, with a count badge in the sidebar: a list of pending learners with name, class, and date, filter by class, and the actions Accept, Decline, Accept all, and Decline all (with a confirmation that shows how many learners it will affect). Accept all and Decline all only apply to the pending requests currently shown by the filter. The learner is not told anything until the teacher acts.
- A learner can have several teachers (one for each teacher who accepts them). A teacher sees, reports on, and enters marks for only the learners who have been accepted by that teacher. Learners added or imported by a teacher are accepted for that teacher automatically.
- The learner dashboard shows the status ("Waiting for a teacher", "Accepted by Mr/Ms X", "Not accepted"), and shows subjects and results only from accepted teachers.
- Administrator "Assignments" page (view only, no buttons that change anything): a table of each teacher with their classes, subjects, and number of accepted learners, with a way to open the list of learners per teacher and class, the pending and declined counts, and a list of unmatched learners. Add filters for teacher and class, and PDF and CSV export.
- Existing data: existing subjects and links have no teacher and cannot be guessed. Make them visible to the administrator only, marked "not set up", and tell me which teachers will need to set up their classes and subjects again.
- Record every request, acceptance, decline, class setup, and subject setup in the activity log.

## STAGE 4: Results, assessment weights, and the fixed grading scale

- Remove the old assessment weights page, the Grade Bands button/form/model/pages, and SIMS_MAX_TOTAL_SCORE and its check command.
- On Manage Results, a teacher picks a class and a subject (only their own) and an academic period, and sees a table: one row per accepted learner, and one column per assessment.
  - Default columns: "Test 1" and "Examination".
  - Each column header has a weight box (for example 20) shown as a percentage, and a "maximum mark" box (default 100).
  - A plus button next to the last test adds another test column (Test 2, Test 3, and so on). Added tests can be renamed, and removed with a confirmation (removing deletes their marks).
  - The weights of all the columns must total 100. Show the running total, show a warning while it is not 100, and do not calculate final marks until it is 100.
  - A learner's final mark is the sum of (mark ÷ maximum × weight), rounded to 2 decimals, shown in a "Final %" column and next to it the grade symbol, filled in automatically as the teacher types.
  - Marks must be 0 or more and not above the column's maximum, with a clear error. Blank marks stay blank and are not counted as 0, and a learner with a missing mark shows "Incomplete" instead of a final mark.
  - The settings are saved per class, subject, and academic period, and a new period copies the previous period's columns and weights (not the marks).
- The grading scale is fixed and not editable. Store it once as a constant in code and use it everywhere. Look up the symbol from the final mark rounded to 2 decimals:
  - A* 90.00-100.00 Excellent (US A+)
  - A 80.00-89.99 Excellent (US A)
  - B 70.00-79.99 Very good (US A-)
  - C 60.00-69.99 Good (US B)
  - D 50.00-59.99 Sufficient (US C+)
  - E 40.00-49.99 Mediocre (US C)
  - F 30.00-39.99 Poor (US D+)
  - G 20.00-29.99 Very poor (US D)
  - U 0.00-19.99 Ungraded (US F)
- Learners see the symbol and description on their results. The US grade appears only in the grading key printed on reports.
- Existing results: do not guess. Convert each old result into one assessment called "Previous total (test + exam)" with weight 100 and maximum 100, keep the old test and exam scores stored but hidden, and mark these results "Previous record" so the teacher knows they can re-enter them.
- Record every change to marks and weights in the activity log, and keep learners limited to seeing their own results.

## STAGE 5: Reports that look like real school documents

- Add a "School Profile" page for the administrator with: school name, postal address, phone numbers, motto, principal's name, and a logo that defaults to the crest. Leave out any line that is empty. The system must not invent a motto or address. Do not use any payment code, LIN, or email on documents.
- One shared PDF layout (A4, margins, embedded fonts) with: the crest left of the school name in a bold serif heading, with the address, phone numbers, and an underlined motto centred under it; a thin rule; the document title in large underlined capitals; a faint crest watermark in the middle of the page; page numbers ("Page 1 of 3"); the date and time generated, and who generated it. Table headers repeat on every page and rows never split across pages.
- Each report has its own designed body, in the style of a real school report. Give each its own template:
  - Learner report card (the main one):   learner photo (left, only if there is one) beside a boxed table with Name, Account ID, Class, Gender, and Academic period; the title "END OF TERM 1 2026 LEARNER REPORT CARD" (from the academic period); a subject table with Subject, one column per assessment with its weight in the header, Final %, Grade, Comment (the grade description), and the teacher's initials; then a box with Overall Total and Overall Average; blank boxed areas for "Class teacher's comment" and "Principal's comment" with signature lines; "Next term begins on" and "Ends on" taken from the next academic period (blank line if not set); the full grading key table at the bottom (grade, scale, description, US grade); and the line "This report is invalid without a valid school stamp" in red at the very bottom. A teacher's report card includes only the subjects that teacher teaches this learner, and a learner's own download includes all subjects from all their teachers.
  - Class list: school header, class, class teacher (the teacher who generated it), academic period, a numbered table of learners (name, gender, account ID), totals for boys and girls, and signature and date lines.
  - Results mark sheet: for one class and subject, with the assessment names and weights in the header, one row per learner, Final % and grade, and at the bottom the class average, highest, lowest, pass rate (50% and above), and a count of learners per grade.
  - School summary (administrator): learners per grade and class, split by gender, number of teachers, each teacher's classes and subjects, accepted, pending, and unmatched learners, and the average final mark per class.
  - The Assignments report, the learner list, the teacher list, the activity log export, and the new-learner credentials sheet (marked "CONFIDENTIAL: hand out, then destroy") each get a proper title block, matching header and footer, and layout suited to what they show.
- Every PDF still has a CSV twin with the same data and filters, and the same permission rules. If the crest or a logo file is missing, the PDF still downloads.

## STAGE 6: Administrator pages and navigation

- Activity: remove the separate Login Activity page and menu link, and show sign-ins on the Activity page in the layout of the existing Activity Log, which I prefer. Add one role filter (All, Learner, Teacher, Administrator) and keep the other filters. Remove the "Changes" tab and any before-and-after view. Instead, give every entry a detailed readable sentence that says who did what, to which record, and when, with the old and new values written in the sentence (for example "Teacher Maria K. changed Test 1 for Selma I. in Grade 8C Mathematics from 45 to 52"). Generate sentences for old entries from the data that exists. Never show passwords, PINs, or hashes. Keep the per-user timeline.
- Notify Learners: add a "Notify all learners" option (with a confirmation showing how many learners will receive it) beside the existing way of choosing one learner. Keep it administrator-only.
- Credential changes: administrators sign in with a PIN and have no password. Administrators and teachers can reset their own PIN from My Profile after entering the current PIN; new PINs are shown once. Administrators can set a new password for any learner and reset PINs for any teacher or another administrator. Administrators must use My Profile and verify their current PIN to reset their own PIN.
- Sidebar: in all three panels, highlight the menu item of the page the user is on (and its group), decided on the server from the URL name. Child pages (edit, import, requests, mark entry, report pages) highlight their parent item. The active item has a filled background in the existing accent colour, bold text, a thick left border, and aria-current="page", with good contrast. Update the sidebars to match the removals above (no attendance, no grade bands, no assessment weights, no course, no login activity, no assignment actions) and to add the teacher items "My Classes and Subjects" and "Learner Requests", and the administrator items "Assignments" and "School Profile".

## STAGE 7: Tests, SRS wording, final check

- Update the existing tests and add new ones so that they check, at minimum: attendance and course are gone from the interface and the code, and old URLs return 404; the class parser (8A, 8 a, 9, 12G are accepted; 13A and 8H are rejected); the registration field; the requests flow (automatic requests, unmatched learners waiting, Accept, Decline, Accept all, Decline all, filters, a learner changing class); a teacher cannot see another teacher's learners or subjects, and learners only see subjects from their accepted teachers in their own class; the administrator view-only Assignments page and the absence of any assign buttons or URLs; the weight and mark grid (weights must total 100, adding a test, maximum marks, incomplete marks, final mark, every grade boundary, 89.99 and 90.00); the previous-record conversion; the structure of every report in PDF and CSV; role access for every page; the merged activity page and role filter, the detailed sentences, and that no secrets appear; notify all learners; the administrator PIN change and the reset powers; and the sidebar highlight for each role.
- Update the SRS wording for everything that changed, and list the older requirements these replace (attendance, grade bands, course, assessment weights, administrator assignment, login activity).
- Run the whole test suite one last time and give me: the results, the list of files changed, the list of data that could not be converted, and the manual steps I must do (backups, running migrations on PythonAnywhere, teachers setting up their classes, the School Profile page).
- Then STOP and wait for my next instruction.

## Additional requirements added after stage 7

- Replace user-entered usernames with generated account IDs for existing and
  new accounts. Generate each ID from the lowercase normalized surname and
  first initial; append the smallest numeric suffix when needed. Do not expose
  an editable ID field. Keep passwords and PINs unchanged.
- A teacher can publish one generated report card to an accepted learner.
  Published cards appear in that learner's results page and are downloadable
  by that learner as PDF or CSV. Other learners cannot access them.

## Additional requirements added after stage 7: deletion and recovery

- Deleting records moves them to a private recycle bin owned by the signed-in
  account. The bin supports search and record-type filtering, restore, and
  permanent deletion.
- Bulk-delete actions operate only on the current server-side search and filter
  scope. Teacher actions remain limited to the teacher's accepted learners and
  own results; teacher accounts and academic periods remain administrator-only.
- Related records deleted by a cascade are stored and restored together.
  Related records whose links are cleared by deletion have those links restored
  without overwriting unrelated edits made afterward.
- The main learner, teacher, academic-period, and results lists provide search
  and filtered bulk-delete controls. Academic periods with results or
  assessment settings cannot be bulk-deleted.
- Use Namibia Central Africa Time (`Africa/Windhoek`, GMT+2) for application
  dates and times. Log users out after five minutes without activity and show a
  message asking them to sign in again; saved database records remain intact.
- An administrator can create another administrator account. New accounts
  receive generated account IDs and one-time PINs.

