"""JSON schemas for structured LLM output.

Every schema sets ``additionalProperties: false`` and lists every property in
``required`` - both are conditions of the API's json_schema output format.
"""

from __future__ import annotations

SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Working title, <= 40 chars"},
        "hook": {
            "type": "string",
            "description": "The first spoken/on-screen line. Must land within 3 seconds.",
        },
        "body": {"type": "string", "description": "Main narration, plain prose"},
        "cta": {"type": "string", "description": "One single call to action"},
        "lines": {
            "type": "array",
            "description": "Ordered beats covering the whole runtime with no gaps",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "narration": {"type": "string"},
                    "telop": {
                        "type": "string",
                        "description": "On-screen text, <= 20 full-width chars",
                    },
                    "visual": {"type": "string", "description": "What is on screen"},
                },
                "required": ["index", "start", "end", "narration", "telop", "visual"],
                "additionalProperties": False,
            },
        },
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "rationale": {
            "type": "string",
            "description": "Which research findings drove these choices",
        },
    },
    "required": ["title", "hook", "body", "cta", "lines", "hashtags", "rationale"],
    "additionalProperties": False,
}

STORYBOARD_SCHEMA = {
    "type": "object",
    "properties": {
        "style": {
            "type": "string",
            "description": "One consistent visual style for every shot",
        },
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "narration": {"type": "string"},
                    "telop": {"type": "string"},
                    "visual_prompt": {
                        "type": "string",
                        "description": "Self-contained image-generation prompt in English",
                    },
                    "camera": {
                        "type": "string",
                        "description": "Shot size and movement, e.g. 'medium close-up, slow push in'",
                    },
                    "transition": {
                        "type": "string",
                        "enum": ["cut", "dissolve", "whip", "zoom", "slide"],
                    },
                },
                "required": [
                    "index", "start", "end", "narration", "telop",
                    "visual_prompt", "camera", "transition",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["style", "shots"],
    "additionalProperties": False,
}

STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "hook_type": {
            "type": "string",
            "enum": [
                "question", "negative", "listicle", "curiosity",
                "result", "authority", "urgency", "statement",
            ],
        },
        "cta": {"type": "string"},
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "purpose": {"type": "string"},
                },
                "required": ["label", "start", "end", "purpose"],
                "additionalProperties": False,
            },
        },
        "takeaways": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Transferable lessons, not descriptions of this post",
        },
    },
    "required": ["hook_type", "cta", "beats", "takeaways"],
    "additionalProperties": False,
}

IMPROVEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["success", "partial", "failure", "inconclusive"],
        },
        "learnings": {"type": "string"},
        "next_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["action", "reason", "priority"],
                "additionalProperties": False,
            },
        },
        "next_hypothesis": {"type": "string"},
    },
    "required": ["verdict", "learnings", "next_actions", "next_hypothesis"],
    "additionalProperties": False,
}
