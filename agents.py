"""AirGuard's agent harness: every model call goes through an Agent declared here.

    py agents.py            -> prints the crew (role, model, memory, budget)

Each agent has a name (its trace id in out/calls.jsonl), a model, optional conversation memory, and a
per-call context budget it refuses to exceed. Agents only PROPOSE or DESCRIBE; Python (checks.py)
decides pass/fail, verify.validate_scenario gates every scenario, meta_implement.sanitize gates code.
"""
import json
import llm


class Agent:
    """Chat agent (SubQ subq-preview or Meta). With memory=True it keeps the whole conversation -
    that is how the red team carries every earlier trace into the next round."""

    def __init__(self, name, model, role, system=None, memory=True, budget_tokens=1_000_000):
        self.name, self.model, self.role, self.memory, self.budget = name, model, role, memory, budget_tokens
        self.history = [{"role": "system", "content": system}] if system else []
        self.calls = 0

    def context_chars(self):
        return sum(len(json.dumps(m)) for m in self.history)

    def ask(self, content, rnd=None, image=None):
        """Send one user turn. Returns {"text", "json", "hash", "usage", "cached"} or None
        (no key / offline cache miss / over budget). The reply joins memory if memory=True."""
        msgs = self.history + [{"role": "user", "content": content}]
        est = sum(len(json.dumps(m)) for m in msgs) // 3            # conservative chars->tokens
        if est > self.budget:
            print(f"  [{self.name}] over budget ({est:,} > {self.budget:,} est. tokens) - skipped")
            return None
        call = llm.meta if self.model == "meta" else llm.subq
        r = call(msgs, image=image, agent=self.name, rnd=rnd) if self.model == "meta" else call(msgs, self.name, rnd)
        self.calls += 1
        if r is None:
            return None
        if self.memory:
            self.history = msgs + [{"role": "assistant", "content": r["text"]}]
        return {**r, "json": llm.extract_json(r["text"])}


class DecisionAgent:
    """SubQ Snap: named noul/choice/score questions over a long state -> probabilities (393k-token window)."""

    def __init__(self, name, role):
        self.name, self.model, self.role, self.memory, self.budget = name, "subq-snap", role, False, 393_216

    def decide(self, state, questions, rnd=None):
        return llm.snap(state, questions, self.name, rnd)

    def note(self, outcome):
        llm.note(outcome)


CREW = {
    "subq-redteam": "Long-horizon red team: FAA AC + SFO map + code + ALL previous traces, designs harder scenarios each round",
    "subq-improve": "Grows the versioned scenario library from escaped mutants + rejection feedback",
    "snap-triage": "Severity of each violation + whether to phone ops (picks SMS vs call only)",
    "snap-screen": "Reads ~370k tokens of raw traces, flags rule breaches - cross-check vs the Python judge",
    "snap-realism": "Drops implausible proposed scenarios before they are simulated",
    "snap-diff-baseline": "Diff-only reviewer - the status quo AirGuard is compared against",
    "meta-implement": "Writes controller updates from change requests (output is sanitized, then gated)",
    "meta-vision": "Describes the violation frame in plain English",
}


def redteam(system):
    return Agent("subq-redteam", "subq", CREW["subq-redteam"], system, memory=True, budget_tokens=1_200_000)


def improver(system):
    return Agent("subq-improve", "subq", CREW["subq-improve"], system, memory=False, budget_tokens=400_000)


def vision():
    return Agent("meta-vision", "meta", CREW["meta-vision"], memory=False, budget_tokens=50_000)


def implementer(system):
    return Agent("meta-implement", "meta", CREW["meta-implement"], system, memory=False, budget_tokens=50_000)


def snap(name):
    return DecisionAgent(name, CREW[name])


if __name__ == "__main__":
    models = {"subq": llm.SUBQ_MODEL, "meta": llm.META_MODEL, "snap": llm.SNAP_MODEL}
    print(f"{'agent':20} {'model':16} {'memory':7} {'budget tok':>11}  role")
    for name, role in CREW.items():
        m = models["snap" if name.startswith("snap") else "meta" if name.startswith("meta") else "subq"]
        mem = "yes" if name == "subq-redteam" else "no"
        bud = {"subq-redteam": "1,200,000", "subq-improve": "400,000"}.get(name, "393,216" if name.startswith("snap") else "50,000")
        print(f"{name:20} {m:16} {mem:7} {bud:>11}  {role}")
    print("\nJudge: deterministic Python (checks.py) - no agent decides PASS/BLOCK.")
