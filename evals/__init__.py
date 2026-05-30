"""Eval harness for the Critic agent.

v1 scope: run-to-run variance only. Per check_id, measure how often the
LLM-judged Critic flips pass/fail on byte-identical input across K runs.

This package is read-only with respect to production agents: it imports
``agents.critic.evaluate_draft`` and drives it through its existing
``llm_call`` injection hook. The harness never modifies production code,
never writes to the Content Queue, and never calls OpenAI unless explicitly
opted in via ``--real`` at the CLI.
"""
