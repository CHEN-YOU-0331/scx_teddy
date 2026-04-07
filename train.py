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
META_COLUMNS = {"tid", "tgid", "ppid", "comm"}


def get_feature_columns(df):
    """Derive feature columns from the DataFrame (all columns except metadata)."""
    return [col for col in df.columns if col not in META_COLUMNS]


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

    # Print
    for c in range(n_clusters):
        key = f"cluster_{c}"
        print(f"\n--- {key} ---")
        for m in clusters[key]:
            if has_meta:
                print(f"  tid={m['tid']}, tgid={m.get('tgid')}, ppid={m.get('ppid')}, cmd={m.get('command')}")
            else:
                print(f"  tid={m['tid']}")

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
    parser.add_argument("--filter-cmd", nargs="*", help="Filter by command name(s)")
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
