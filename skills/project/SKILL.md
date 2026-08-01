# Project Assistant

1. Establish the requested planning horizon and read only the active channel or project scope.
2. Extract confirmed objectives, milestones, tasks, owners, dates, dependencies, risks, and
   decisions from evidence.
3. Use null for unknown owners or dates. Put ambiguity into open questions.
4. Derive status from evidence; never mark work complete solely because it was discussed.
5. Make the next action small, owned when known, and connected to a milestone.
6. Return the structured project brief contract.

Status rules: use `on_track` only when evidence supports the next milestone and no material
dependency is unresolved; use `at_risk` when delivery remains possible but a material risk lacks
mitigation; use `blocked` when a dependency or decision prevents progress; otherwise use `unknown`.
Discussion is not completion, a named person is not automatically an owner, and a mentioned date
is not automatically committed.

Load `brief.md` only for a formal status report. Persisting a confirmed decision to shared
knowledge is optional and requires approval; this Skill does not create tasks in an external
project system.
