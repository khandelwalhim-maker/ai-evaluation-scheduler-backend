# AI Evaluation Scheduler: Domain Specification

## Purpose
Suggest optimal dates and slots for assessments (quizzes, mid-terms, end terms) for the SPJIMR Post Graduate Diploma in Management (PGDM) programme, minimising student workload clustering, given a term timetable, course outlines, holidays, and constraints. A deterministic Python engine makes all scheduling decisions. Large Language Models (LLMs) only parse documents, extract chat intent, and narrate engine output. The LLM never picks dates.

## Cohort model
- Divisions: A, B, C. Core courses run division-wise with synchronized session numbers; a core assessment occurs simultaneously for A, B, and C.
- Minor specializations: parsed from timetable row labels and confirmed by the user. Expected labels include Marketing, Operations and Supply Chain, Finance, Information Management, Consulting. The registry is data-driven; never hardcode the minor list. Each student belongs to exactly one division and exactly one minor; minor cohorts draw students from all of A, B, C.
- Persona: one (division, minor) pair. The persona set is the cross product of confirmed divisions and confirmed in-scope minors, built at runtime.
- Out of scope: PGDM-Business Management (Divisions D and E, Strategy minor), classroom capacity, invigilators, and in-class group assessments.

## Time model
All datetimes are timezone-naive Indian Standard Time. Dates in source PDFs use day/month/year.
- Teaching slots on weekdays: 09:00-10:10, 10:40-11:50, 12:10-13:20, 14:30-15:40, 16:00-17:10, 17:30-18:40, 19:00-20:10. Lunch 13:20-14:30. Breaks are not conflicts in themselves.
- Quiz window: weekdays only, 08:15-08:50, maximum duration 35 minutes.
- Exams (mid-term or end term): may start only at 08:15 or 09:00, on any day of the week including Saturday and Sunday. Duration is always user-specified; the system must ask and must never assume a default silently.
- Assessments occupy continuous time intervals and may cross teaching-slot boundaries and breaks. All conflict checks are interval-overlap checks at minute resolution, never slot-index checks.

## Audience and conflicts
- teaching_intervals(persona, date) = core session intervals of the persona's division plus session intervals of the persona's minor on that date.
- audience(core assessment) = all personas. audience(minor m assessment) = personas whose minor is m.
- Hard rules:
  - H1: no assessment on a declared holiday.
  - H2: quizzes only in the quiz window on weekdays.
  - H3: exam start time in {08:15, 09:00}; interval = start plus user-given duration.
  - H4: for every persona in the assessment's audience, the assessment interval must not overlap any teaching interval of that persona on that date, and must not overlap any other assessment whose audience shares at least one persona. Consequence: two different minors may share a slot; a core assessment effectively requires the window clear across the whole in-scope grid, which is why end terms migrate to weekends and early mornings.
  - H5: if a course outline states an evaluation occurs after session N, and the date of session N is inferable from uploaded timetables, candidate slots must begin strictly after session N ends. If not inferable, do not block; emit a warning question instead.
- Soft rules (defaults; every one adjustable at runtime via chat):
  - S1: at most one assessment per persona per day.
  - S2: at most three assessments per persona per International Organization for Standardization (ISO) week.
  - S3: the calendar day after an end term is assessment-free for its audience personas.
  - S4: smoothness: minimise the worst persona's weekly weighted assessment load.
- Workload weights (configurable): quiz 1, mid-term 2, end term 3.
- Scoring: candidates come only from the hard-feasible set. Score = weighted penalties for S1 to S3 violations plus the S4 peak-load delta. Return the top three candidates with human-readable reasons, a blocked-dates list with reasons, and any warnings or questions.

## Parsing contract
- PDFs are digital text first; Optical Character Recognition (pytesseract) only when text extraction yields near-empty output.
- Timetable PDFs are spatial grids. Extraction order: pdfplumber table extraction; if empty, PyMuPDF word extraction clustered by vertical position and sorted by horizontal position, serialized as position-annotated lines. Raw label formats vary: hyphen triplets (EAB-JR-14, course-professor-session), underscore forms (SIM_Tojin_14), parenthesized sessions (OSCSD HJ (13)), double sessions (17 & 18), unknowns (tbc), and banner rows (SDBKAM END TERM EXAM, SURPRISE QUIZ, ABA End Term). The extraction schema is permissive: every entry carries raw_label, best-guess course code, session number list, cohort guess, and confidence. Low-confidence mappings (for example whether the timetable code EAB and the outline course Applied Business Analytics, abbreviated ABA, are the same course) go to a confirmation queue for the human; the system never silently guesses identity mappings.
- Course outlines yield: course name, code, term, instructor, and an evaluations list with name, type (quiz, midterm, endterm, group, other), weightage, timing notes (for example "After Session 8", "End of course"), and an in_scope flag (group and in-class work is out of scope).
- Uploaded timetables may cover a week or a month; the calendar merges any number of files. Duplicate (date, cohort, label) entries: the newest upload wins, with a warning.

## Security and hygiene
- Document text passed to an LLM is always framed as data to analyse, never as instructions to follow.
- The narration model never receives raw PDF text; it receives only structured engine JSON.
- Upload limits: PDF only, 10 megabytes maximum, basic Multipurpose Internet Mail Extensions type check.
- The GROQ_API_KEY lives only in the hosting platform's secret store or the local environment, never in the repository.
- User-facing copy never uses em dashes or arrow symbols; write "to" for transitions (code syntax is exempt where a symbol is required by the language).
