"""readability: LLM rubric review anchored by deterministic style measurements.

Modes (``params.mode``):
* ``heuristic`` - only the deterministic measurements in ``autograder.style``
* ``llm``       - only the model's rubric score
* ``hybrid``    - ``llm_weight * llm + (1 - llm_weight) * heuristic`` (default)

The model is called with temperature 0 and a fixed seed, and its reply is
cached (see ``autograder.llm``), so a repeated run reuses the stored verdict.
If the model cannot be reached the analyzer falls back to the heuristic
score and raises an ``llm_unavailable`` flag, so the instructor knows.
"""

from __future__ import annotations

from ..llm import LLMClient, LLMError
from ..style import readability
from ..util import clamp01
from .base import Analyzer, AnalyzerResult, register

SYSTEM_PROMPT = """You are a strict teaching assistant who grades the READABILITY of student C programs.
Do NOT judge correctness or efficiency. Score with this rubric (integers):
- naming (0-3): descriptive variable/function names; single letters only for loop counters
- comments (0-2): a header/purpose comment and comments on non-obvious steps
- formatting (0-2): consistent indentation, spaces around operators and after commas, one statement per line
- structure (0-3): short focused functions, no magic numbers, clear control flow
score = naming + comments + formatting + structure (0-10). A cramped program with one-letter names and no
comments must not score above 5.
Reply with ONLY a JSON object:
{"naming": int, "comments": int, "formatting": int, "structure": int, "score": int,
 "reasons": [up to 4 short strings], "suggestions": [up to 3 short, concrete strings]}"""


_TOPICS = (("naming", ("name", "rename", "identifier")), ("comments", ("comment", "document", "header")),
           ("formatting", ("space", "indent", "format", "whitespace", "line")),
           ("clarity", ("magic", "constant", "#define")))


def _topic(suggestion: str) -> str:
    low = suggestion.lower()
    for topic, words in _TOPICS:
        if any(w in low for w in words):
            return topic
    return low


def _numbered(source: str, limit: int) -> str:
    lines = source.split("\n")
    text = "\n".join(f"{i:3d} | {line}" for i, line in enumerate(lines, 1))
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


@register
class LLMReview(Analyzer):
    name = "llm_review"
    description = "Readability: LLM rubric (temperature 0, fixed seed, cached) blended with style metrics"

    def run(self, ctx, params):
        mode = params.get("mode", "hybrid")
        llm_weight = float(params.get("llm_weight", 0.5))
        heur = readability(ctx.csource)
        evidence = {"mode": mode, "heuristic": heur}
        flags = []
        llm_score = None
        llm_data = {}

        if mode in ("llm", "hybrid"):
            client = LLMClient.from_params(params, cache_dir=ctx.cache_dir / "llm")
            user = (f"Problem statement:\n{ctx.rules.statement.strip() or '(not given)'}\n\n"
                    f"Student file {ctx.name}:\n{_numbered(ctx.source_text, int(params.get('max_source_chars', 8000)))}")
            try:
                data, reply = client.chat_json(SYSTEM_PROMPT, user)
                llm_score = self._rubric_score(data)
                llm_data = data
                evidence["llm"] = {"provider": client.provider, "model": client.model,
                                   "temperature": client.temperature, "seed": client.seed,
                                   "cached": reply.cached, "cache_key": reply.cache_key[:16],
                                   "raw_response": reply.text[:2000]}
            except LLMError as exc:
                evidence["llm"] = {"provider": client.provider, "model": client.model, "error": str(exc)[:300]}
                flags.append({"type": "llm_unavailable", "severity": "info",
                              "message": f"readability fell back to deterministic style metrics ({str(exc)[:120]})"})

        if llm_score is None:
            score, used = heur["score"], "heuristic" if mode == "heuristic" else "heuristic (LLM unavailable)"
        elif mode == "llm":
            score, used = llm_score, "llm"
        else:
            score = llm_weight * llm_score + (1 - llm_weight) * heur["score"]
            used = f"hybrid: {llm_weight:g} x llm ({llm_score * 10:.1f}/10) + {1 - llm_weight:g} x heuristic " \
                   f"({heur['score'] * 10:.1f}/10)"
        evidence["formula"] = used

        reasons = [str(r).strip().rstrip(".") for r in llm_data.get("reasons", [])
                   if isinstance(r, (str, int, float)) and str(r).strip()][:4]
        heur_reasons = self._heuristic_reasons(heur)
        summary = ", ".join((heur_reasons or reasons or ["readable"])[:3])
        feedback = "; ".join((reasons or heur_reasons or ["Code is readable"])[:4]) + "."
        # Concrete heuristic advice first, then the model's; one suggestion per topic.
        llm_suggestions = [str(s).strip() for s in llm_data.get("suggestions", []) if isinstance(s, str)]
        suggestions, topics = [], set()
        for s in self._heuristic_suggestions(heur) + llm_suggestions:
            topic = _topic(s)
            if s and topic not in topics and len(suggestions) < 4:
                topics.add(topic)
                suggestions.append(s if s.endswith(".") else s + ".")
        return AnalyzerResult(clamp01(score), summary, feedback, evidence, suggestions, flags=flags)

    @staticmethod
    def _rubric_score(data) -> float:
        parts = [data.get(k) for k in ("naming", "comments", "formatting", "structure")]
        if all(isinstance(p, (int, float)) for p in parts):
            total = min(3, max(0, parts[0])) + min(2, max(0, parts[1])) + min(2, max(0, parts[2])) \
                + min(3, max(0, parts[3]))
        elif isinstance(data.get("score"), (int, float)):
            total = data["score"]
        else:
            raise LLMError("reply has no usable score")
        return clamp01(float(total) / 10.0)

    @staticmethod
    def _heuristic_reasons(h):
        d, s = h["details"], h["subscores"]
        out = []
        if d["naming"]["terse"]:
            out.append("terse names: " + ", ".join(d["naming"]["terse"][:4]))
        if d["comments"]["comment_lines"] == 0:
            out.append("no comments")
        elif s["comments"] < 0.6:
            out.append("few comments")
        if s["formatting"] < 0.75:
            out.append("cramped formatting")
        if d["clarity"]["count"] >= 3:
            out.append("magic numbers")
        return out

    @staticmethod
    def _heuristic_suggestions(h):
        d, s = h["details"], h["subscores"]
        out = []
        if d["naming"]["terse"]:
            out.append(f"Rename {', '.join(d['naming']['terse'][:3])} to descriptive names.")
        if s["comments"] < 0.6:
            out.append("Add a header comment saying what the program does and comment non-obvious steps.")
        if s["formatting"] < 0.75:
            out.append("Put spaces around operators and after commas.")
        if d["clarity"]["count"] >= 3:
            out.append("Replace magic numbers with named constants (#define or const).")
        return out
