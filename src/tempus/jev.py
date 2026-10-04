"""Minimal client for TypeSafe's Jev decision model — https://docs.typesafe.ai/api.md

Jev returns calibrated decisions (a choice with a probability for every option, a score, or
a yes/no probability) rather than text, which is what the filing thresholds rely on.
"""

import time
from dataclasses import dataclass

import httpx

BASE_URL = "https://api.typesafe.ai/v1"
MODEL = "jev-latest"
MAX_RETRIES = 4


class JevError(RuntimeError):
    pass


@dataclass(frozen=True)
class ChoiceAnswer:
    choice: str
    probabilities: dict[str, float]

    def ranked(self) -> list[tuple[str, float]]:
        return sorted(self.probabilities.items(), key=lambda kv: kv[1], reverse=True)


class JevClient:
    def __init__(self, api_key: str | None, client: httpx.Client | None = None):
        if not api_key:
            raise JevError(
                "TYPESAFE_API_KEY is not set. Create one at https://console.typesafe.ai/"
            )
        self._client = client or httpx.Client(
            timeout=60, headers={"Authorization": f"Bearer {api_key}"}
        )

    def choice(self, state, instructions: str, criteria: dict[str, str]) -> ChoiceAnswer:
        answer = self._evaluate(
            state,
            {"q": {"type": "choice", "instructions": instructions, "criteria": criteria}},
        )["q"]
        return ChoiceAnswer(answer["choice"], answer["probabilities"])

    def _evaluate(self, state, questions: dict) -> dict:
        body = {"model": MODEL, "state": state, "questions": questions}
        for attempt in range(MAX_RETRIES):
            resp = self._client.post(f"{BASE_URL}/systemone", json=body)
            if resp.status_code in (429, 529) or resp.status_code >= 500:
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                raise JevError(f"Jev request failed ({resp.status_code}): {resp.text[:300]}")
            return resp.json()["answers"]
        raise JevError(f"Jev request failed after {MAX_RETRIES} attempts")
