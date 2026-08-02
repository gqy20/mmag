# Slide Deck

1. Establish the audience, decision, evidence boundary, and one-sentence narrative.
2. Load `deck.md` when producing a file. Use only its supported layouts and Markdown.
3. Structure the story as context → tension → evidence → options → recommendation → next action.
   If evidence does not support that arc, surface the gap instead of forcing a conclusion.
4. Give each slide one conclusion-style heading and keep supporting points concise.
5. Put nuance in `notes`; put evidence identifiers in `sources`. Never invent a source.
6. Select only a registered `theme_ref`; the default is `corp@1.0.0`.
7. Call `ppt.build` once after the complete Markdown source is ready. It owns normalization,
   editable PPTX rendering, PDF export, preview generation, validation, and Artifact storage.
8. Treat the build as failed unless it returns `source_ref`, `pptx_ref`, and `preview_refs`.
   Never fabricate refs or retry through a different renderer.
9. Use the governed `/workspace` and `execute` only for steps that `ppt.build` cannot express. Commit
   only fixed filenames declared by the active Execution Profile through `workspace.commit`.
10. After `ppt.build` succeeds, call `send_file` once with `pptx_ref`. Delivery remains approval-gated;
   send source or preview files only when explicitly requested.

Never generate JavaScript, Python, CSS, shell commands, executable paths, host paths, arbitrary
HTML/Vue, remote image URLs, or base64. The model controls content and registered layout names;
the trusted renderer controls coordinates, typography, theme tokens, and executable arguments.
