"""Hardcoded catalog entries for things that aren't SKILL.md files.

Descriptions mirror the ones surfaced in Claude Code's Agent tool spec and
slash-command help so the embedding model sees the same phrasing users do.
"""
from __future__ import annotations

SUBAGENTS: list[dict[str, str]] = [
    {
        "name": "Explore",
        "description": "Fast agent specialized for exploring codebases. Use to find files by patterns, search code for keywords, or answer questions about the codebase.",
    },
    {
        "name": "Plan",
        "description": "Software architect agent for designing implementation plans. Use to plan the implementation strategy for a non-trivial task.",
    },
    {
        "name": "general-purpose",
        "description": "General-purpose agent for researching complex questions, searching for code, and executing multi-step tasks.",
    },
    {
        "name": "feature-dev:code-architect",
        "description": "Designs feature architectures by analyzing existing codebase patterns and conventions, then providing comprehensive implementation blueprints.",
    },
    {
        "name": "feature-dev:code-explorer",
        "description": "Deeply analyzes existing codebase features by tracing execution paths, mapping architecture layers, understanding patterns and abstractions.",
    },
    {
        "name": "feature-dev:code-reviewer",
        "description": "Reviews code for bugs, logic errors, security vulnerabilities, code quality issues, and adherence to project conventions.",
    },
    {
        "name": "superpowers:code-reviewer",
        "description": "Use when a major project step has been completed and needs to be reviewed against the original plan and coding standards.",
    },
    {
        "name": "code-simplifier",
        "description": "Simplifies and refines code for clarity, consistency, and maintainability while preserving all functionality.",
    },
    {
        "name": "claude-code-guide",
        "description": "Answers questions about Claude Code (the CLI), Claude Agent SDK, and Claude API. Features, hooks, slash commands, MCP servers, settings.",
    },
    {
        "name": "statusline-setup",
        "description": "Configure the user's Claude Code status line setting.",
    },
]


COMMANDS: list[dict[str, str]] = [
    {
        "name": "/loop",
        "description": "Run a prompt or slash command on a recurring interval (e.g. every 5 minutes, or self-paced).",
    },
    {
        "name": "/schedule",
        "description": "Create, update, list, or run scheduled remote agents (triggers) that execute on a cron schedule.",
    },
    {
        "name": "/init",
        "description": "Generate or refresh the project's CLAUDE.md from the current codebase.",
    },
    {
        "name": "/help",
        "description": "Show Claude Code help and list available commands.",
    },
    {
        "name": "/clear",
        "description": "Clear the current conversation context.",
    },
    {
        "name": "/compact",
        "description": "Compact the conversation history to free context window.",
    },
    {
        "name": "/remember",
        "description": "Save something to memory for future sessions.",
    },
    {
        "name": "/fast",
        "description": "Toggle Fast mode (Opus 4.6 with faster output).",
    },
]
