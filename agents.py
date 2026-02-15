"""
agents.py — Multi-agent debate system for research paper analysis.

Three agents (Scout, Advocate, Skeptic) debate in rounds, then a
Moderator synthesises a final verdict.  Uses the Anthropic Python SDK

you can also run `python agents.py` with a ANTHROPIC_API_KEY env
var to see a demo debate on a sample paper
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from anthropic import Anthropic

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1024

# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

SCOUT_SYSTEM = """\
You are the **Scout** — an expert at scanning new research and spotting
hidden gems.  Your job:

• Identify what is *genuinely novel* about the paper (method, dataset,
  result, or framing).
• Estimate the *potential impact* — academic and commercial.
• Flag which investor / VC verticals should care (e.g. biotech, robotics,
  climate-tech, AI infra, etc.).
• Be specific: cite numbers, comparisons, or prior work when possible.

Keep your response focused and under 300 words.
"""

ADVOCATE_SYSTEM = """\
You are the **Advocate** — an enthusiastic but rigorous champion of
promising research.  Your job:

• Build on the Scout's analysis and *strengthen* the case for this paper.
• Identify real-world applications, potential start-up ideas, or products
  that could emerge from this work.
• Draw connections to adjacent fields or market trends.
• Rebut the Skeptic's concerns point-by-point when they arise.

Stay grounded in evidence — never resort to empty hype.
Keep your response under 300 words.
"""

SKEPTIC_SYSTEM = """\
You are the **Skeptic** — a sharp, fair, but tough critic.  Your job:

• Stress-test the paper's claims: methodology, statistical rigour,
  dataset quality, reproducibility.
• Identify *risks* — technical barriers, market timing, ethical issues,
  regulatory headwinds.
• Call out when the Scout or Advocate are over-extrapolating from the
  evidence.
• Suggest what *additional evidence* would be needed to convince you.

Be direct and specific.  Aim for constructive criticism, not dismissal.
Keep your response under 300 words.
"""

MODERATOR_SYSTEM = """\
You are the **Moderator**.  You have just observed a multi-round debate
between a Scout, an Advocate and a Skeptic about a research paper.

Produce a **structured JSON** verdict with exactly these keys:

{
  "verdict": "PROMISING" | "INTERESTING" | "UNCERTAIN" | "WEAK",
  "confidence": <float 0-1>,
  "one_liner": "<1-sentence summary for a busy investor>",
  "key_strengths": ["...", "..."],
  "key_risks": ["...", "..."],
  "suggested_verticals": ["...", "..."],
  "follow_up_questions": ["...", "..."]
}

Return ONLY valid JSON — no markdown fences, no commentary outside the JSON.
"""


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    """Thin wrapper around a Claude conversation with a fixed system prompt."""

    name: str
    system_prompt: str
    client: Anthropic
    model: str = MODEL
    history: list[dict[str, str]] = field(default_factory=list)

    # ---- public API -------------------------------------------------------

    def say(self, user_message: str) -> str:
        """Send *user_message* and return the assistant's reply."""
        self.history.append({"role": "user", "content": user_message})
        response = self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=self.system_prompt,
            messages=self.history,
        )
        text = response.content[0].text
        self.history.append({"role": "assistant", "content": text})
        return text

    def reset(self) -> None:
        self.history.clear()


# ---------------------------------------------------------------------------
# Debate orchestrator
# ---------------------------------------------------------------------------

@dataclass
class DebateResult:
    paper: dict[str, Any]
    rounds: list[dict[str, str]]
    verdict: dict[str, Any]
    raw_verdict: str


def _format_paper(paper: dict[str, Any]) -> str:
    """Turn a paper dict (matching the Supabase ``papers`` schema) into a
    readable prompt block.

    Expected keys (from DB): paper_name, paper_authors (JSON list),
    published (date string), abstract, journal, fulltext, url, topic.
    Also tolerates the generic keys title/authors/abstract for testing.
    """
    title = paper.get("paper_name") or paper.get("title", "Unknown")
    authors = paper.get("paper_authors") or paper.get("authors", "Unknown")
    if isinstance(authors, list):
        authors = ", ".join(authors)
    abstract = paper.get("abstract", "")
    date = paper.get("published") or paper.get("date", "")
    url = paper.get("url", "")
    topic = paper.get("topic", "")
    journal = paper.get("journal", "")
    fulltext = paper.get("fulltext", "")

    lines = [f"**Title:** {title}"]
    if authors:
        lines.append(f"**Authors:** {authors}")
    if journal:
        lines.append(f"**Journal:** {journal}")
    if date:
        lines.append(f"**Published:** {date}")
    if topic:
        lines.append(f"**Topic:** {topic}")
    if abstract:
        lines.append(f"**Summary:** {abstract}")
    if fulltext:
        lines.append(f"**Full Text:**\n{fulltext}")
    if url:
        lines.append(f"**URL:** {url}")

    return "\n".join(lines)


def run_debate(
    paper: dict[str, Any],
    *,
    num_rounds: int = 2,
    client: Anthropic | None = None,
    model: str = MODEL,
    verbose: bool = False,
) -> DebateResult:
    """
    Run a multi-round Scout → Advocate → Skeptic debate on *paper*,
    then have a Moderator produce a structured JSON verdict.

    Parameters
    ----------
    paper : dict
        Should contain ``paper_name`` and ``abstract`` (Supabase schema),
        or ``title`` and ``abstract`` for local testing.
    num_rounds : int
        How many debate loops to run (default 3).
    client : Anthropic | None
        Re-use an existing client, or one will be created.
    model : str
        Claude model to use.
    verbose : bool
        Print agent responses to stdout as they happen.

    Returns
    -------
    DebateResult
    """
    if client is None:
        client = Anthropic()

    scout = Agent(name="Scout", system_prompt=SCOUT_SYSTEM, client=client, model=model)
    advocate = Agent(name="Advocate", system_prompt=ADVOCATE_SYSTEM, client=client, model=model)
    skeptic = Agent(name="Skeptic", system_prompt=SKEPTIC_SYSTEM, client=client, model=model)

    paper_text = _format_paper(paper)
    rounds: list[dict[str, str]] = []

    # --- Round 1: Scout opens papers --------------------------------------------------
    scout_msg = scout.say(
        f"Analyse this research paper and identify its novelty, impact, "
        f"and relevance for investors:\n\n{paper_text}"
    )
    rounds.append({"agent": "Scout", "round": 1, "message": scout_msg})
    if verbose:
        print(f"\n{'='*60}\n🔭 Scout (R1):\n{scout_msg}")

    last_scout = scout_msg

    for r in range(1, num_rounds + 1):
        # Advocate builds on Scout and rebuts Skeptic
        if r == 1:
            adv_prompt = (
                f"The Scout just said:\n\n{last_scout}\n\n"
                f"Build on these findings. What are the strongest opportunities here?"
            )
        else:
            adv_prompt = (
                f"The Skeptic just raised these concerns:\n\n{last_skeptic}\n\n"
                f"Rebut these points and reinforce the case for this paper."
            )
        adv_msg = advocate.say(adv_prompt)
        rounds.append({"agent": "Advocate", "round": r, "message": adv_msg})
        if verbose:
            print(f"\n{'='*60}\n💡 Advocate (R{r}):\n{adv_msg}")

        # Skeptic challenges
        skep_prompt = (
            f"Scout said:\n{last_scout}\n\n"
            f"Advocate added:\n{adv_msg}\n\n"
            f"Challenge the weakest points. What are the critical risks?"
        )
        skep_msg = skeptic.say(skep_prompt)
        rounds.append({"agent": "Skeptic", "round": r, "message": skep_msg})
        last_skeptic = skep_msg
        if verbose:
            print(f"\n{'='*60}\n🧐 Skeptic (R{r}):\n{skep_msg}")

        # Scout responds to Skeptic (unless it is the last round)
        if r < num_rounds:
            scout_rebuttal = scout.say(
                f"The Skeptic challenged:\n\n{skep_msg}\n\n"
                f"Respond to the strongest objection and refine your assessment."
            )
            rounds.append({"agent": "Scout", "round": r + 1, "message": scout_rebuttal})
            last_scout = scout_rebuttal
            if verbose:
                print(f"\n{'='*60}\n🔭 Scout (R{r+1}):\n{scout_rebuttal}")

    # --- Moderator verdict -----------------------------------------------------
    debate_transcript = "\n\n".join(
        f"[{entry['agent']} — Round {entry['round']}]\n{entry['message']}"
        for entry in rounds
    )
    moderator = Agent(
        name="Moderator",
        system_prompt=MODERATOR_SYSTEM,
        client=client,
        model=model,
    )
    raw_verdict = moderator.say(
        f"Here is the full debate transcript about the paper "
        f"\"{paper.get('paper_name') or paper.get('title', 'Unknown')}\":\n\n{debate_transcript}\n\n"
        f"Produce your JSON verdict now."
    )
    if verbose:
        print(f"\n{'='*60}\n⚖️  Moderator verdict:\n{raw_verdict}")

    # Try to parse the verdict as JSON -----------------------------------------
    # Claude sometimes wraps in ```json ... ``` despite being told not to.
    cleaned = raw_verdict.strip()
    if cleaned.startswith("```"):
        # Strip opening ```json and closing ```
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    try:
        verdict = json.loads(cleaned)
    except json.JSONDecodeError:
        verdict = {"raw": raw_verdict, "verdict": "UNCERTAIN", "confidence": 0.0}

    # Normalise common key variations Claude might use
    _KEY_ALIASES = {
        "key_strengths": ["strengths", "key_strength"],
        "key_risks": ["risks", "key_risk", "concerns"],
        "suggested_verticals": ["verticals", "industries", "sectors"],
        "follow_up_questions": ["questions", "follow_ups", "followup_questions"],
    }
    for canonical, aliases in _KEY_ALIASES.items():
        if canonical not in verdict:
            for alias in aliases:
                if alias in verdict:
                    verdict[canonical] = verdict.pop(alias)
                    break

    return DebateResult(
        paper=paper,
        rounds=rounds,
        verdict=verdict,
        raw_verdict=raw_verdict,
    )


# ---------------------------------------------------------------------------
# Debate a list of papers
# ---------------------------------------------------------------------------

def debate_papers(
    papers: list[dict[str, Any]],
    **kwargs,
) -> list[DebateResult]:
    """Run ``run_debate`` on every paper in *papers*."""
    client = kwargs.pop("client", None) or Anthropic()
    return [run_debate(p, client=client, **kwargs) for p in papers]


# ---------------------------------------------------------------------------
# CLI demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    sample_paper = {
        "paper_name": "Scaling Monosemanticity: Extracting Interpretable Features from Claude 3 Sonnet",
        "paper_authors": ["Adly Templeton", "Tom Conerly", "Jonathan Marcus", "Jack Clark"],
        "abstract": (
            "We apply sparse autoencoders to extract interpretable features from "
            "a production-scale language model (Claude 3 Sonnet) and find millions "
            "of features corresponding to a vast range of concepts — cities, people, "
            "code vulnerabilities, emotional states, and more.  We show features can "
            "be used to steer model behaviour and that scaling laws govern feature "
            "extraction."
        ),
        "published": "2024-05-21",
        "url": "https://transformer-circuits.pub/2024/scaling-monosemanticity/",
        "topic": "mechanistic interpretability",
        "journal": "Transformer Circuits Thread",
        "fulltext": "",  # empty for demo, real papers will have this
    }

    result = run_debate(sample_paper, num_rounds=2, verbose=True)

    print("\n\n" + "=" * 60)
    print("FINAL VERDICT (parsed):")
    print(json.dumps(result.verdict, indent=2))