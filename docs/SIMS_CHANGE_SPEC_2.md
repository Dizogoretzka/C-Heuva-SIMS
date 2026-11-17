# C. Heuva Junior Secondary School Information System (SIMS) — Change Specification 2

I have a Django project, the C. Heuva Junior Secondary School Information System (SIMS), with three roles (administrator, teacher, learner), main code in `main_app`. The release is deployed and tested, but I found the problems below. Work through them in the stages given. Do not add anything that is not listed.

## Rules for this whole job

- Work on a copy of the database. Keep existing data in every migration.
- Use the word "class" (for example Grade 8A) and never "course". Attendance stays removed.
- Run the full test suite after each stage and fix what you break. If tests still fail, stop and tell me. Do not weaken, skip, or delete a test to make it pass.
- Save this text to `docs/SIMS_CHANGE_SPEC_2.md` first, and re-read it at the start of each stage.
- After the last stage, stop. Give me the final summary and wait for my next instruction. No extra features, no refactoring, no deployment.

## STAGE 1: Reports come out empty (fix first)

Problem: teachers and administrators cannot create reports. They come out empty, although the system holds learners, subjects, and marks.

- Reproduce it first, using the working-copy data and a seeded test class. Check every report: learner report card, class list, results mark sheet, school summary, Assignments report, learner list, teacher list, and the CSV version of each.
- Find the real cause for each one that fails (for example filters on the old course or session fields, joins on removed models, the academic period not being the current one, the "accepted" check, class matching, the move of old results to "Previous record" assessments, or a permission scope that returns nothing). Fix the cause. Do not remove or hide the "no data" message to make the problem look solved.
- Rules to keep: administrators see every learner and subject. A teacher sees only learners accepted by them and only their own subjects. Report cards are available to teachers and administrators at any time, whether or not they are published (see stage 4).
- Before the PDF or CSV is created, the report form shows how much data matches (for example "42 learners, 3 subjects, Term 3 2026"), so an empty result is obvious before the download.
- Add tests with seeded data that fail before your fix and pass after it, for each report, in both PDF and CSV, for a teacher and for an administrator.

## STAGE 2: Manage Results page

- Remove the "search learner" behaviour. After the teacher picks the class, subject, and academic period, display every accepted learner of that class in one table, with the assessment columns, weights, Final %, and grade as already built.
- Add a search bar above the table that filters the rows instantly by name or account ID as the teacher types, without reloading, and keeps the marks already typed. Show "No learner matches" when nothing matches. Without JavaScript, all rows still display.
- Add a Save button (fixed at the top and the bottom of the table). It saves all marks and the assessment settings (names, weights, maximums) in one database transaction. Mark the changed cells while unsaved, show "Unsaved changes", and warn before the teacher leaves the page with unsaved changes. If any cell is invalid, save nothing, show the error next to that cell, and keep everything the teacher typed.
- Typing marks counts as activity for the inactivity timer (stage 5).
- Record each changed mark in the activity log with the old and new value, as before.
- Add tests: saving works, nothing is saved when one cell is invalid, the search filters correctly, and a teacher cannot save marks for a learner or subject that is not theirs.

## STAGE 3: Importing learners and first sign-in

### a. Class on import

- If a row has no class, do not leave the learner unassigned silently. After the file is checked, the import page asks for the class to use for those rows (and shows how many rows it will apply to). Rows that already have a class keep it.
- Teacher: the teacher types or picks the class. If it is not yet in "My Classes and Subjects", show a confirmation that it will be added there, because imported learners are accepted for the importing teacher automatically.
- Administrator: the administrator must choose the class (the classes that teachers have set up are listed, and any other valid class can be typed). Because several teachers use the system, the administrator's import does not choose a teacher. The learners then follow the normal request flow to the teachers who teach that class.
- Use the shared class parser. Reject an invalid class with a clear message. Nothing is saved until the teacher or administrator confirms.

### b. First sign-in for imported learners only

- Imported learners are created without a usable password and are flagged "not yet activated". Remove generated passwords from the import result and from the credentials sheet. The sheet now lists the learner's name, class, and account ID, with the line "First sign-in: enter your account ID and leave the password empty."
- On the learner sign-in form, if a learner enters an account ID that is flagged and leaves the password empty, the system does not show an error. It sends them to a "Set your new password" page (new password and confirmation, checked with Django's password validators). After they save, the flag is removed and they are signed in.
- This applies only to imported learners. Learners who registered themselves, and learners added by hand, never see this step. For every other account an empty password is rejected with the usual generic message.
- Safety: the step page works only through a short-lived server-side token (about 10 minutes) created at that sign-in attempt, and cannot be opened by URL. Count attempts toward the existing lockout and the per-IP limit. Record the activation in the activity log without any secret. Teachers and administrators can see which of their imported learners are "not yet activated" in the learner list, and can reset that status for a learner who has already activated by mistake, which means the learner must set a password again.
- Existing learners keep their current passwords. This applies only to learners imported from now on.
- Add tests for all of the above, including that a registered learner with an empty password is rejected.

### c. Alphabetical order

- Everywhere learners appear, sort them by surname and then first name, ignoring upper and lower case and accents. This includes every learner list, the mark entry table, learner requests, class lists, the Assignments page, dropdowns, every PDF and CSV report, and the credentials sheet. Add a test for it.

## STAGE 4: Publish and unpublish report cards

- Learners see and download their report card only for subjects whose results have been published for that academic period.
- Teacher: on the Reports page, for a chosen class and academic period, add "Publish all report cards" and "Unpublish all report cards" buttons, each with a confirmation that shows how many learners it affects. Also add a publish switch on each learner's row. This publishes the results of the teacher's own subjects. Show a "Published / Not published" status.
- Administrator: the same buttons, working for any class, and a school-wide option for a whole academic period (all classes and teachers), with a confirmation showing the numbers.
- Unpublishing hides the report card from learners at once. A learner whose report card is not published sees "Your report card has not been published yet." Teachers and administrators can still create and preview report cards in every state.
- Record every publish and unpublish in the activity log.
- Add tests for each rule above.

## STAGE 5: Inactivity timer

- Remove the five-minute inactivity logout, its message, its code, and its tests, and the mention of it in the SRS.
- Add a countdown in the top navigation bar for all three roles, from 1:00:00 down, shown in hours, minutes, and seconds. It resets to 1:00:00 whenever the user is active (mouse, keyboard, touch, scroll, and typing in the marks table), and when it reaches zero the user is signed out and sent to the sign-in page with the message "You were signed out after 1 hour of inactivity."
- Make the time limit a setting, `SIMS_IDLE_TIMEOUT_SECONDS`, default 3600.
- Enforce it on the server, not only in the browser. A request after an hour of inactivity must be refused and redirected to sign-in. To stop an active user from being signed out while typing on a page that makes no requests, the page sends a small keep-alive request when the user is active, at most once every 5 minutes, and this request resets the server timer. The keep-alive must not show the crest loading screen.
- Calculate the remaining time from timestamps, not by counting seconds, so it stays correct when the computer sleeps or the tab is in the background. Several open tabs share the same timer. Do not show the timer on the landing page or sign-in pages.
- Record "signed out after inactivity" in the activity log.
- The timer must be readable (`role="timer"`, not announced every second), fit on a phone in the top bar, and not break the sidebar or the crest.
- Add tests for the setting, the server refusal after the limit, the keep-alive, and the removal of the old five-minute code.

## STAGE 6: Finish

- Update the SRS wording for everything above.
- Run the full test suite once more.
- Report: what changed, test results, the root cause you found for the empty reports, and any manual steps for me. Then STOP and wait.
