"""Generate the synthetic demo inputs for the three tenant use cases.

Everything here is invented: names come from small fixed vocabularies, and the
seeded RNG makes the output identical on every run (``--seed 42`` default).
No real patient, customer or pharmacy data is used anywhere in this repo.

Output: ``data/synthetic/<use_case>.jsonl``, one request input per line.

    python scripts/generate_synthetic.py            # default 20 per use case
    python scripts/generate_synthetic.py --n 50 --seed 7
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"

FIRST = ["Ana", "Bruno", "Carla", "Diego", "Elisa", "Felipe", "Gabi", "Hugo"]
LAST = ["Silva", "Souza", "Lima", "Costa", "Rocha", "Alves", "Pereira", "Gomes"]
VISIT_TYPES = ["first visit", "follow-up", "routine check", "return visit"]
PAYERS = ["health plan A", "health plan B", "private pay"]
INGREDIENTS = ["Ingredient A", "Ingredient B", "Ingredient C", "Ingredient D"]
FORMS = ["capsule", "cream", "oral solution"]
DOC_KINDS = {
    "invoice": "Invoice no. {n}. Amount due: R$ {v:.2f}. Due date: 2026-{m:02d}-15.",
    "receipt": "Payment receipt {n}. Amount received: R$ {v:.2f} on 2026-{m:02d}-03.",
    "bank_statement": "Account statement, month {m:02d}. Closing balance R$ {v:.2f}.",
    "payslip": "Payslip ref {n}. Gross salary R$ {v:.2f}, competence {m:02d}/2026.",
}


def appointment(rng: random.Random) -> dict[str, object]:
    """Clinibud: notes of an appointment, for an administrative summary."""
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    return {
        "use_case": "clinical_summary",
        "tenant": "clinibud",
        "text": (
            f"Appointment record (synthetic). Patient {name}, {rng.choice(VISIT_TYPES)}, "
            f"arrived {rng.randint(0, 20)} min late, payer: {rng.choice(PAYERS)}. "
            f"Requested a certificate for work: {rng.choice(['yes', 'no'])}. "
            f"Next visit suggested in {rng.choice([15, 30, 60, 90])} days."
        ),
    }


def order(rng: random.Random) -> dict[str, object]:
    """OmniMagistral: free-text compounding order, for structured extraction."""
    items = [
        f"{rng.randint(1, 4) * 30} {rng.choice(FORMS)}s of {rng.choice(INGREDIENTS)} "
        f"{rng.choice([5, 10, 25, 50, 100])} mg"
        for _ in range(rng.randint(1, 3))
    ]
    return {
        "use_case": "order_extraction",
        "tenant": "omnimagistral",
        "text": (
            f"Order from {rng.choice(FIRST)} {rng.choice(LAST)} (synthetic): "
            + "; ".join(items)
            + f". Delivery: {rng.choice(['pickup', 'courier'])}."
        ),
    }


def financial_doc(rng: random.Random) -> dict[str, object]:
    """Plutus: short financial document text, for classification."""
    kind = rng.choice(sorted(DOC_KINDS))
    text = DOC_KINDS[kind].format(
        n=rng.randint(1000, 9999), v=rng.uniform(50, 9000), m=rng.randint(1, 12)
    )
    return {
        "use_case": "doc_classification",
        "tenant": "plutus",
        "text": f"(synthetic) {text}",
        "expected_label": kind,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=20, help="records per use case")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, fn in [
        ("clinical_summary", appointment),
        ("order_extraction", order),
        ("doc_classification", financial_doc),
    ]:
        path = OUT_DIR / f"{name}.jsonl"
        with path.open("w", encoding="utf-8", newline="\n") as fh:
            for _ in range(args.n):
                fh.write(json.dumps(fn(rng), ensure_ascii=False) + "\n")
        print(f"wrote {args.n} records to {path}")


if __name__ == "__main__":
    main()
