# Slide Deck

1. Identify the audience, desired decision, time allowance, and evidence boundary.
2. Write a one-sentence narrative before creating slides.
3. Give every slide one purpose and a conclusion-style title.
4. Keep body points concise; move detail into speaker notes.
5. Link factual slides to source identifiers and mark unsupported visual ideas as proposals.
6. Review the deck for narrative gaps, duplicate slides, unsupported numbers, and missing decisions.
7. Call ppt.render only after the deck structure is complete and validated.
8. Call ppt.export_pdf only with the same-scope Artifact ref returned by ppt.render.
9. If explicit file delivery was requested, call send_file with the returned Artifact ref; never
   pass raw content, base64, or a host path.
10. Return the structured deck contract and report only Artifact refs returned by capabilities.

Load references/story.md when the argument needs restructuring. Load templates/deck.md only when
editable Markdown slide source or governed file delivery is requested. File delivery is optional,
requires explicit user intent, and may pause for approval. Never write a process command, Python
snippet, executable path, or output path; the Execution Profile owns every process argument.
