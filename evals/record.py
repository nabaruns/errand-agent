"""Record the live-model scenario against real Claude, for offline replay by the eval suite.

    uv run python -m evals.record          # needs CLAUDE_API_KEY or ANTHROPIC_API_KEY (reads .env)

Uses the same environment as the replay scenario (fake clock, seeded mocks, forced timeout after
success), so the replay sees identical tool results.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from errand_agent.claude_model import ClaudeModel
from errand_agent.cli import load_dotenv
from errand_agent.model import RecordingModel

from .scenarios import RECORDING, SCENARIOS, build_env, drive_plumber_conversation


def main() -> int:
    load_dotenv(Path(".env"))
    spec = next(s for s in SCENARIOS if s.fn.__name__ == "recorded_live")
    if RECORDING.exists():
        RECORDING.unlink()
    with tempfile.TemporaryDirectory(prefix="eval-record-") as tmp:
        env = build_env(spec, Path(tmp))
        env.model = RecordingModel(ClaudeModel(), RECORDING)
        env.restart()
        drive_plumber_conversation(env)
        for e in env.events("run_end"):
            print(f"{e['run_id']}: {e['outcome']}  ${e['cost_usd']:.4f}\n  {e['reply'][:300]}\n")
        problems = env.invariant_violations()
        env.close()
    print(f"Recorded {len(env.model.responses)} responses to {RECORDING}")
    print("Invariant violations:", problems or "none")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
