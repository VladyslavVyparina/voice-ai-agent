# Valeriy / Pulse AI — 4-layer prompt architecture

These files are separate layers. **Do not mix them** into one prompt.

## 1. System Prompt — `system_prompt.txt`
How Valeriy behaves and speaks: role, dialogue logic, demo→sales flow, objections, language, forbidden phrases, honesty about AI.

## 2. Knowledge Base — `knowledge_base.txt`
What Valeriy knows about Pulse AI: products, prices, capabilities, integrations, cases, timelines, limitations. Facts only — no dialogue rules.

## 3. Call Briefing — `call_briefing.txt`
Goal of a specific conversation: what to discover, how to map a solution, next-step options, result statuses. Not product facts and not speaking style.

## 4. CRM Output — `crm_output.txt`
What to save after the call: structured fields for Pulse CRM so the next manager understands the lead without re-asking.

## Rule
Keep layers separate. System Prompt = behavior. Knowledge Base = facts. Call Briefing = call goal. CRM Output = post-call structure.
