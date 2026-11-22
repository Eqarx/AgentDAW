# AgentDAW Tool Guardrail

When creating, editing, inspecting, or rendering Kontakt 8 / multi-track audio projects with AgentDAW:
- Load the `agent-daw` skill for exact tool arguments and JSON schema details.
- Always query real local patch paths with `list_instruments` (or `studio.cmd library`) before referencing an instrument in a project JSON.
- Always pass absolute file paths to AgentDAW MCP tools.
- Validate and inspect metering output (`seconds`, `peak_dbfs`, `rms_dbfs`, per-track peaks) after rendering.
- When evaluating or revising rendered audio, use `audition_audio` or `audition_project` and call `read_file` on `preview_path` so you listen natively to the actual audio alongside its objective acoustic telemetry before making edits.
- When creating a revision intended for comparison with an earlier version, save to a distinct `.json` and `.wav` filename so earlier outputs remain intact unless the user asks to overwrite.
