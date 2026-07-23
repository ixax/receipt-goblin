## AI Agent Routing & Execution Pipeline

This repository dictates a mandatory multi-agent cascading workflow to maximize reasoning capabilities while strictly optimizing token usage and budget. 

### 1. Model Hierarchy & Roles
*   **Architect / Planner (Opus / Fable)**: Used exclusively for high-level system reasoning, initial codebase analysis, structural design, and creating actionable implementation plans.
*   **Developer / Executor (Sonnet)**: Used exclusively for writing, refactoring, compiling, and debugging code.
*   **Utility Micro-Agent (Haiku)**: Used by the Developer for isolated, repetitive low-token tasks (e.g., writing JSDoc, basic unit tests, formatting).

### 2. Mandatory Handoff Rules (Workflow)

#### Phase 1: Planning (Opus/Fable Only)
The master agent (Opus/Fable) must analyze the user request and generate a complete, concrete blueprint. 
*   *Enforcement:* **Opus/Fable is strictly FORBIDDEN from writing or modifying any implementation code.** 
*   *Action:* Once the plan is saved (e.g., to a markdown file or tracking ticket), Opus/Fable **must** halt execution and spawn the `@developer` agent.

#### Phase 2: Implementation (Sonnet Only)
The `@developer` agent (Sonnet) takes over the context using the blueprint from Phase 1.
*   *Enforcement:* All continuous coding, sequential execution, and local debugging loops must stay inside Sonnet.
*   *Token Efficiency:* Do not feed the long historical reasoning thoughts of Opus into the Sonnet subagent. Only pass the final actionable plan to prevent token bloating.
*   *Sub-spawning:* Sonnet may spin up `haiku` agents for mundane utility tasks, but it **cannot** spin up high-level planners.

#### Phase 3: Emergency Escalation Trigger
The `@developer` agent (Sonnet) must code until the goal is fully achieved. It is forbidden from yielding control back to Opus/Fable for standard errors, failing tests, or runtime bugs. It may escalate back to the master agent **ONLY** if:
1.  A fundamental logical contradiction is discovered in the original architecture plan that makes implementation physically impossible.
2.  An unexpected blocker forces a major structural redesign of the core system layout or database schema (ClickHouse/Redis/LiteLLM structures).

### 3. Traceability Note
Every agent execution and model swap will be captured by the local LiteLLM proxy and logged down the pipeline (`webhook` -> `redis` -> `webhook-worker` -> `ClickHouse`), ensuring full visibility of this cascading workflow in the Grafana "Agents Overview" dashboard.
