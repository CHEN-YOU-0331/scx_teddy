#!/usr/bin/env python3
"""Train a KMeans model from event.csv and export to JSON for Rust consumption."""

import argparse
import json
import sys

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler

# Columns that are metadata, not features for training
META_COLUMNS = {"tid", "tgid", "ppid", "comm", "ancestor"}


def get_feature_columns(df):
    """Derive feature columns from the DataFrame (all columns except metadata)."""
    reserved = META_COLUMNS | {"label"}
    return [col for col in df.columns if col not in reserved]


def load_train_config(path: str) -> list[str]:
    """Load comm prefixes from a train_config.config file.

    Returns a list of prefix strings. Lines starting with '#' and blank lines
    are ignored.
    """
    patterns = []
    with open(path) as f:
        for line in f:
            s = line.split("#")[0].strip()
            if s:
                patterns.append(s)
    return patterns


def select_by_patterns(df: "pd.DataFrame", patterns: list[str]) -> "pd.DataFrame":
    """Keep only rows whose comm matches any pattern (prefix match)."""
    if "comm" not in df.columns:
        print("Error: CSV does not contain 'comm' column.", file=sys.stderr)
        sys.exit(1)
    mask = df["comm"].astype(str).apply(
        lambda c: any(c == p or c.startswith(p) for p in patterns)
    )
    out = df[mask].copy()
    print(f"Selected {len(out)} tasks matching train_config patterns")
    return out


def find_best_k(X_scaled, k_range=range(2, 11)):
    """Use the elbow method (largest inertia drop) to pick K."""
    inertias = []
    for k in k_range:
        model = KMeans(n_clusters=k, n_init=10, random_state=42)
        model.fit(X_scaled)
        inertias.append(model.inertia_)
        print(f"  K={k}: inertia={model.inertia_:.2f}")

    # Find the K with the largest second derivative (elbow point)
    diffs = [inertias[i] - inertias[i + 1] for i in range(len(inertias) - 1)]
    diffs2 = [diffs[i] - diffs[i + 1] for i in range(len(diffs) - 1)]
    best_idx = np.argmax(diffs2) + 2  # +2 because diffs2 starts at k_range[2]
    best_k = list(k_range)[best_idx]
    return best_k, inertias


def train(csv_path, output_path, n_clusters=None):
    df = pd.read_csv(csv_path)
    print(f"Loaded {len(df)} tasks from {csv_path}")

    feature_columns = get_feature_columns(df)
    X = df[feature_columns].values

    # Standardize
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)

    # Determine K
    if n_clusters is None:
        print("Finding best K with elbow method...")
        n_clusters, _ = find_best_k(X_scaled)
        print(f"Selected K={n_clusters}")
    else:
        print(f"Using user-specified K={n_clusters}")

    # Train
    model = KMeans(n_clusters=n_clusters, n_init=10, random_state=42)
    model.fit(X_scaled)
    labels = model.labels_

    # Export model as JSON
    model_data = {
        "algorithm": "kmeans",
        "n_clusters": n_clusters,
        "features": feature_columns,
        "scaler": {
            "mean": scaler.mean_.tolist(),
            "std": scaler.scale_.tolist(),
        },
        "centroids": model.cluster_centers_.tolist(),
    }
    with open(output_path, "w") as f:
        json.dump(model_data, f, indent=2)
    print(f"Model saved to {output_path}")

    # Print cluster statistics
    print(f"\n{'='*60}")
    print(f"Classification results ({n_clusters} clusters)")
    print(f"{'='*60}")

    for c in range(n_clusters):
        mask = labels == c
        cluster_df = df[mask]
        print(f"\n--- Cluster {c} ({mask.sum()} tasks) ---")
        print(cluster_df[feature_columns].describe().to_string())

    # Build cluster membership from CSV metadata columns
    has_meta = "tgid" in df.columns
    print(f"\n{'='*60}")
    print("Cluster membership (tid, tgid, command)")
    print(f"{'='*60}")

    clusters = {}
    for c in range(n_clusters):
        mask = labels == c
        members = []
        for idx in np.where(mask)[0]:
            row = df.iloc[idx]
            member = {"tid": int(row["tid"])}
            if has_meta:
                member["tgid"] = int(row["tgid"]) if pd.notna(row.get("tgid")) else None
                member["ppid"] = int(row["ppid"]) if pd.notna(row.get("ppid")) else None
                member["command"] = row.get("comm", None)
            members.append(member)
        clusters[f"cluster_{c}"] = members

    # Save classification result
    result_path = output_path.replace(".json", "_result.json")
    with open(result_path, "w") as f:
        json.dump(clusters, f, indent=2)
    print(f"\nClassification result saved to {result_path}")


def main():
    parser = argparse.ArgumentParser(description="Train KMeans model from event.csv")
    parser.add_argument("csv", help="Path to event.csv")
    parser.add_argument("-o", "--output", default="model.json", help="Output model JSON path")
    parser.add_argument("-k", "--clusters", type=int, default=None,
                        help="Number of clusters (auto-detect if not specified)")
    parser.add_argument("--filter-tid", type=int, nargs="*", help="Filter by tid(s)")
    parser.add_argument("--filter-tgid", type=int, nargs="*", help="Filter by tgid(s)")
    parser.add_argument("--filter-cmd", nargs="*",
                        help="Filter by exact command name(s).")
    parser.add_argument("--train-config", default=None, metavar="PATH",
                        help="Path to train_config.config (comm prefix list). "
                             "If not given, all tasks in the CSV are used.")
    args = parser.parse_args()

    df = pd.read_csv(args.csv)

    # Apply filters using CSV metadata columns (no /proc access needed)
    if args.filter_tid:
        df = df[df["tid"].isin(args.filter_tid)]
        print(f"Filtered to {len(df)} tasks by tid")
    if args.filter_tgid:
        if "tgid" not in df.columns:
            print("Error: CSV does not contain 'tgid' column. Re-collect data with updated scx_teddy.", file=sys.stderr)
            sys.exit(1)
        df = df[df["tgid"].isin(args.filter_tgid)]
        print(f"Filtered to {len(df)} tasks by tgid")
    if args.filter_cmd:
        if "comm" not in df.columns:
            print("Error: CSV does not contain 'comm' column. Re-collect data with updated scx_teddy.", file=sys.stderr)
            sys.exit(1)
        df = df[df["comm"].isin(args.filter_cmd)]
        print(f"Filtered to {len(df)} tasks by command")
    elif args.train_config:
        patterns = load_train_config(args.train_config)
        print(f"Loaded {len(patterns)} comm patterns from {args.train_config}: {patterns}")
        df = select_by_patterns(df, patterns)
    else:
        print(f"No train_config specified — using all {len(df)} tasks in CSV")

    if len(df) == 0:
        print("No tasks remaining after filtering.", file=sys.stderr)
        sys.exit(1)

    # Write filtered CSV to a temp file and train from it
    filtered_path = args.csv + ".filtered.tmp"
    df.to_csv(filtered_path, index=False)
    train(filtered_path, args.output, args.clusters)

    import os
    os.remove(filtered_path)


if __name__ == "__main__":
    main()
