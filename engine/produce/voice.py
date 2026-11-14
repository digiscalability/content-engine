"""Voice: ElevenLabs text-to-speech with Abbas's clone (SRD FR-PRD-2), behind `VoiceProvider`.

Every batch is preflighted against both limits of §3.5 before any call: the engine's own
monthly cap (D10, 78,600 characters, read from the ledger) and the live account balance,
which must stay above the floor that protects Life Island (C16, 20,000). Each script is
charged once: its ledger row is keyed `voice:script:<id>`, and a WAV already on disk is
never synthesised again (NFR-1). ElevenLabs documents no charge header, so the ledger
records the characters sent (ADR 0010).
"""

from __future__ import annotations

import io
import sqlite3
import wave
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

ELEVENLABS_API = "https://api.elevenlabs.io"
DEFAULT_MODEL = "eleven_multilingual_v2"
OUTPUT_FORMAT = "wav_48000"  # FR-PRD-2: WAV 48 kHz
SAMPLE_RATE = 48000


class VoiceError(RuntimeError):
    """The voice provider could not produce audio."""


class VoiceBudgetError(VoiceError):
    """A batch would break the engine cap (D10) or the account floor (C16)."""


@dataclass(frozen=True)
class PronunciationDictionary:
    id: str
    version_id: str | None = None


@dataclass(frozen=True)
class VoiceRequest:
    script_id: int
    text: str
    voice_id: str
    model_id: str = DEFAULT_MODEL
    dictionaries: tuple[PronunciationDictionary, ...] = ()

    @property
    def characters(self) -> int:
        return len(self.text)

    @property
    def charge_key(self) -> str:
        return f"voice:script:{self.script_id}"


@dataclass(frozen=True)
class Balance:
    used: int
    limit: int
    resets_at: int | None = None  # unix time

    @property
    def remaining(self) -> int:
        return self.limit - self.used


@runtime_checkable
class VoiceProvider(Protocol):
    def balance(self) -> Balance: ...

    def synthesize(self, request: VoiceRequest, output: Path) -> Path: ...


def silent_wav_bytes(seconds: float, rate: int = SAMPLE_RATE) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * round(seconds * rate))
    return buffer.getvalue()


def _write_atomically(output: Path, data: bytes) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".part")
    partial.write_bytes(data)
    partial.replace(output)
    return output


@dataclass
class ElevenLabsVoice:
    api_key: str
    transport: httpx.BaseTransport | None = None
    base_url: str = ELEVENLABS_API

    def _client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.base_url,
            headers={"xi-api-key": self.api_key},
            transport=self.transport,
            timeout=180,
        )

    def balance(self) -> Balance:
        with self._client() as client:
            response = client.get("/v1/user/subscription")
        self._check(response, "subscription")
        data = response.json()
        return Balance(
            used=int(data["character_count"]),
            limit=int(data["character_limit"]),
            resets_at=data.get("next_character_count_reset_unix"),
        )

    def synthesize(self, request: VoiceRequest, output: Path) -> Path:
        body: dict = {"text": request.text, "model_id": request.model_id}
        if request.dictionaries:
            body["pronunciation_dictionary_locators"] = [
                {"pronunciation_dictionary_id": d.id, "version_id": d.version_id}
                for d in request.dictionaries[:3]  # the API takes at most three
            ]
        with self._client() as client:
            response = client.post(
                f"/v1/text-to-speech/{request.voice_id}",
                params={"output_format": OUTPUT_FORMAT},
                json=body,
            )
        self._check(response, f"text-to-speech for script {request.script_id}")
        if not (response.content[:4] == b"RIFF" and response.content[8:12] == b"WAVE"):
            raise VoiceError(f"ElevenLabs returned something other than WAV for {OUTPUT_FORMAT}")
        return _write_atomically(output, response.content)

    @staticmethod
    def _check(response: httpx.Response, what: str) -> None:
        if response.status_code != 200:
            raise VoiceError(
                f"ElevenLabs {what} failed ({response.status_code}): {response.text[:300]}"
            )


@dataclass
class FakeVoice:
    """Stands in for ElevenLabs in dry runs and tests: silent WAVs, an in-memory balance."""

    silent = True  # by design: the spec check does not fail its silent mix (FR-QA-4)
    limit: int = 131_000
    used: int = 0
    calls: list[VoiceRequest] = field(default_factory=list)

    def balance(self) -> Balance:
        return Balance(used=self.used, limit=self.limit)

    def synthesize(self, request: VoiceRequest, output: Path) -> Path:
        self.calls.append(request)
        self.used += request.characters
        seconds = max(0.5, len(request.text.split()) / 2.5)  # about 150 words a minute
        return _write_atomically(output, silent_wav_bytes(seconds))


def preflight(
    characters: int, engine_used: int, balance: Balance, monthly_cap: int, account_floor: int
) -> None:
    """Refuse a batch that would pass the engine's cap or the account's floor (§3.5)."""
    if engine_used + characters > monthly_cap:
        raise VoiceBudgetError(
            f"engine cap: {engine_used} used this month + {characters} needed > {monthly_cap} (D10)"
        )
    if balance.remaining - characters < account_floor:
        raise VoiceBudgetError(
            f"account floor: {balance.remaining} left - {characters} needed "
            f"< {account_floor} kept for Life Island (C16)"
        )


def engine_characters(conn: sqlite3.Connection, month: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM budget_ledger"
        " WHERE vendor = 'elevenlabs' AND month = ?",
        (month,),
    ).fetchone()
    return int(row[0])


def _record(conn: sqlite3.Connection, request: VoiceRequest, month: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO budget_ledger (vendor, month, units, charge_key) "
        "VALUES ('elevenlabs', ?, ?, ?)",
        (month, request.characters, request.charge_key),
    )


def synthesize_batch(
    requests: Sequence[VoiceRequest],
    provider: VoiceProvider,
    conn: sqlite3.Connection,
    out_dir: Path,
    monthly_cap: int,
    account_floor: int,
    month: str,
) -> dict[int, Path]:
    """Voice every script once, after one preflight for the whole batch."""
    outputs = {r.script_id: out_dir / f"script-{r.script_id}.wav" for r in requests}
    pending = [r for r in requests if not outputs[r.script_id].is_file()]
    for request in requests:
        if request not in pending:
            _record(conn, request, month)  # backfill a row lost to a crash; no new charge
    if pending:
        preflight(
            sum(r.characters for r in pending),
            engine_characters(conn, month),
            provider.balance(),
            monthly_cap,
            account_floor,
        )
    for request in pending:
        provider.synthesize(request, outputs[request.script_id])
        _record(conn, request, month)
    return outputs
