#!/usr/bin/env python3
"""Causal launch-name/symbol narrative features for autonomous entry hazard.

Names and symbols are known at creation and can encode meme templates, copied
narratives, celebrity/news references and coordinated naming conventions that
numeric flow features miss.  Text transforms are fitted on training windows
only; holdout vocabulary and outcomes are never inspected during fitting.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
from scipy import sparse
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer

from scripts import e4_v12_allout_profit_hazard as base
from scripts import e4_v12_allout_profit_hazard_strict  # noqa: F401
from scripts.e4_v12_allout_profit_hazard_stream import load_corpus_stream

VERSION = "e4-v12-narrative-text-hazard-v1"


def load_text(path: Path) -> list[str]:
    opener = gzip.open if path.suffix == ".gz" else open
    output = []
    with opener(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            name = str(row.get("name") or "")
            symbol = str(row.get("symbol") or "")
            host = str(row.get("metadata_uri_host") or "")
            output.append(f"name={name} symbol={symbol} host={host}")
    return output


def text_components(
    documents: list[str],
    train_mask: np.ndarray,
    seed: int,
    components: int,
) -> tuple[np.ndarray, dict[str, Any]]:
    train_docs = [documents[index] for index in np.flatnonzero(train_mask)]
    char = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(2, 5),
        min_df=2,
        max_features=45_000,
        sublinear_tf=True,
        lowercase=True,
        strip_accents="unicode",
    )
    word = TfidfVectorizer(
        analyzer="word",
        ngram_range=(1, 2),
        min_df=2,
        max_features=20_000,
        sublinear_tf=True,
        lowercase=True,
        strip_accents="unicode",
        token_pattern=r"(?u)\b\w+\b",
    )
    char.fit(train_docs)
    word.fit(train_docs)
    train_matrix = sparse.hstack(
        (char.transform(train_docs), word.transform(train_docs)), format="csr"
    )
    effective = max(2, min(components, train_matrix.shape[1] - 1, train_matrix.shape[0] - 1))
    svd = TruncatedSVD(n_components=effective, n_iter=8, random_state=seed)
    svd.fit(train_matrix)
    all_matrix = sparse.hstack(
        (char.transform(documents), word.transform(documents)), format="csr"
    )
    dense = svd.transform(all_matrix).astype(np.float32)
    return dense, {
        "char_vocabulary": len(char.vocabulary_),
        "word_vocabulary": len(word.vocabulary_),
        "components": effective,
        "explained_variance_ratio_sum": float(svd.explained_variance_ratio_.sum()),
        "fit_split": "train",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument(
        "--lane",
        choices=(
            "intent-logistic",
            "intent-hgb",
            "intent-extra",
            "profit-logistic",
            "profit-hgb",
            "profit-extra",
            "multitask",
            "multitask-identity",
        ),
        required=True,
    )
    parser.add_argument("--seed", type=int, default=812_413)
    parser.add_argument("--components", type=int, default=96)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    corpus = load_corpus_stream(args.corpus, 0)
    documents = load_text(args.corpus)
    if len(documents) != len(corpus.rows):
        raise ValueError("text and numeric corpus row counts differ")
    train = corpus.splits == "train"
    text_matrix, audit = text_components(
        documents, train, args.seed, args.components
    )
    names = [f"narrative_text_svd_{index}" for index in range(text_matrix.shape[1])]
    corpus.x_general = np.concatenate((corpus.x_general, text_matrix), axis=1)
    corpus.feature_names_general.extend(names)
    corpus.x_identity = np.concatenate((corpus.x_identity, text_matrix), axis=1)
    corpus.feature_names_identity.extend(names)

    report = base.evaluate_lane(corpus, args.lane, args.seed)
    report["version"] = VERSION
    report["text_audit"] = audit
    report["text_fields"] = ["name", "symbol", "metadata_uri_host"]
    report["causality"] = "creation-time text only; transforms fitted on train"
    corpus_hash = hashlib.sha256(args.corpus.read_bytes()).hexdigest()
    report["corpus_sha256"] = corpus_hash
    report["experiment_id"] = "e4x-text-" + hashlib.sha256(
        json.dumps(
            {
                "version": VERSION,
                "lane": args.lane,
                "seed": args.seed,
                "components": args.components,
                "corpus": corpus_hash,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(base.json_safe(report), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "lane": args.lane,
                "status": report.get("status"),
                "experiment_id": report["experiment_id"],
                "text_audit": audit,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
