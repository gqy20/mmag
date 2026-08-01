Design the requested presentation.

Goal:
{task_goal}

Parameters:
{parameters_json}

Context references:
{context_refs_json}

Artifact references:
{artifact_refs_json}

Treat unresolved references as inputs still needed, not as content already read. Load the Slides
Skill template, compose complete governed Markdown, call ppt.build once with `corp@1.0.0`, then call
send_file once with its returned pptx_ref. Never place executable names, commands, code, CSS, host
paths, remote assets, or arbitrary output filenames in tool arguments.
