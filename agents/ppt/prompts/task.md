Design the requested presentation.

Goal:
{task_goal}

Parameters:
{parameters_json}

Context references:
{context_refs_json}

Artifact references:
{artifact_refs_json}

Treat unresolved references as inputs still needed, not as content already read. If the user asks
for a file, render it through the governed execution capabilities. Never place executable names,
commands, Python code, host paths, or arbitrary output filenames in tool arguments.
